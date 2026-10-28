#!/usr/bin/env python3
"""Mnevo 冷启动注入器 —— 身份层四件 → 确定性常驻上下文块（PLAN §4.1/§5.1）.

身份层 = 常驻注入：persona → values → goals → threads 固定顺序（身份层 →
活跃线程），输出不含时间戳/随机数——同样输入必得同样输出，不打爆下游
prompt cache（§5.1 确定性排序）。身份层内容永远人工确认（机器只有提案权），
本工具只做装配，不改写一个字。

用法::

    python scripts/mnevo_bootstrap.py build --identity identity/live [--budget 2400]
    python scripts/mnevo_bootstrap.py build --format json            # 机器可读
    python scripts/mnevo_bootstrap.py check --identity identity/live # 体检（缺件=非零）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SECTION_ORDER = ("persona.md", "values.md", "goals.md", "threads.md")
SECTION_TITLES = {"persona.md": "Persona · 我是谁/沟通风格",
                  "values.md": "Values · 决策偏好与哲学",
                  "goals.md": "Goals · 目标（季度粒度）",
                  "threads.md": "Active Threads · 活跃线程"}
DEFAULT_BUDGET = 2400     # 字符预算（中文为主，≈1.5k~2k token 量级）
MIN_SECTION_BUDGET = 200
DEFAULT_STALE_DAYS = 14   # threads 超过此天数未更新 → 注入过时提示


def read_identity_files(identity_dir: Path) -> dict[str, str | None]:
    """读四件；缺失记 None（装配不阻塞会话，缺失档留占位）。"""
    out: dict[str, str | None] = {}
    for name in SECTION_ORDER:
        p = identity_dir / name
        out[name] = p.read_text(encoding="utf-8").strip() if p.is_file() else None
    return out


def threads_stale(identity_dir: Path, stale_days: int) -> bool:
    p = identity_dir / "threads.md"
    if not p.is_file():
        return False
    return (time.time() - p.stat().st_mtime) > stale_days * 86400


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """行边界截断（不切半行）；返回 (文本, 是否截断)。"""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut.rstrip(), True


def build_profile(files: dict[str, str | None], budget: int = DEFAULT_BUDGET,
                  stale_threads: bool = False) -> str:
    """装配确定性注入块。同样输入+同样的缺失/过时状态 → 逐字节相同输出。"""
    per = max(MIN_SECTION_BUDGET, budget // len(SECTION_ORDER))
    lines = ["# Mnevo 身份层（常驻注入快照 · 内容一律人工确认）", ""]
    for name in SECTION_ORDER:
        lines.append(f"## {SECTION_TITLES[name]}")
        text = files.get(name)
        if text is None:
            lines.append(f"（缺失：{name}——见 Mnevo/identity/）")
        else:
            body, cut = _truncate(text, per)
            lines.append(body)
            if cut:
                lines.append(f"…（{name} 超预算截断，全文见源文件）")
            if name == "threads.md" and stale_threads:
                lines.append("（注意：活跃线程较久未更新，可能过时——以当前事实为准）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_build(args: argparse.Namespace) -> int:
    identity_dir = Path(args.identity)
    if not identity_dir.is_dir():
        print(f"error: identity dir not found: {identity_dir}", file=sys.stderr)
        return 2
    files = read_identity_files(identity_dir)
    stale = threads_stale(identity_dir, args.stale_days)
    missing = [n for n, v in files.items() if v is None]
    if missing:
        print(f"warn: missing identity files: {', '.join(missing)}",
              file=sys.stderr)
    if args.format == "json":
        payload = {"files": files, "stale_threads": stale}
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        profile = build_profile(files, args.budget, stale)
        print(profile, end="")
        print(f"--- profile: {len(profile)} chars "
              f"(budget {args.budget}, ~{int(len(profile) * 0.7)} tokens est.)",
              file=sys.stderr)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    identity_dir = Path(args.identity)
    files = read_identity_files(identity_dir)
    ok = True
    for name in SECTION_ORDER:
        text = files.get(name)
        if text is None:
            print(f"MISSING  {name}")
            ok = False
        else:
            stale = name == "threads.md" and threads_stale(
                identity_dir, args.stale_days)
            print(f"{'STALE ' if stale else 'OK    '}  {name}  {len(text)} chars")
            if stale and args.strict:
                ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="mnevo_bootstrap", description="身份层冷启动注入器（确定性装配）")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--identity", default="identity/live",
                        help="身份层目录（默认 identity/live）")
    common.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                        help="threads 过时阈值天数（默认 14）")

    p_build = sub.add_parser("build", parents=[common],
                             help="装配常驻注入块（stdout）")
    p_build.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                         help="总字符预算（默认 2400）")
    p_build.add_argument("--format", choices=("md", "json"), default="md")
    p_build.set_defaults(func=cmd_build)

    p_check = sub.add_parser("check", parents=[common],
                             help="体检：缺件/过时报告")
    p_check.add_argument("--strict", action="store_true",
                         help="threads 过时也算失败（默认只警告）")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
