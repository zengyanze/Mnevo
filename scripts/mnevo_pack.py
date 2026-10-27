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
import re
import shutil
import sys
import tarfile
import unicodedata
import urllib.error
import urllib.request
import uuid
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
                timeout: int = 60, store: str | None = None) -> tuple[str, str]:
    """重存一条记忆；返回 (结果分类, 说明)。分类 ∈ created/duplicate/rejected。"""
    url = f"{base_url.rstrip('/')}/api/memories"
    body = {"content": row["content"], "tags": row.get("tags") or []}
    if store:
        body["store"] = store
    status, resp = http_json(
        url, token, method="POST", body=body, timeout=timeout,
    )
    body = resp
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    if status in (200, 201):
        # v12 语义查重：拒存时包成 HTTP 200 + success:false + "Duplicate…"
        # 但"相似已存仍写入"时是 success:true + duplicate 字样 → 应算 created
        if isinstance(body, dict) and body.get("success") is False:
            if "duplicate" in text.lower() or "exist" in text.lower():
                return "duplicate", text[:200]
            return "rejected", text[:200]
        if isinstance(body, dict) and body.get("success") is True:
            return "created", text[:200]
        if "duplicate" in text.lower() or "exist" in text.lower():
            return "duplicate", text[:200]
        return "created", text[:200]
    if status in (409, 422) or "exist" in text.lower() or "duplicate" in text.lower():
        return "duplicate", text[:200]
    return "rejected", f"HTTP {status}: {text[:200]}"


# ---------- PAM v1.0（Portable AI Memory）映射 ----------
# 规范：https://portable-ai-memory.org/spec/v1.0/（2026-02-17，CC BY 4.0）
PAM_SPEC_URI = "https://portable-ai-memory.org/spec/v1.0/"
PAM_TOOL_ID = "mnevo_pack/0.2.0"  # spec 要求 system/semver 格式
PAM_TYPE_BY_TAG = {"identity": "identity", "skill": "skill", "goal": "goal",
                   "preference": "preference", "fact": "fact"}


def pam_normalize(text: str) -> str:
    """PAM §6.1：trim → 小写 → NFC → 折叠连续空白。"""
    return unicodedata.normalize("NFC", " ".join(text.strip().lower().split()))


def pam_content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(
        pam_normalize(text).encode("utf-8")).hexdigest()


def _pam_tags(tags: list) -> list[str]:
    """清洗为 ^[a-z0-9][a-z0-9_-]*$（PAM tag 模式），去重保序。"""
    out: list[str] = []
    for t in tags or []:
        t = str(t).lower().strip().replace(":", "-").replace(" ", "-")
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", t) and t not in out:
            out.append(t)
    return out


def _jcs_dumps(obj) -> str:
    """RFC 8785 子集：递归字典键排序+无空白+最小转义。
    本实现只保证无浮点、ASCII 键的对象（我们自产的 store 恰好如此）。
    遇浮点直接拒（ES6 数字序列化不做近似）。"""
    def walk(v):
        if isinstance(v, dict):
            return {k: walk(v[k]) for k in sorted(v)}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, float):
            raise ValueError("JCS subset: floats not supported")
        return v
    return json.dumps(walk(obj), ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False)


def build_pam_store(rows: list[dict], namespace: str,
                    export_id: str | None = None) -> dict:
    """mempack rows -> PAM memory-store 文档。
    id 用 uuid5(URL-ns, 'mnevo:'+engine_hash)：确定性，重导出幂等。
    （spec 说 SHOULD uuid4；确定性优先，偏差在此注记。）"""
    memories = []
    for r in rows:
        tags = _pam_tags(r.get("tags") or [])
        hit = next((t for t in tags if t in PAM_TYPE_BY_TAG), "")
        memories.append({
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "mnevo:" + r["content_hash"])),
            "type": PAM_TYPE_BY_TAG.get(hit, "context"),
            "status": "active",
            "content": r["content"],
            "content_hash": pam_content_hash(r["content"]),
            "tags": tags,
            "temporal": {
                "created_at": r.get("created_at_iso") or _now_iso(),
                "updated_at": r.get("updated_at_iso"),
            },
            "provenance": {
                "platform": "mnevo",
                "platform_user_id": namespace,
                "extraction_method": "api_export",
                "extractor": PAM_TOOL_ID,
            },
        })
    memories.sort(key=lambda m: m["id"])
    return {
        "schema": "portable-ai-memory",
        "schema_version": "1.0",
        "spec_uri": PAM_SPEC_URI,
        "export_id": export_id or str(uuid.uuid4()),
        "exported_by": PAM_TOOL_ID,
        "export_date": _now_iso(),
        "export_type": "full",
        "owner": {"id": f"urn:mnevo:{namespace}"},
        "memories": memories,
        "relations": [],
        "integrity": {
            "canonicalization": "RFC8785",
            "checksum": "sha256:" + hashlib.sha256(
                _jcs_dumps(memories).encode("utf-8")).hexdigest(),
            "total_memories": len(memories),
        },
    }


def write_pam(store: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "memory-store.json").write_text(
        json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_pam(pam_dir: Path) -> tuple[dict, list[dict]]:
    store = json.loads(
        (pam_dir / "memory-store.json").read_text(encoding="utf-8"))
    if store.get("schema") != "portable-ai-memory":
        raise ValueError("not a PAM store")
    mems = store.get("memories", [])
    integ = store.get("integrity") or {}
    total = integ.get("total_memories")
    if isinstance(total, int) and total != len(mems):
        raise ValueError(
            f"integrity failure: total_memories {total} != {len(mems)}")
    if integ.get("checksum"):
        try:
            calc = "sha256:" + hashlib.sha256(_jcs_dumps(
                sorted(mems, key=lambda m: m["id"])).encode("utf-8")).hexdigest()
            if calc != integ["checksum"]:
                raise ValueError("integrity failure: checksum mismatch")
        except ValueError as e:
            if "floats" in str(e):
                print("warn: 外来 PAM 含浮点字段，JCS 校验跳过（子集实现限制）")
            else:
                raise
    return store, mems


def pam_to_rows(mems: list[dict]) -> list[dict]:
    """PAM memories -> mempack rows（仅 active；hash 重算为引擎口径）。"""
    rows = []
    for m in mems:
        if m.get("status") not in (None, "active"):
            continue
        content = m["content"]
        rows.append({
            "content": content,
            "content_hash": hashlib.sha256(
                content.encode("utf-8")).hexdigest(),
            "tags": _pam_tags(m.get("tags") or []),
            "memory_type": None if m.get("type") in (None, "custom") else m["type"],
            "created_at_iso": (m.get("temporal") or {}).get("created_at"),
            "updated_at_iso": (m.get("temporal") or {}).get("updated_at"),
        })
    return rows


def cmd_to_pam(args: argparse.Namespace) -> int:
    manifest, rows = read_pack(Path(args.pack))
    store = build_pam_store(rows, manifest.get("namespace", "work"))
    write_pam(store, Path(args.out))
    print(f"pam export: {len(store['memories'])} memories -> "
          f"{args.out}/memory-store.json "
          f"(checksum {store['integrity']['checksum'][:23]}…)")
    return 0


def cmd_from_pam(args: argparse.Namespace) -> int:
    store, mems = read_pam(Path(args.pam))
    rows = pam_to_rows(mems)
    write_pack(Path(args.out), rows, [],
               store.get("owner", {}).get("id", "pam"), "pam-import")
    print(f"pam import: {len(rows)} active memories -> {args.out}")
    return 0


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
        outcome, note = post_memory(args.url, token, r,
                                    store=getattr(args, "store", None))
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


# ------------------------------------------------- Claude 导出读入（v0.2）

CLAUDE_CHUNK_LIMIT = 450  # 引擎 ONNX 嵌入实测上限 ~500 字，留余量


def _find_zip_entry(zf, suffix: str) -> str | None:
    """zip 内按后缀找文件（官方导出可能嵌套在子目录）。"""
    for name in zf.namelist():
        if name.endswith(suffix) and not name.endswith("/"):
            return name
    return None


def _split_long(text: str, limit: int = CLAUDE_CHUNK_LIMIT) -> list[str]:
    """超长内容切分：段落贪心打包，单段超限再按句/字符硬切。"""
    if len(text) <= limit:
        return [text]
    paras = [p for p in (s.strip() for s in text.split("\n\n")) if p]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        candidate = f"{cur}\n\n{p}".strip() if cur else p
        if len(candidate) <= limit or not cur:
            cur = candidate
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    # 单段仍超限 → 按句号硬切
    out: list[str] = []
    for c in chunks:
        while len(c) > limit:
            cut = c.rfind("。", 0, limit)
            cut2 = c.rfind(". ", 0, limit)
            cut = max(cut, cut2)
            if cut < limit // 2:
                cut = limit
            out.append(c[:cut].strip())
            c = c[cut:].strip()
        if c:
            out.append(c)
    return out


def parse_claude_export(source: Path) -> tuple[list[dict], dict]:
    """读 Claude 数据导出（zip 或裸 memories.json）→ (rows, stats)。

    三代格式全兼容（依据官方导出 + 社区解析器实证 2026-10）：
      3. memory_files: [{path, content, updated_at}]（markdown 文件夹制）
      2. conversations_memory: str（旧版摘要）
      1. project_memories: {project-uuid: str}（项目名经 projects.json 解析）
    超长文件按段落切块（引擎嵌入上限）；块序号保序，全文无损。
    """
    import zipfile

    project_names: dict[str, str] = {}
    mem_obj: dict | None = None
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            proj_entry = _find_zip_entry(zf, "projects.json")
            if proj_entry:
                projects = json.loads(zf.read(proj_entry).decode("utf-8"))
                project_names = {
                    p.get("uuid", ""): p.get("name", "")
                    for p in projects if isinstance(p, dict)
                }
            mem_entry = _find_zip_entry(zf, "memories.json")
            if mem_entry:
                data = json.loads(zf.read(mem_entry).decode("utf-8"))
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    mem_obj = data[0]
    elif source.is_file():
        data = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(data, list) and data and isinstance(data[0], dict):
            mem_obj = data[0]

    rows: list[dict] = []
    stats = {"source": str(source), "memory_files": 0, "chunks": 0,
             "legacy": 0, "projects": 0, "project_names": 0}
    if not mem_obj:
        return rows, stats

    def add_rows(content: str, tags: list[str], updated: str | None) -> None:
        parts = _split_long(content)
        n = len(parts)
        for i, chunk in enumerate(parts, start=1):
            body = chunk if n == 1 else f"[part {i}/{n}]\n{chunk}"
            rows.append({
                "content": body,
                "content_hash": hashlib.sha256(
                    body.encode("utf-8")).hexdigest(),
                "tags": tags,
                "memory_type": None,
                "created_at_iso": updated,
                "updated_at_iso": updated,
            })

    for f in mem_obj.get("memory_files") or []:
        if not isinstance(f, dict):
            continue
        content = (f.get("content") or "").strip()
        if not content:
            continue
        path = f.get("path") or ""
        name = path.rstrip("/").rpartition("/")[2] or path or "memory"
        stats["memory_files"] += 1
        add_rows(content, ["claude", f"claude-file:{name}"],
                 f.get("updated_at"))
    stats["chunks"] = len(rows)

    legacy = (mem_obj.get("conversations_memory") or "").strip()
    if legacy:
        stats["legacy"] = 1
        add_rows(legacy, ["claude", "claude:conversations-memory"], None)

    for pid, text in (mem_obj.get("project_memories") or {}).items():
        if not (isinstance(text, str) and text.strip()):
            continue
        pname = project_names.get(pid) or pid or "unknown"
        if project_names.get(pid):
            stats["project_names"] += 1
        stats["projects"] += 1
        add_rows(text, ["claude", f"claude-project:{pname}"], None)
    return rows, stats


def cmd_from_claude(args: argparse.Namespace) -> int:
    rows, stats = parse_claude_export(Path(args.claude))
    if not rows:
        print(f"no memories found in {args.claude} "
              f"(memories.json 缺失或为空)", file=sys.stderr)
        return 1
    out = write_pack(Path(args.out), rows, [], args.namespace,
                     "claude.ai-export")
    print(f"claude -> mempack: {out}")
    print(f"  memory_files={stats['memory_files']} (rows={stats['chunks']}, "
          f"超长已切块) legacy={stats['legacy']} "
          f"projects={stats['projects']} (resolved names={stats['project_names']})")
    print(f"  后续: import --pack {out} 入库 / to-pam 转社区标准")
    return 0


# ------------------------------------------------- Gemini 写出（v0.2）

GEMINI_TYPE_SECTIONS = (
    ("identity", "Who I am"),
    ("preference", "Preferences"),
    ("fact", "Facts about me"),
    ("goal", "Current goals"),
    ("skill", "Skills & how I work"),
)
GEMINI_IDENTITY_SECTIONS = (
    ("persona.md", "Who I am"),
    ("values.md", "What I value"),
    ("goals.md", "Current goals"),
    ("threads.md", "What I'm working on"),
)


def build_gemini_text(rows: list[dict], identity_texts: dict[str, str]) -> str:
    """构造 Gemini import-memory 粘贴文本（官方路径=无 schema 纯文本）。"""
    lines: list[str] = [
        "# About me",
        "",
        f"(memory import, generated {_now_iso()[:10]} from my Mnevo memory pack)",
        "",
    ]
    for name, section in GEMINI_IDENTITY_SECTIONS:
        text = (identity_texts.get(name) or "").strip()
        if text:
            lines += [f"## {section}", "", text.strip(), ""]
    for mtype, section in GEMINI_TYPE_SECTIONS:
        items = [r for r in rows if r.get("memory_type") == mtype]
        if not items:
            continue
        lines += [f"## {section}", ""]
        for r in items:
            item = r["content"].strip().replace("\n", " ")
            lines.append(f"- {item}")
        lines.append("")
    rest = [r for r in rows
            if r.get("memory_type") not in
            {t for t, _ in GEMINI_TYPE_SECTIONS}]
    if rest:
        lines += ["## Notes", ""]
        for r in rest:
            item = r["content"].strip().replace("\n", " ")
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cmd_to_gemini(args: argparse.Namespace) -> int:
    pack = Path(args.pack)
    manifest, rows = read_pack(pack)
    identity_texts = {}
    for name, _ in GEMINI_IDENTITY_SECTIONS:
        p = pack / "identity" / name
        if p.is_file():
            identity_texts[name] = p.read_text(encoding="utf-8")
    text = build_gemini_text(rows, identity_texts)
    out = Path(args.out)
    out.write_text(text, encoding="utf-8")
    typed = sum(1 for r in rows if r.get("memory_type") in
                {t for t, _ in GEMINI_TYPE_SECTIONS})
    print(f"gemini paste text: {out} ({len(text)} chars)")
    print(f"  rows={len(rows)} typed={typed} identity_files={len(identity_texts)}")
    print("  用法: gemini.google/import-memory → Paste into Gemini")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台防 GBK 乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS)
    common.add_argument("--token-env", default=argparse.SUPPRESS)
    common.add_argument("--store", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(
        prog="mnevo_pack", description="Mnevo memory pack v0.1",
        parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", parents=[common], help="引擎 -> mempack 目录")
    p_export.add_argument("--out", required=True)
    p_export.add_argument("--identity", help="身份层目录（默认不附带）")
    p_export.add_argument("--tar", action="store_true", help="额外打 tar.gz")
    p_export.add_argument("--namespace", default="work")
    p_export.add_argument("--engine", default="mcp-memory-service")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", parents=[common],
                              help="mempack -> 引擎（按内容重存，幂等）")
    p_import.add_argument("--pack", required=True)
    p_import.add_argument("--dry-run", action="store_true")
    p_import.set_defaults(func=cmd_import)

    p_verify = sub.add_parser("verify", parents=[common],
                              help="校验包完整性（可选对引擎比对）")
    p_verify.add_argument("--pack", required=True)
    p_verify.add_argument("--against-engine", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    p_to = sub.add_parser("to-pam", parents=[common],
                          help="mempack -> PAM v1.0 目录（导出兼容社区标准）")
    p_to.add_argument("--pack", required=True)
    p_to.add_argument("--out", required=True)
    p_to.set_defaults(func=cmd_to_pam)

    p_from = sub.add_parser("from-pam", parents=[common],
                            help="PAM v1.0 目录 -> mempack（跨系统迁入）")
    p_from.add_argument("--pam", required=True)
    p_from.add_argument("--out", required=True)
    p_from.set_defaults(func=cmd_from_pam)

    p_fc = sub.add_parser("from-claude", parents=[common],
                          help="Claude 数据导出(zip/memories.json) -> mempack")
    p_fc.add_argument("--claude", required=True,
                      help="claude 导出 zip 或裸 memories.json")
    p_fc.add_argument("--out", required=True)
    p_fc.add_argument("--namespace", default="personal")
    p_fc.set_defaults(func=cmd_from_claude)

    p_tg = sub.add_parser("to-gemini", parents=[common],
                          help="mempack -> Gemini import-memory 粘贴文本")
    p_tg.add_argument("--pack", required=True)
    p_tg.add_argument("--out", required=True)
    p_tg.set_defaults(func=cmd_to_gemini)

    args = parser.parse_args(argv)
    for name, dv in (("url", "http://127.0.0.1:8000"),
                     ("token_env", "MNEVO_WORK_TOKEN"),
                     ("store", "default")):
        setattr(args, name, getattr(args, name, dv))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
