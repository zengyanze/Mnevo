#!/usr/bin/env python3
"""Mnevo butler v0.1 — 确定性流水线骨架：扫描疑似重复 → 提案 → 审计 → 人工确认面.

PLAN §7/Q26：管家不是常驻 agent。候选发现 = 确定性代码（引擎 /api/search 作
相似度 oracle，memory-as-query 找近重复对）；提案/审计落盘（markdown + jsonl，
天然 git/文件审计）；确认面 = 本 CLI 的 review 子命令（pi 会话里直接跑）。
LLM 蒸馏提案是 v0.2 插槽——scan 输出的提案文件即其输入格式。

目录布局（--home，默认 ~/.mnevo）::

    proposals/   *_merge.md   提案（front-matter + 两条内容全文对照）
    audit/butler.jsonl        追加式审计日志（scan/review/delete 全落笔）

用法::

    mnevo_butler.py scan                                  # 全库扫描，写提案
    mnevo_butler.py distill [--all]                       # LLM 蒸馏提案（新增>=3条才跑）
    mnevo_butler.py review --list                         # 待审提案
    mnevo_butler.py review --show <id前缀>                # 看全文
    mnevo_butler.py review --decide <id前缀> accept|dismiss
    mnevo_butler.py audit --tail 20
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mnevo_pack import fetch_all_memories, http_json, post_memory  # noqa: E402

SIM_THRESHOLD = 0.90       # 近重复判定（保守，误报宁可少）
SEARCH_LIMIT = 5
PROPOSAL_KIND = "merge-candidate"
DISTILL_KIND = "distill"
MIN_DISTILL_INPUT = 3      # 新增不足此数不吆喝 LLM
DEFAULT_LLM_BASE = "https://open.bigmodel.cn/api/coding/paas/v4"
DEFAULT_LLM_MODEL = "glm-5.3"


def llm_chat(base_url: str, api_key: str, model: str,
             system: str, user: str, timeout: int = 120) -> str:
    """OpenAI 兼容 chat/completions 单次调用（zai glm 等）。失败抛 RuntimeError。"""
    status, body = http_json(
        f"{base_url.rstrip('/')}/chat/completions", api_key, method="POST",
        body={"model": model, "temperature": 0,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=timeout)
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    if status != 200:
        raise RuntimeError(f"llm -> HTTP {status}: {text[:200]}")
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"llm response malformed: {text[:200]}") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("llm returned empty content")
    return content


def _parse_llm_json(text: str) -> dict:
    """从 LLM 输出里提取并校验提案 JSON；不合法抛 ValueError。"""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = fence.group(1) if fence else text
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no json object in llm output: {text[:120]}")
    data = json.loads(raw[start:end + 1])
    out = {"merges": [], "distills": [], "archives": []}
    for key in out:
        items = data.get(key, [])
        if not isinstance(items, list):
            raise ValueError(f"llm json '{key}' not a list")
        for it in items:
            if not isinstance(it, dict):
                raise ValueError(f"llm json '{key}' item not object")
            if key == "archives":
                if not isinstance(it.get("hash"), str) or not it["hash"]:
                    raise ValueError("archive item missing hash")
            else:
                hashes = it.get("hashes")
                content = it.get("content")
                if (not isinstance(hashes, list) or not hashes
                        or not all(isinstance(h, str) and h for h in hashes)
                        or not isinstance(content, str) or not content.strip()):
                    raise ValueError(f"{key} item needs non-empty hashes+content")
            out[key].append(it)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(hash_: str) -> str:
    return hash_[:12]


class Butler:
    def __init__(self, url: str, token: str, store: str, home: Path):
        self.url = url.rstrip("/")
        self.token = token
        self.store = store
        self.home = home
        self.proposals = home / "proposals"
        self.audit_path = home / "audit" / "butler.jsonl"
        self.proposals.mkdir(parents=True, exist_ok=True)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 审计 ----------
    def audit(self, event: str, **detail) -> None:
        entry = {"ts": _now_iso(), "event": event, **detail}
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---------- 提案文件 ----------
    @staticmethod
    def _pair_key(a: str, b: str) -> str:
        return f"{min(a, b)}|{max(a, b)}"

    def known_pairs(self) -> set[str]:
        pairs = set()
        for md in self.proposals.glob("*.md"):
            text = md.read_text(encoding="utf-8").splitlines()
            for line in text[:15]:
                m = re.match(r"^pair:\s*(\S+)$", line.strip())
                if m:
                    pairs.add(m.group(1))
        return pairs

    def write_proposal(self, keep: dict, dup: dict, score: float) -> Path:
        pair = self._pair_key(keep["content_hash"], dup["content_hash"])
        path = self.proposals / (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
            f"-{_short(dup['content_hash'])}-{PROPOSAL_KIND}.md"
        )
        body = "\n".join([
            "---",
            f"type: {PROPOSAL_KIND}",
            f"created: {_now_iso()}",
            "status: open",
            f"pair: {pair}",
            f"keep: {_short(keep['content_hash'])} (created {keep.get('created_at_iso')})",
            f"delete: {_short(dup['content_hash'])} (created {dup.get('created_at_iso')})",
            f"action: delete-duplicate",
            f"score: {score:.3f}",
            "decided_by: ''",
            "---",
            "",
            f"# 疑似重复对（相似度 {score:.3f}）",
            "",
            f"## 保留 {_short(keep['content_hash'])}",
            "",
            "```text",
            keep["content"],
            "```",
            "",
            f"## 删除 {_short(dup['content_hash'])}",
            "",
            "```text",
            dup["content"],
            "```",
            "",
            "依据：memory-as-query 检索相似度 >= "
            f"{SIM_THRESHOLD}，created_at 晚者为删除候选。",
            "",
        ])
        path.write_text(body, encoding="utf-8")
        return path

    # ---------- scan ----------
    def search_similar(self, content: str) -> list[dict]:
        status, body = http_json(
            f"{self.url}/api/search", self.token, method="POST",
            body={"query": content, "limit": SEARCH_LIMIT},
        )
        if status != 200:
            raise RuntimeError(f"search -> HTTP {status}: {str(body)[:120]}")
        return body.get("results", []) if isinstance(body, dict) else []

    def scan(self) -> int:
        memories = fetch_all_memories(self.url, self.token, self.store)
        by_hash = {m["content_hash"]: m for m in memories}
        known = self.known_pairs()
        proposals_written = 0
        for mem in memories:
            for hit in self.search_similar(mem["content"]):
                other = hit.get("memory", {})
                other_hash = other.get("content_hash", "")
                score = hit.get("similarity_score", 0.0) or 0.0
                if not other_hash or other_hash == mem["content_hash"]:
                    continue
                if score < SIM_THRESHOLD:
                    continue
                pair = self._pair_key(mem["content_hash"], other_hash)
                if pair in known:
                    continue
                keep, dup = mem, by_hash.get(other_hash, other)
                # created_at 晚者为删除候选；取不到时间则按 hash 序保守跳过
                if (keep.get("created_at_iso") or "") > (dup.get("created_at_iso") or ""):
                    keep, dup = dup, keep
                if not keep.get("created_at_iso") or not dup.get("created_at_iso"):
                    continue
                self.write_proposal(keep, dup, score)
                known.add(pair)
                proposals_written += 1
        self.audit("scan", memories=len(memories),
                   proposals_written=proposals_written,
                   threshold=SIM_THRESHOLD)
        print(f"scan: {len(memories)} memories, "
              f"{proposals_written} new proposals "
              f"(threshold {SIM_THRESHOLD}, {len(known)} pairs known)")
        return 0

    # ---------- distill ----------
    STATE_FILE = "butler-state.json"

    def _load_state(self) -> dict:
        p = self.home / self.STATE_FILE
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                return {}
        return {}

    def _save_state(self, state: dict) -> None:
        (self.home / self.STATE_FILE).write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    def _distill_prompt(self, memories: list[dict]) -> tuple[str, str]:
        items = [{"hash": m["content_hash"], "content": m["content"],
                  "tags": m.get("tags") or [],
                  "created": m.get("created_at_iso")} for m in memories]
        system = ("你是 Mnevo 记忆管家的蒸馏器。只提案不执行，不改写事实，不丢指针。"
                  "输出必须是纯 JSON，不要输出 JSON 以外的任何文字。")
        user = (
            "以下是工作记忆库新增条目（hash 为唯一标识）：\n"
            + json.dumps(items, ensure_ascii=False, indent=1)
            + "\n\n请提案（拿不准就不提，宁缺毋滥）：\n"
            "- merges：多条是同一事实的重复/变体 → 合成一条，保留全部事实细节；\n"
            "- distills：多条相关经历 → 蒸馏为一条更高层结论（保留日期/结论/位置指针，可弃过程细节）；\n"
            "- archives：过时/已被取代/一次性演示且无未来价值 → 建议归档（注意：归档即从库中删除，提案必须慎重）；\n"
            '\n输出 schema（无提案则全空数组）：\n'
            '{"merges":[{"hashes":["..."],"content":"合并后完整中文文本"}],'
            '"distills":[{"hashes":["..."],"content":"蒸馏后完整中文文本","note":"一句话依据"}],'
            '"archives":[{"hash":"...","reason":"..."}]}')
        return system, user

    def write_distill_proposal(self, proposals: dict, source: list[dict]) -> Path:
        path = self.proposals / (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{DISTILL_KIND}.md")
        actions = [{"kind": "merge", "hashes": it["hashes"], "content": it["content"]}
                   for it in proposals["merges"]]
        actions += [{"kind": "distill", "hashes": it["hashes"], "content": it["content"]}
                    for it in proposals["distills"]]
        actions += [{"kind": "archive", "hashes": [it["hash"]], "content": "",
                     "reason": it.get("reason", "")} for it in proposals["archives"]]
        by_hash = {m["content_hash"]: m for m in source}
        lines = [
            "---",
            f"type: {DISTILL_KIND}",
            f"created: {_now_iso()}",
            "status: open",
            "action: apply-json",
            f"model: {os.environ.get('MNEVO_LLM_MODEL', DEFAULT_LLM_MODEL)}",
            f"merges: {len(proposals['merges'])}  "
            f"distills: {len(proposals['distills'])}  "
            f"archives: {len(proposals['archives'])}",
            "decided_by: ''",
            "---",
            "",
            "# 蒸馏提案（LLM 生成，人工确认后执行）",
            "",
        ]
        for label, group in (("合并 merges", proposals["merges"]),
                             ("蒸馏 distills", proposals["distills"]),
                             ("归档 archives", proposals["archives"])):
            lines += [f"## {label}（{len(group)}）", ""]
            for i, it in enumerate(group, 1):
                if "hashes" in it:
                    lines.append(f"### {i}. 影响 {len(it['hashes'])} 条")
                    for h in it["hashes"]:
                        m = by_hash.get(h)
                        lines += [f"- `{_short(h)}`：", "",
                                  "```text",
                                  (m["content"] if m else "（不在本轮源列表中，可能是旧提案的 hash）"),
                                  "```", ""]
                    lines += ["**新内容**：", "", "```text", it["content"], "```", ""]
                    if it.get("note"):
                        lines.append(f"依据：{it['note']}")
                else:
                    m = by_hash.get(it.get("hash", ""))
                    lines += [f"### {i}. 归档 `{_short(it.get('hash', ''))}`",
                              f"理由：{it.get('reason', '')}", "",
                              "```text",
                              (m["content"] if m else "（hash 未解析到内容）"),
                              "```", ""]
            lines.append("")
        lines += ["确认：`review --decide <id> accept` 将按下方 json 逐条执行"
                  "（merge/distill=写入新内容后删除原条；archive=删除）。"
                  "原条全文已保留在本提案与审计日志中。", "",
                  "```json",
                  json.dumps({"actions": actions}, ensure_ascii=False, indent=1),
                  "```", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def distill(self, all_memories: bool = False) -> int:
        api_key = os.environ.get("MNEVO_LLM_API_KEY", "")
        if not api_key:
            self.audit("distill", skipped="no_llm_key")
            print("distill: skipped (MNEVO_LLM_API_KEY not set)")
            return 0
        base = os.environ.get("MNEVO_LLM_BASE_URL", DEFAULT_LLM_BASE)
        model = os.environ.get("MNEVO_LLM_MODEL", DEFAULT_LLM_MODEL)
        last = self._load_state().get("last_distill_iso", "")
        memories = [m for m in fetch_all_memories(self.url, self.token, self.store)
                    if all_memories or (m.get("created_at_iso") or "") > last]
        if len(memories) < MIN_DISTILL_INPUT:
            self.audit("distill", skipped="too_few_new",
                       new=len(memories), since=last or "(first run)")
            print(f"distill: skipped (only {len(memories)} new memories "
                  f"since {last or 'first run'}, need {MIN_DISTILL_INPUT})")
            return 0
        system, user = self._distill_prompt(memories)
        try:
            raw = llm_chat(base, api_key, model, system, user)
            proposals = _parse_llm_json(raw)
        except (RuntimeError, ValueError) as exc:
            self.audit("distill", error=str(exc)[:300])
            print(f"distill: error: {exc}", file=sys.stderr)
            return 1
        counts = {k: len(v) for k, v in proposals.items()}
        if not any(counts.values()):
            self._save_state({**self._load_state(), "last_distill_iso": _now_iso()})
            self.audit("distill", **counts, input=len(memories), result="none")
            print(f"distill: {len(memories)} memories reviewed, nothing proposed")
            return 0
        path = self.write_distill_proposal(proposals, memories)
        self._save_state({**self._load_state(), "last_distill_iso": _now_iso()})
        self.audit("distill", **counts, input=len(memories), proposal=path.name)
        print(f"distill: {len(memories)} memories -> {path.name} "
              f"(merges={counts['merges']} distills={counts['distills']} "
              f"archives={counts['archives']})")
        return 0

    # ---------- review ----------
    def find_proposal(self, prefix: str) -> Path:
        hits = [p for p in self.proposals.glob(f"*{prefix}*.md")]
        if len(hits) != 1:
            raise SystemExit(f"proposal id {prefix!r}: {len(hits)} matches "
                             "(need unique prefix)")
        return hits[0]

    def review_list(self) -> int:
        rows = [p for p in sorted(self.proposals.glob("*.md"))
                if "status: open" in p.read_text(encoding="utf-8")[:400]]
        if not rows:
            print("no open proposals")
            return 0
        for p in rows:
            head = p.read_text(encoding="utf-8").splitlines()[:12]
            meta = {k: v for k, v in
                    (re.match(r"^(\w+):\s*(.*)$", l).groups()
                     for l in head if re.match(r"^\w+:\s", l))}
            print(f"{p.name[:26]}  score={meta.get('score', '?'):>6}  "
                  f"keep={meta.get('keep', '?')[:12]}  delete={meta.get('delete', '?')[:12]}")
        return 0

    def review_show(self, prefix: str) -> int:
        print(self.find_proposal(prefix).read_text(encoding="utf-8"))
        return 0

    def _apply_actions(self, text: str, proposal_name: str) -> tuple[int, int]:
        """执行 apply-json 蒸馏提案；返回 (applied, failed)。失败保留提案可重试。"""
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
        if not m:
            print("apply-json proposal has no json block", file=sys.stderr)
            return 0, 1
        actions = json.loads(m.group(1))["actions"]
        by_hash = {x["content_hash"]: x for x in
                   fetch_all_memories(self.url, self.token, self.store)}
        applied = failed = 0
        for act in actions:
            kind, hashes = act["kind"], act.get("hashes") or []
            if kind in ("merge", "distill"):
                tags = sorted({t for h in hashes if h in by_hash
                               for t in (by_hash[h].get("tags") or [])} | {"distilled"})
                outcome, note = post_memory(
                    self.url, self.token,
                    {"content": act["content"], "tags": tags})
                if outcome == "rejected":
                    self.audit("apply-error", proposal=proposal_name,
                               kind=kind, note=note)
                    failed += 1
                    continue
                self.audit("apply-store", proposal=proposal_name,
                           kind=kind, outcome=outcome)
            elif kind != "archive":
                self.audit("apply-error", proposal=proposal_name,
                           kind=str(kind), note="unknown kind")
                failed += 1
                continue
            for h in hashes:
                if h not in by_hash:
                    continue  # 已删（幂等重试）
                status, body = http_json(
                    f"{self.url}/api/memories/{h}", self.token, method="DELETE")
                if status in (200, 202, 204):
                    applied += 1
                else:
                    failed += 1
                    self.audit("apply-error", proposal=proposal_name, hash=h,
                               note=f"HTTP {status}: {str(body)[:120]}")
        return applied, failed

    def review_decide(self, prefix: str, decision: str) -> int:
        path = self.find_proposal(prefix)
        text = path.read_text(encoding="utf-8")
        if "status: open" not in text[:400]:
            print(f"already decided: {path.name}")
            return 1

        if decision == "accept":
            if "action: apply-json" in text[:400]:
                applied, failed = self._apply_actions(text, path.name)
                if failed:
                    print(f"{path.name}: {failed} action(s) FAILED — "
                          "proposal left open for retry", file=sys.stderr)
                    return 2
                self.audit("apply-done", proposal=path.name, applied=applied)
            else:
                m = re.search(r"^delete:\s*(\S+)", text, re.M)
                full_hash = None
                if m:  # 从提案行取 12 位前缀，再回全库对照
                    short = m.group(1).split()[0]
                    memories = fetch_all_memories(self.url, self.token, self.store)
                    full = [x for x in memories
                            if x["content_hash"].startswith(short)]
                    full_hash = full[0]["content_hash"] if len(full) == 1 else None
                if not full_hash:
                    print("cannot resolve delete target hash — refusing",
                          file=sys.stderr)
                    return 2
                status, body = http_json(
                    f"{self.url}/api/memories/{full_hash}", self.token,
                    method="DELETE")
                if status not in (200, 202, 204):
                    print(f"delete -> HTTP {status}: {str(body)[:120]}",
                          file=sys.stderr)
                    return 2
                self.audit("delete-duplicate", proposal=path.name, hash=full_hash)
        new_text = text.replace("status: open", f"status: {decision}", 1)
        new_text = new_text.replace("decided_by: ''",
                                    f"decided_by: human {decision}", 1)
        path.write_text(new_text, encoding="utf-8")
        self.audit("review", proposal=path.name, decision=decision)
        print(f"{path.name}: {decision}")
        return 0

    def audit_tail(self, n: int) -> int:
        if not self.audit_path.is_file():
            print("(no audit entries yet)")
            return 0
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        for line in lines[-n:]:
            print(line)
        return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS)
    common.add_argument("--token-env", default=argparse.SUPPRESS)
    common.add_argument("--store", default=argparse.SUPPRESS)
    common.add_argument("--home", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog="mnevo_butler", parents=[common],
                                     description="Mnevo butler v0.1 (pipeline + review)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", parents=[common], help="扫描疑似重复，写提案")
    p_distill = sub.add_parser("distill", parents=[common],
                               help="LLM 蒸馏提案（新记忆不足则跳过）")
    p_distill.add_argument("--all", action="store_true",
                           help="无视上次蒸馏时间，全库提案")
    p_audit = sub.add_parser("audit", parents=[common], help="审计日志")
    p_review = sub.add_parser("review", parents=[common], help="提案确认面")

    p_audit.add_argument("--tail", type=int, default=20)
    r = p_review
    r.add_argument("--list", action="store_true")
    r.add_argument("--show", metavar="ID_PREFIX")
    r.add_argument("--decide", nargs=2, metavar=("ID_PREFIX", "accept|dismiss"))

    args = parser.parse_args(argv)
    for name, dv in (("url", "http://127.0.0.1:8000"),
                     ("token_env", "MNEVO_WORK_TOKEN"),
                     ("store", "default"),
                     ("home", os.path.expanduser("~/.mnevo"))):
        setattr(args, name, getattr(args, name, dv))
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"error: env {args.token_env} not set", file=sys.stderr)
        return 2
    butler = Butler(args.url, token, args.store, Path(args.home))

    if args.command == "scan":
        return butler.scan()
    if args.command == "distill":
        return butler.distill(all_memories=getattr(args, "all", False))
    if args.command == "audit":
        return butler.audit_tail(args.tail)
    if args.command == "review":
        if args.list:
            return butler.review_list()
        if args.show:
            return butler.review_show(args.show)
        if args.decide:
            prefix, decision = args.decide
            if decision not in ("accept", "dismiss"):
                print("decision must be accept|dismiss", file=sys.stderr)
                return 2
            return butler.review_decide(prefix, decision)
        r.print_help()
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
