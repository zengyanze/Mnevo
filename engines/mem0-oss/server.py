#!/usr/bin/env python3
"""mem0 OSS 引擎侧 REST shim —— 暴露 mcp-memory-service 兼容 API 子集。

定位（PLAN §1.1/Q1：引擎只经 MCP/REST 调用，绝不 import 内部）：
本文件是**引擎部署胶水**（engine-side），与 mcp-memory-service 自带的 web app
同层——Mnevo 核心（butler/pack）只看到与引擎 A 相同的 REST 面，换引擎=换 URL。

mem0 配置：infer=False 原样存取（不做 LLM 提取，ADD-only 是它本来面目）；
fastembed 本地 ONNX 嵌入（bge-small-zh，无 API key、无 torch）；
tags/谱系经 metadata.mnevo_* 往返。

端点（mcp-memory-service 子集）：
    GET  /api/health
    GET  /api/memories?page&page_size          → {memories,total,has_more}
    GET  /api/memories/hashes?cursor&limit     → {hashes}
    GET  /api/memories/{sha256}
    POST /api/memories  {content,tags,metadata?}
    PUT  /api/memories/{sha256} {tags?,metadata?}   （降权等：删旧存新，内容不变 hash 不变）
    DELETE /api/memories/{sha256}
    POST /api/search  {query,limit}            → {results:[{memory,similarity_score}]}

环境：MNEVO_MEM0_HOME（数据目录）、MNEVO_MEM0_PORT（默认 8100）、
      MNEVO_MEM0_USER（默认 work）、MNEVO_MEM0_TOKEN｜MCP_API_KEY（Bearer 校验）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 遥测先关再 import mem0（隔离试跑纪律；记忆引擎不外呼）
os.environ.setdefault("MEM0_TELEMETRY", "false")

TAGS_KEY = "mnevo_tags"
TYPE_KEY = "mnevo_memory_type"


# ---------------- 纯映射（离线单测覆盖，不依赖 mem0） ----------------

def content_hash(content: str) -> str:
    """引擎 A 同款约定（mcp-memory-service utils/hashing.py）：
    sha256(content.strip().lower()) —— 内容身份与 tags/元数据无关，
    双引擎 verify 哈希集合比对的锚点。"""
    return hashlib.sha256(content.strip().lower().encode("utf-8")).hexdigest()


def row_from_mem0(item: dict) -> dict:
    """mem0 行 → mcp-memory-service 行形状（pack/butler 直接消费）。"""
    content = item.get("memory") or ""
    meta = item.get("metadata") or {}
    tags = meta.get(TAGS_KEY)
    row = {
        "content": content,
        "content_hash": content_hash(content),
        "tags": tags if isinstance(tags, list) else [],
        "memory_type": meta.get(TYPE_KEY),
        "metadata": {k: v for k, v in meta.items() if k.startswith("mnevo_")
                     and k not in (TAGS_KEY, TYPE_KEY)},
        "created_at_iso": item.get("created_at"),
        "updated_at_iso": item.get("updated_at"),
    }
    return row


def lineage_metadata(metadata: dict | None) -> dict:
    """mcp 请求 metadata → mem0 metadata（mnevo_* 命名空间直传）。"""
    meta = metadata or {}
    return {k: v for k, v in meta.items()
            if k.startswith("mnevo_") and k not in (TAGS_KEY, TYPE_KEY)}


# ---------------- mem0 引擎封装 ----------------

_LOCK = threading.Lock()
_MEM = None
_USER = "work"


def get_memory():
    global _MEM, _USER
    if _MEM is None:
        from mem0 import Memory  # 懒加载：离线单测只碰上面的纯映射
        home = os.environ.get("MNEVO_MEM0_HOME",
                               os.path.expanduser("~/.mnevo/engines/mem0-oss"))
        _USER = os.environ.get("MNEVO_MEM0_USER", "work")
        cfg = {
            # infer=False 全程不调 LLM；dummy key 只是过 schema 校验
            "llm": {"provider": "openai",
                    "config": {"api_key": "unused-infer-false",
                               "model": "gpt-4o-mini"}},
            "embedder": {"provider": "fastembed",
                         "config": {"model": "BAAI/bge-small-zh-v1.5"}},
            "vector_store": {"provider": "qdrant",
                             "config": {"path": os.path.join(home, "qdrant"),
                                        "collection_name": "mnevo",
                                        "embedding_model_dims": 512}},
            "history_db_path": os.path.join(home, "history.db"),
        }
        _MEM = Memory.from_config(cfg)
    return _MEM


def _all_items() -> list[dict]:
    res = get_memory().get_all(filters={"user_id": _USER}, top_k=100000)
    return res.get("results", []) if isinstance(res, dict) else []


def _find(sha: str) -> dict | None:
    return next((it for it in _all_items() if content_hash(it.get("memory") or "") == sha),
                None)


def engine_add(content: str, tags: list, metadata: dict) -> str:
    meta = {TAGS_KEY: tags, **lineage_metadata(metadata)}
    if metadata.get("mnevo_memory_type"):
        meta[TYPE_KEY] = metadata["mnevo_memory_type"]
    with _LOCK:
        res = get_memory().add(content, user_id=_USER, infer=False, metadata=meta)
    results = res.get("results", []) if isinstance(res, dict) else []
    if not results:
        raise RuntimeError(f"mem0 add failed: {str(res)[:200]}")
    return content_hash(content)


def engine_delete(sha: str) -> bool:
    item = _find(sha)
    if not item:
        return False
    with _LOCK:
        get_memory().delete(item["id"])
    return True


def engine_update(sha: str, tags: list | None, metadata: dict | None) -> str:
    """PUT 语义（降权）：删旧存新——内容不变 ⇒ sha256 不变，id 会变。"""
    item = _find(sha)
    if not item:
        raise KeyError(sha)
    old_meta = item.get("metadata") or {}
    new_tags = tags if tags is not None else old_meta.get(TAGS_KEY, [])
    new_meta = {**old_meta, **(lineage_metadata(metadata) if metadata else {})}
    new_meta[TAGS_KEY] = new_tags
    content = item["memory"]
    with _LOCK:
        get_memory().delete(item["id"])
        get_memory().add(content, user_id=_USER, infer=False, metadata=new_meta)
    return sha


def engine_search(query: str, limit: int) -> list[dict]:
    res = get_memory().search(query, filters={"user_id": _USER}, top_k=limit)
    return res.get("results", []) if isinstance(res, dict) else []


# ---------------- HTTP（mcp-memory-service 兼容面） ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "mnevo-mem0-shim/0.1"

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        token = os.environ.get("MNEVO_MEM0_TOKEN") or os.environ.get("MCP_API_KEY") or ""
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {token}"

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"success": False, "error": "unauthorized"})
        u = urlparse(self.path)
        if u.path == "/api/health":
            return self._send(200, {"status": "ok", "engine": "mem0-oss",
                                    "user": _USER})
        if u.path == "/api/memories":
            q = parse_qs(u.query)
            page = int(q.get("page", ["1"])[0])
            size = int(q.get("page_size", ["100"])[0])
            rows = [row_from_mem0(it) for it in _all_items()]
            rows.sort(key=lambda r: r["created_at_iso"] or "")
            total = len(rows)
            chunk = rows[(page - 1) * size: page * size]
            return self._send(200, {"memories": chunk, "total": total,
                                    "page": page, "page_size": size,
                                    "has_more": page * size < total})
        if u.path == "/api/memories/hashes":
            hashes = [content_hash(it.get("memory") or "") for it in _all_items()]
            return self._send(200, {"hashes": hashes, "total": len(hashes)})
        m = re.fullmatch(r"/api/memories/([0-9a-f]{64})", u.path)
        if m:
            item = _find(m.group(1))
            if not item:
                return self._send(404, {"success": False, "error": "not found"})
            return self._send(200, row_from_mem0(item))
        return self._send(404, {"success": False, "error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"success": False, "error": "unauthorized"})
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/api/memories":
            content = body.get("content", "")
            if not content.strip():
                return self._send(400, {"success": False,
                                        "error": "empty content"})
            try:
                sha = engine_add(content, body.get("tags") or [],
                                 body.get("metadata") or {})
            except RuntimeError as exc:
                return self._send(500, {"success": False, "error": str(exc)[:200]})
            return self._send(200, {"success": True, "content_hash": sha})
        if u.path == "/api/search":
            query = body.get("query", "")
            if not query.strip():
                return self._send(400, {"success": False, "error": "empty query"})
            results = [{"memory": row_from_mem0(it),
                        "similarity_score": it.get("score", 0.0)}
                       for it in engine_search(query, int(body.get("limit", 10)))]
            return self._send(200, {"results": results,
                                    "query": query})
        return self._send(404, {"success": False, "error": "not found"})

    def do_PUT(self):
        if not self._authed():
            return self._send(401, {"success": False, "error": "unauthorized"})
        m = re.fullmatch(r"/api/memories/([0-9a-f]{64})", urlparse(self.path).path)
        if not m:
            return self._send(404, {"success": False, "error": "not found"})
        body = self._body()
        try:
            sha = engine_update(m.group(1), body.get("tags"), body.get("metadata"))
        except KeyError:
            return self._send(404, {"success": False, "error": "not found"})
        return self._send(200, {"success": True, "message": "updated",
                                "content_hash": sha})

    def do_DELETE(self):
        if not self._authed():
            return self._send(401, {"success": False, "error": "unauthorized"})
        m = re.fullmatch(r"/api/memories/([0-9a-f]{64})", urlparse(self.path).path)
        if not m:
            return self._send(404, {"success": False, "error": "not found"})
        ok = engine_delete(m.group(1))
        return self._send(200 if ok else 404,
                          {"success": ok} if ok
                          else {"success": False, "error": "not found"})

    def log_message(self, *args):  # 静默访问日志（mem0/uvicorn 自己有日志）
        return


def main() -> int:
    if hasattr(__import__("sys").stdout, "reconfigure"):
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    port = int(os.environ.get("MNEVO_MEM0_PORT", "8100"))
    get_memory()  # 预热（下载嵌入模型在服务前完成）
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mnevo mem0 shim on 127.0.0.1:{port} (user={_USER}, "
          f"telemetry={os.environ.get('MEM0_TELEMETRY')})", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
