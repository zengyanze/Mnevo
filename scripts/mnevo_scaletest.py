#!/usr/bin/env python3
"""Mnevo 规模化复测工具（PLAN §12 v0.2-6）：合成数据 → 检索质量 + 判重阈值校准.

引擎无关：走 mcp-memory-service 兼容 REST 面（引擎 A/B 通用——可插拔的直接
收益）。建议在隔离/可丢弃引擎上跑（如 engine B mem0）；`cleanup` 按 hash 清场。

用法::

    mnevo_scaletest.py generate --n 300 --out scaletest.json
    mnevo_scaletest.py load --in scaletest.json --url http://127.0.0.1:8100
    mnevo_scaletest.py probe-retrieval --in scaletest.json --url ...
    mnevo_scaletest.py probe-dedup --in scaletest.json --url ...
    mnevo_scaletest.py cleanup --in scaletest.json --url ...

生成物（单 JSON，自包含）：items（含 content_hash，引擎 A 同款 sha256(strip+lower)）、
pairs（分级近重复对 L1 轻改/L2 同义改写/L3 相关不同事）、probes（检索探针：
query+期望 hash）。全部 tags=["scaletest"]，与真实记忆可区分。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mnevo_pack import fetch_engine_hashes, http_json, post_memory  # noqa: E402

TAG = "scaletest"
PROJECTS = ("凤凰", "青梧", "磐石", "流沙", "山岚", "远帆", "晨雾", "星轨",
            "沧澜", "琥珀", "银线", "回声")
MATTERS = ("联调结论", "踩坑记录", "部署要点", "对拍口径", "权限方案", "数据迁移",
           "性能基线", "回滚预案", "告警治理", "接口对齐", "配置管理", "容量评估",
           "灰度策略", "日志规范", "验收标准", "成本核算", "安全审查", "依赖升级",
           "压测结论", "灾备演练", "命名约定", "文档缺口", "复盘要点", "监控盲区",
           "发布节奏", "缓存策略", "分页口径", "错误码治理")
ASPECTS = ("三环境口径对齐，遗留两项在跟", "根因是版本错配，已回滚修复",
           "方案 A 定稿，备选 B 留档", "上游未就绪，先走降级路径",
           "指标达标但尾延迟仍需优化", "演练通过，恢复时间在预算内",
           "灰度 10% 观察 48h 后全量", "旧链路下线前保留一周观察期")


def content_hash(content: str) -> str:
    """引擎 A/B 统一口径：sha256(content.strip().lower())。"""
    return hashlib.sha256(content.strip().lower().encode("utf-8")).hexdigest()


def _mk_item(item_id: int, content: str, kind: str = "base",
             pair_level: str | None = None) -> dict:
    return {"id": item_id, "content": content,
            "content_hash": content_hash(content),
            "tags": [TAG], "kind": kind, "pair_level": pair_level}


def generate_dataset(n: int = 300, seed: int = 42,
                     pairs_per_level: int = 8, probes: int = 60) -> dict:
    """确定性生成：同 seed → 逐字节同数据集。"""
    rng = random.Random(seed)
    combos = [(p, m) for p in PROJECTS for m in MATTERS]
    rng.shuffle(combos)
    items, seen = [], set()

    def _add(content: str, kind: str = "base", level: str | None = None):
        if content in seen:
            return None
        seen.add(content)
        it = _mk_item(len(items) + 1, content, kind, level)
        items.append(it)
        return it

    for project, matter in combos:
        if len(items) >= n - pairs_per_level * 6:
            break
        date = f"2026-1{rng.randrange(0, 2)}-{rng.randrange(10, 29):02d}"
        i = len(items) + 1
        _add(f"{project}项目{matter}（{date}）：{matter}已收口——"
             f"{rng.choice(ASPECTS)}；跟进清单 REF-{i:04d}")

    # 分级近重复对：L1 轻改（应判重）/ L2 同义改写（灰带）/ L3 相关不同事（不应判重）
    # v2 教训（2026-10-28）：语料必须避免跨条目共享长模板句——嵌入会被共享
    # 后缀主导，L3 与 L1 不可分；每条带唯一 REF + 事项化详情，L3=同项目不同事项。
    pairs = []
    proj_of = lambda c: c.split("项目", 1)[0]
    pair_defs = {
        "L1": lambda b: b + "；补充：结论已同步到周报",
        "L2": lambda b: (b.split("（", 1)[1].split("）", 1)[0] + "收口记录，"
                        + proj_of(b) + "项目" + b.split("项目", 1)[1].split("（")[0]
                        + "：" + b.split("——", 1)[1]),
        "L3": lambda b: f"{proj_of(b)}项目{rng.choice(MATTERS)}"
                        f"（2026-10-{rng.randrange(10, 29):02d}）：另案待定——"
                        f"{rng.choice(ASPECTS)}；跟进清单 REF-{9000 + len(items):04d}",
    }
    bases = [it for it in items if it["kind"] == "base"]
    for level, mutate in pair_defs.items():
        for base in rng.sample(bases, pairs_per_level):
            twin = _add(mutate(base["content"]), "dup", level)
            if twin:
                pairs.append({"level": level, "a": base["content_hash"],
                              "b": twin["content_hash"]})

    probe_items = [it for it in items if it["kind"] == "base"]
    rng.shuffle(probe_items)
    probe_set = []
    for it in probe_items[:probes]:
        head = it["content"].split("项目", 1)[0]
        matter = it["content"].split("项目", 1)[1][:4]
        probe_set.append({"query": f"{head} {matter}",
                          "expected": it["content_hash"]})
    return {"seed": seed, "n": len(items), "items": items, "pairs": pairs,
            "probes": probe_set}


def _engine(url: str, token: str):
    base = url.rstrip("/")

    def search(query: str, limit: int = 5) -> list[dict]:
        status, body = http_json(f"{base}/api/search", token, method="POST",
                                 body={"query": query, "limit": limit}, timeout=60)
        if status != 200:
            raise RuntimeError(f"search HTTP {status}: {str(body)[:120]}")
        return body.get("results", [])

    def hashes() -> set[str]:
        return fetch_engine_hashes(base, token)

    return search, hashes


def retrieval_metrics(results: list[dict], expected: str) -> dict:
    """hit@1/3/5 + MRR + 首名得分（results 为排好序的检索结果）。"""
    scores = [((r.get("memory") or {}).get("content_hash", ""),
               r.get("similarity_score", 0.0)) for r in results]
    ranks = [i + 1 for i, (h, _) in enumerate(scores) if h == expected]
    rank = ranks[0] if ranks else None
    return {"hit1": rank == 1, "hit3": rank is not None and rank <= 3,
            "hit5": rank is not None and rank <= 5,
            "rr": 1.0 / rank if rank else 0.0,
            "top_score": scores[0][1] if scores else 0.0}


def calibration_table(pair_scores: dict[str, list[float]],
                      noise_scores: list[float]) -> str:
    """分级得分分布 + 噪声底 → 建议阈值带（人工裁决输入，不自动定阈）。"""
    def stats(xs):
        if not xs:
            return "—"
        xs = sorted(xs)
        q = xs[min(len(xs) - 1, int(0.5 * len(xs)))]
        return f"n={len(xs):2d} min={xs[0]:.3f} p50={q:.3f} max={xs[-1]:.3f}"

    lines = ["判重阈值校准（similarity 分布）：",
             f"  L1 轻改（应判重）    {stats(pair_scores.get('L1', []))}",
             f"  L2 同义改写（灰带）  {stats(pair_scores.get('L2', []))}",
             f"  L3 相关不同事（不应） {stats(pair_scores.get('L3', []))}",
             f"  噪声底（非对最高分）  {stats(noise_scores)}"]
    l2 = sorted(pair_scores.get("L2", []))
    if l2:
        lines.append(f"  现行阈值 0.90 下灰带命中 "
                     f"{sum(1 for s in l2 if s >= 0.90)}/{len(l2)}"
                     "（灰带漏网属预期：宁缺毋滥）")
    return "\n".join(lines)


def cmd_generate(args):
    data = generate_dataset(args.n, args.seed, args.pairs_per_level, args.probes)
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    levels: dict[str, int] = {}
    for pr in data["pairs"]:
        levels[pr["level"]] = levels.get(pr["level"], 0) + 1
    print(f"generated: {data['n']} items ({len(data['probes'])} probes, "
          f"pairs={levels}) -> {args.out}")
    return 0


def cmd_load(args):
    data = json.loads(Path(args.in_).read_text(encoding="utf-8"))
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"error: env {args.token_env} not set", file=sys.stderr)
        return 2
    existing = _engine(args.url, token)[1]()
    tally, t0 = {}, time.time()
    for it in data["items"]:
        if it["content_hash"] in existing:
            tally["skip"] = tally.get("skip", 0) + 1
            continue
        outcome, _ = post_memory(args.url, token, it)
        tally[outcome] = tally.get(outcome, 0) + 1
    dt = time.time() - t0
    print(f"load: {json.dumps(tally, ensure_ascii=False)} in {dt:.1f}s "
          f"({data['n'] / max(dt, 0.01):.1f} items/s)")
    return 1 if tally.get("rejected") else 0


def cmd_probe_retrieval(args):
    data = json.loads(Path(args.in_).read_text(encoding="utf-8"))
    token = os.environ.get(args.token_env, "")
    search, _ = _engine(args.url, token)
    rows, times = [], []
    for pr in data["probes"]:
        t0 = time.time()
        results = search(pr["query"], limit=5)
        times.append(time.time() - t0)
        rows.append(retrieval_metrics(results, pr["expected"]))
    n = len(rows) or 1
    times.sort()
    print(f"retrieval: n={n} hit@1={sum(r['hit1'] for r in rows) / n:.2f} "
          f"hit@3={sum(r['hit3'] for r in rows) / n:.2f} "
          f"hit@5={sum(r['hit5'] for r in rows) / n:.2f} "
          f"MRR={sum(r['rr'] for r in rows) / n:.3f}")
    print(f"latency: p50={times[len(times) // 2] * 1000:.0f}ms "
          f"p95={times[int(len(times) * 0.95)] * 1000:.0f}ms")
    return 0


def cmd_probe_dedup(args):
    data = json.loads(Path(args.in_).read_text(encoding="utf-8"))
    token = os.environ.get(args.token_env, "")
    search, _ = _engine(args.url, token)
    by_hash = {it["content_hash"]: it for it in data["items"]}
    pair_scores: dict[str, list[float]] = {"L1": [], "L2": [], "L3": []}
    noise: list[float] = []
    for pair in data["pairs"]:
        results = search(by_hash[pair["a"]]["content"], limit=10)
        hit = next((r.get("similarity_score", 0.0) for r in results
                    if (r.get("memory") or {}).get("content_hash") == pair["b"]),
                   None)
        if hit is not None:
            pair_scores[pair["level"]].append(hit)
        for r in results:
            h = (r.get("memory") or {}).get("content_hash", "")
            if h not in (pair["a"], pair["b"]):  # 自匹配（查询句自身）不入噪声
                noise.append(r.get("similarity_score", 0.0))
    print(calibration_table(pair_scores, noise))
    return 0


def cmd_cleanup(args):
    data = json.loads(Path(args.in_).read_text(encoding="utf-8"))
    token = os.environ.get(args.token_env, "")
    base = args.url.rstrip("/")
    ok = failed = 0
    for it in data["items"]:
        status, body = http_json(f"{base}/api/memories/{it['content_hash']}",
                                 token, method="DELETE", timeout=30)
        if status in (200, 202, 204, 404):
            ok += 1
        else:
            failed += 1
    left = len(_engine(args.url, token)[1]()
               & {it["content_hash"] for it in data["items"]})
    print(f"cleanup: {ok} deleted, {failed} failed, {left} left（404=幂等）")
    return 0 if failed == 0 and left == 0 else 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="mnevo_scaletest",
                                description="规模化复测：合成数据+检索质量+阈值校准")
    sub = p.add_subparsers(dest="command", required=True)
    g = sub.add_parser("generate", help="生成确定性合成数据集")
    g.add_argument("--n", type=int, default=300)
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--pairs-per-level", type=int, default=8)
    g.add_argument("--probes", type=int, default=60)
    g.add_argument("--out", required=True)
    g.set_defaults(func=cmd_generate)
    for name, fn, help_ in (("load", cmd_load, "灌库（幂等，已存在跳过）"),
                            ("probe-retrieval", cmd_probe_retrieval,
                             "检索质量 hit@k/MRR"),
                            ("probe-dedup", cmd_probe_dedup,
                             "近重复对得分分布+阈值带"),
                            ("cleanup", cmd_cleanup, "按 hash 清场")):
        c = sub.add_parser(name, help=help_)
        c.add_argument("--in", dest="in_", required=True)
        c.add_argument("--url", default="http://127.0.0.1:8100")
        c.add_argument("--token-env", default="MNEVO_WORK_TOKEN")
        c.set_defaults(func=fn)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
