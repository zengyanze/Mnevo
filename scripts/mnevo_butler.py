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
from mnevo_pack import fetch_all_memories, http_json  # noqa: E402

SIM_THRESHOLD = 0.90       # 近重复判定（保守，误报宁可少）
SEARCH_LIMIT = 5
PROPOSAL_KIND = "merge-candidate"


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

    def review_decide(self, prefix: str, decision: str) -> int:
        path = self.find_proposal(prefix)
        text = path.read_text(encoding="utf-8")
        if "status: open" not in text[:400]:
            print(f"already decided: {path.name}")
            return 1
        m = re.search(r"^delete:\s*(\S+)", text, re.M)
        full_hash = None
        if m:  # 从提案行取 12 位前缀，再回全库对照
            short = m.group(1).split()[0]
            memories = fetch_all_memories(self.url, self.token, self.store)
            full = [x for x in memories if x["content_hash"].startswith(short)]
            full_hash = full[0]["content_hash"] if len(full) == 1 else None

        if decision == "accept":
            if not full_hash:
                print("cannot resolve delete target hash — refusing", file=sys.stderr)
                return 2
            status, body = http_json(
                f"{self.url}/api/memories/{full_hash}", self.token, method="DELETE")
            if status not in (200, 202, 204):
                print(f"delete -> HTTP {status}: {str(body)[:120]}", file=sys.stderr)
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
