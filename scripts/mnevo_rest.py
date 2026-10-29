#!/usr/bin/env python3
"""Mnevo agent REST 助手 —— 给看不到 MCP 工具的 agent（Codex 等）的一页 CLI.

与 pi 的 MCP 路线同语义（adapters/pi.md ↔ adapters/codex.md）：store 带
`agent:<名>` 标签、search/recent 供召回。长文本走 stdin（免命令行引号地狱，
Windows 尤其）。客户端侧 secrets 闸门：命中明显密钥模式直接拒存（--force 才放行，
引擎服务端还有第二道）。

用法::

    echo "经历内容……" | python scripts/mnevo_rest.py store --tags work,mnevo
    python scripts/mnevo_rest.py store --content "一条短记忆" --tags work,mnevo
    python scripts/mnevo_rest.py search "上次进展" --limit 3
    python scripts/mnevo_rest.py recent --n 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mnevo_pack import http_json, post_memory_full  # noqa: E402

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"age1[0-9a-z]{40,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-.]{20,}"),
    re.compile(r"(?i)passw(or)?d\s*[=:]\s*\S{6,}"),
    re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S{10,}"),
)


def secret_hits(content: str) -> list[str]:
    return [p.pattern for p in SECRET_PATTERNS if p.search(content)]


def _read_content(args) -> str:
    if args.content is not None:
        return args.content
    if not sys.stdin.isatty():
        raw = sys.stdin.buffer.read()          # Windows 管道编码不可信，读原始字节
        if raw[:3] == b"\xef\xbb\xbf":         # 剥 UTF-8 BOM
            raw = raw[3:]
        text = ""
        for enc in ("utf-8", "gbk"):           # PS7 默认 utf8NoBOM；老壳按 GBK
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
        if text.strip():
            return text.strip()
    raise SystemExit("error: 内容为空——用 --content 或管道/stdin 传入")


def cmd_store(args) -> int:
    content = _read_content(args)
    hits = secret_hits(content)
    if hits and not args.force:
        print("拒存：内容疑似包含密钥/凭据（命中 "
              f"{len(hits)} 个模式）。确认无误请加 --force", file=sys.stderr)
        return 2
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    if not any(t.startswith("agent:") for t in tags):
        tags.append(f"agent:{args.agent}")
    outcome, note, resp = post_memory_full(
        args.url, os.environ.get(args.token_env, ""),
        {"content": content, "tags": tags})
    h = ""
    if isinstance(resp, dict):
        h = resp.get("content_hash") or ""
    print(f"store: {outcome}" + (f" {h[:12]}…" if h else "")
          + (f" | {note[:80]}" if outcome != "created" else ""))
    return 0 if outcome == "created" else (1 if outcome == "rejected" else 0)


def cmd_search(args) -> int:
    query = args.query or args.query_pos
    if not query:
        raise SystemExit("error: 缺查询词——mnevo_rest.py search <词> 或 --query <词>")
    status, body = http_json(f"{args.url.rstrip('/')}/api/search",
                             os.environ.get(args.token_env, ""), method="POST",
                             body={"query": query, "limit": args.limit},
                             timeout=60)
    if status != 200:
        print(f"search HTTP {status}: {str(body)[:120]}", file=sys.stderr)
        return 1
    results = body.get("results", []) if isinstance(body, dict) else []
    if not results:
        print("(no results)")
        return 0
    for r in results:
        mem = r.get("memory") or {}
        tags = ",".join(mem.get("tags") or [])
        print(f"[{r.get('similarity_score', 0):.3f}] "
              f"{(mem.get('created_at_iso') or '')[:10]} [{tags}]")
        print(f"  {mem.get('content', '')[:120]}")
    return 0


def cmd_recent(args) -> int:
    url = (f"{args.url.rstrip('/')}/api/memories"
           f"?page=1&page_size={args.n}")
    status, body = http_json(url, os.environ.get(args.token_env, ""), timeout=60)
    if status != 200:
        print(f"recent HTTP {status}: {str(body)[:120]}", file=sys.stderr)
        return 1
    memories = body.get("memories", []) if isinstance(body, dict) else []
    for m in memories:
        tags = ",".join(m.get("tags") or [])
        print(f"{(m.get('created_at_iso') or '')[:10]} [{tags}]")
        print(f"  {m.get('content', '')[:120]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="mnevo_rest",
                                description="Mnevo agent REST 助手（store/search/recent）")
    sub = p.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default="http://127.0.0.1:8000")
    common.add_argument("--token-env", default="MNEVO_WORK_TOKEN")

    s = sub.add_parser("store", parents=[common], help="写一条经历记忆（长文走 stdin）")
    s.add_argument("--content", help="短内容直接传；长内容用管道/stdin")
    s.add_argument("--tags", default="", help="逗号分隔，如 work,mnevo")
    s.add_argument("--agent", default="codex", help="agent 标签（默认 codex）")
    s.add_argument("--force", action="store_true", help="跳过客户端 secrets 闸门")
    s.set_defaults(func=cmd_store)

    q = sub.add_parser("search", parents=[common], help="语义召回：search <词>")
    q.add_argument("query_pos", nargs="?", help="查询词（位置参数优先）")
    q.add_argument("--query", dest="query", help="查询词（旗标形式）")
    q.add_argument("--limit", type=int, default=5)
    q.set_defaults(func=cmd_search)

    r = sub.add_parser("recent", parents=[common], help="最近记忆（核对记了什么）")
    r.add_argument("--n", type=int, default=5)
    r.set_defaults(func=cmd_recent)

    args = p.parse_args(argv)
    if not os.environ.get(args.token_env, ""):
        print(f"error: env {args.token_env} not set", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
