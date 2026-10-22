#!/usr/bin/env python3
"""Mnevo memory pack v0.1 — export / import / verify for mcp-memory-service.

私有格式（版本化，不宣称标准）——``<name>.mempack/`` 目录::

    manifest.json    格式版本 / 来源引擎 / 条目数 / 每个文件 sha256
    memories.jsonl   每行一条记忆：content/content_hash/tags/memory_type/时间戳
    identity/        可选：身份层已确认文件（persona/values/goals/threads.md）

运输形态 = 目录的 tar.gz（export --tar）。包是"用户拥有"的物理证据：
内容为真源，embedding 等派生物不入包（导入端重算）；恢复语义 = 按内容重存，
引擎按内容 sha256 天然幂等；created_at 以导入时间为准（原始时间戳留在 jsonl 溯源）。

用法::

    python scripts/mnevo_pack.py export  --out PACK_DIR [--identity DIR] [--tar]
    python scripts/mnevo_pack.py import  --pack PACK_DIR [--dry-run]
    python scripts/mnevo_pack.py verify  --pack PACK_DIR [--against-engine]

公共参数：--url（默认 http://127.0.0.1:8000）、--token-env（默认 MNEVO_WORK_TOKEN）。
令牌只从环境变量读，绝不进参数/日志。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FORMAT_VERSION = 1
IDENTITY_NAMES = ("persona.md", "values.md", "goals.md", "threads.md")
PAGE_SIZE = 100
USER_AGENT = "mnevo-pack/0.1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def http_json(url: str, token: str, method: str = "GET", body: dict | None = None,
              timeout: int = 30) -> tuple[int, object]:
    """单次 JSON 请求；返回 (status, parsed_body_or_raw_text)。"""
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也带 JSON body，交给调用方判读
        raw = exc.read()
        status = exc.code
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = raw.decode("utf-8", errors="replace")
    return status, parsed


def fetch_all_memories(base_url: str, token: str, store: str = "default") -> list[dict]:
    """翻页拉全量记忆（GET /api/memories?page&page_size&store）。"""
    memories: list[dict] = []
    page = 1
    while True:
        url = (f"{base_url.rstrip('/')}/api/memories"
               f"?page={page}&page_size={PAGE_SIZE}&store={store}")
        status, body = http_json(url, token)
        if status != 200:
            raise RuntimeError(f"list memories page {page} -> HTTP {status}: {body!r}")
        rows = body.get("memories", []) if isinstance(body, dict) else []
        if not rows:
            break
        memories.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
        page += 1
    return memories


def fetch_engine_hashes(base_url: str, token: str, store: str = "default",
                        limit: int = 1000) -> set[str]:
    """游标拉引擎端 content_hash 全集（GET /api/memories/hashes）。"""
    hashes: set[str] = set()
    cursor = 0
    while True:
        url = (f"{base_url.rstrip('/')}/api/memories/hashes"
               f"?cursor={cursor}&limit={limit}&store={store}")
        status, body = http_json(url, token)
        if status != 200:
            raise RuntimeError(f"list hashes cursor {cursor} -> HTTP {status}: {body!r}")
        batch = []
        if isinstance(body, dict):
            batch = body.get("hashes") or body.get("results") or []
            if batch and isinstance(batch[0], dict):
                batch = [h.get("content_hash") or h.get("hash") for h in batch]
        elif isinstance(body, list):
            batch = body
        if not batch:
            break
        hashes.update(h for h in batch if h)
        if len(batch) < limit:
            break
        cursor += len(batch)
    return hashes


def pack_row(memory: dict) -> dict:
    """引擎记忆 → 可携带行：保内容与谱系时间，弃访问遥测与派生元数据。"""
    return {
        "content": memory.get("content", ""),
        "content_hash": memory.get("content_hash", ""),
        "tags": memory.get("tags") or [],
        "memory_type": memory.get("memory_type"),
        "created_at_iso": memory.get("created_at_iso"),
        "updated_at_iso": memory.get("updated_at_iso"),
    }


def write_pack(out_dir: Path, rows: list[dict], identity_files: list[Path],
               namespace: str, engine_name: str) -> Path:
    """写 mempack 目录；返回目录本身。重复导出 = 覆盖重建。"""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "identity").mkdir(parents=True, exist_ok=True)

    jsonl = "".join(
        json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows
    )
    (out_dir / "memories.jsonl").write_text(jsonl, encoding="utf-8")

    copied_identity: list[str] = []
    for src in identity_files:
        if src.name in IDENTITY_NAMES and src.is_file():
            shutil.copy2(src, out_dir / "identity" / src.name)
            copied_identity.append(src.name)

    manifest = {
        "format": "mnevo.mempack",
        "format_version": FORMAT_VERSION,
        "namespace": namespace,
        "created_at": _now_iso(),
        "engine": {"name": engine_name},
        "memory_count": len(rows),
        "identity_files": copied_identity,
        "files": {
            "memories.jsonl": _sha256_bytes((out_dir / "memories.jsonl").read_bytes()),
            **{
                f"identity/{name}": _sha256_bytes((out_dir / "identity" / name).read_bytes())
                for name in copied_identity
            },
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out_dir


def read_pack(pack_dir: Path) -> tuple[dict, list[dict]]:
    """读包并校验 manifest 完整性；任何文件哈希不符即抛错拒用。"""
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest.json not found in {pack_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "mnevo.mempack":
        raise ValueError(f"unknown pack format: {manifest.get('format')!r}")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"unsupported pack version: {manifest.get('format_version')!r}")

    for rel, expected in manifest.get("files", {}).items():
        path = pack_dir / rel
        if not path.is_file():
            raise FileNotFoundError(f"pack file missing: {rel}")
        actual = _sha256_bytes(path.read_bytes())
        if actual != expected:
            raise ValueError(f"integrity failure: {rel} sha256 mismatch "
                             f"(expected {expected[:12]}…, got {actual[:12]}…)")

    rows = []
    jsonl_path = pack_dir / "memories.jsonl"
    if jsonl_path.is_file():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return manifest, rows


def post_memory(base_url: str, token: str, row: dict,
                timeout: int = 60) -> tuple[str, str]:
    """重存一条记忆；返回 (结果分类, 说明)。分类 ∈ created/duplicate/rejected。"""
    url = f"{base_url.rstrip('/')}/api/memories"
    status, body = http_json(
        url, token, method="POST",
        body={"content": row["content"], "tags": row.get("tags") or []},
        timeout=timeout,
    )
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    if status in (200, 201):
        return "created", text[:200]
    if status in (409, 422) or "exist" in text.lower() or "duplicate" in text.lower():
        return "duplicate", text[:200]
    return "rejected", f"HTTP {status}: {text[:200]}"


def cmd_export(args: argparse.Namespace) -> int:
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"error: env {args.token_env} not set", file=sys.stderr)
        return 2
    rows = [pack_row(m) for m in fetch_all_memories(args.url, token, args.store)]
    identity_dir = Path(args.identity) if args.identity else None
    identity_files = sorted(identity_dir.glob("*.md")) if identity_dir else []
    identity_files = [p for p in identity_files if p.name in IDENTITY_NAMES]

    out = write_pack(Path(args.out), rows, identity_files, args.namespace, args.engine)
    print(f"exported {len(rows)} memories -> {out}")
    if identity_files:
        print(f"identity included: {', '.join(p.name for p in identity_files)}")
    if args.tar:
        tar_path = out.with_suffix(".mempack.tar.gz") if out.suffix != ".tar.gz" \
            else out.with_name(out.name + ".tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(out, arcname=out.name)
        print(f"tarball: {tar_path}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    manifest, rows = read_pack(Path(args.pack))
    print(f"pack: {manifest.get('namespace')} @ {manifest.get('created_at')}, "
          f"{len(rows)} memories")
    if args.dry_run:
        for r in rows[:5]:
            print(f"  [dry] {r['content_hash'][:12]}… {r['content'][:40]}")
        if len(rows) > 5:
            print(f"  … and {len(rows) - 5} more")
        return 0
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"error: env {args.token_env} not set", file=sys.stderr)
        return 2
    tally: dict[str, int] = {}
    for r in rows:
        outcome, note = post_memory(args.url, token, r)
        tally[outcome] = tally.get(outcome, 0) + 1
        if outcome == "rejected":
            print(f"  [rejected] {r['content_hash'][:12]}… {note}", file=sys.stderr)
    print(f"import done: {json.dumps(tally, ensure_ascii=False)}")
    return 1 if tally.get("rejected") else 0


def cmd_verify(args: argparse.Namespace) -> int:
    pack_dir = Path(args.pack)
    manifest, rows = read_pack(pack_dir)  # 含完整性校验
    pack_hashes = {r["content_hash"] for r in rows if r.get("content_hash")}
    print(f"manifest OK: {manifest.get('memory_count')} declared, "
          f"{len(rows)} in jsonl, integrity verified")
    # jsonl 内部自洽：声明的条数应与实际行数一致
    if manifest.get("memory_count") != len(rows):
        print(f"warning: count mismatch manifest={manifest.get('memory_count')} "
              f"jsonl={len(rows)}", file=sys.stderr)
    if not args.against_engine:
        return 0
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"error: env {args.token_env} not set", file=sys.stderr)
        return 2
    engine_hashes = fetch_engine_hashes(args.url, token, args.store)
    missing = pack_hashes - engine_hashes
    extra = engine_hashes - pack_hashes
    print(f"engine: {len(engine_hashes)} memories; "
          f"pack↔engine missing={len(missing)} extra={len(extra)}")
    for h in sorted(missing)[:5]:
        print(f"  missing: {h[:12]}…")
    for h in sorted(extra)[:5]:
        print(f"  extra:   {h[:12]}…")
    return 0 if not missing else 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台防 GBK 乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="mnevo_pack",
                                     description="Mnevo memory pack v0.1")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token-env", default="MNEVO_WORK_TOKEN")
    parser.add_argument("--store", default="default")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="引擎 -> mempack 目录")
    p_export.add_argument("--out", required=True)
    p_export.add_argument("--identity", help="身份层目录（默认不附带）")
    p_export.add_argument("--tar", action="store_true", help="额外打 tar.gz")
    p_export.add_argument("--namespace", default="work")
    p_export.add_argument("--engine", default="mcp-memory-service")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="mempack -> 引擎（按内容重存，幂等）")
    p_import.add_argument("--pack", required=True)
    p_import.add_argument("--dry-run", action="store_true")
    p_import.set_defaults(func=cmd_import)

    p_verify = sub.add_parser("verify", help="校验包完整性（可选对引擎比对）")
    p_verify.add_argument("--pack", required=True)
    p_verify.add_argument("--against-engine", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
