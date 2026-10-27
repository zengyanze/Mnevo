"""mem0 引擎 B shim 测试：离线映射单测 + 活引擎集成（不可达自动 skip）。"""

import hashlib
import importlib.util
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "engines" / "mem0-oss" / "server.py"
_spec = importlib.util.spec_from_file_location("mnevo_mem0_shim", _SCRIPT)
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)  # mem0 懒加载：离线机器可安全 import

ENGINE_B = os.environ.get("MNEVO_TEST_ENGINE_B_URL", "http://127.0.0.1:8100")
TOKEN_ENV = "MNEVO_WORK_TOKEN"


# ---------------- 离线：纯映射 ----------------

def test_content_hash_follows_engine_a_convention():
    """引擎 A（mcp-memory-service）口径：sha256(strip+lower(content))，
    与 tags/元数据无关——双引擎 verify 比对的锚点。"""
    assert shim.content_hash("  ABC 记忆 \n") == hashlib.sha256(
        "abc 记忆".encode("utf-8")).hexdigest()
    assert shim.content_hash("Same") == shim.content_hash(" same ")


def test_row_from_mem0_maps_tags_and_lineage():
    item = {
        "id": "m0", "memory": "偏好 X 后改 Y（合并稿）",
        "metadata": {"mnevo_tags": ["work", "distilled"],
                     "mnevo_memory_type": "fact",
                     "mnevo_valid_from": "2026-10-01T00:00:00Z",
                     "mnevo_supersedes": ["a" * 64],
                     "access": "junk-not-mnevo"},
        "created_at": "2026-10-27T10:00:00+00:00",
        "updated_at": "2026-10-27T10:00:00+00:00",
    }
    row = shim.row_from_mem0(item)
    assert row["content"] == item["memory"]
    assert row["content_hash"] == shim.content_hash(item["memory"])
    assert row["tags"] == ["work", "distilled"]
    assert row["memory_type"] == "fact"
    assert row["metadata"] == {"mnevo_valid_from": "2026-10-01T00:00:00Z",
                               "mnevo_supersedes": ["a" * 64]}
    assert row["created_at_iso"] == item["created_at"]


def test_lineage_metadata_filters_to_mnevo_namespace():
    src = {"mnevo_valid_to": "2026-12-01T00:00:00Z", "mnevo_tags": ["x"],
           "other": 1}
    assert shim.lineage_metadata(src) == {"mnevo_valid_to": "2026-12-01T00:00:00Z"}
    assert shim.lineage_metadata(None) == {}


# ---------------- 活引擎（引擎 B 不可达则 skip） ----------------

def _req(method, path, token, body=None):
    data = (json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else None)
    req = urllib.request.Request(
        ENGINE_B + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())


def _live():
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        pytest.skip(f"env {TOKEN_ENV} not set — live engine-b tests skipped")
    try:
        status, _ = _req("GET", "/api/health", token)
    except OSError:
        pytest.skip("engine B unreachable (tunnel down?) — live tests skipped")
    if status != 200:
        pytest.skip(f"engine B health HTTP {status} — live tests skipped")
    return token


def test_live_roundtrip_post_get_delete_search():
    token = _live()
    content = f"shim 活引擎往返测试 {uuid.uuid4().hex[:8]}（自动清理）"
    sha = shim.content_hash(content)
    try:
        status, body = _req("POST", "/api/memories", token,
                            {"content": content, "tags": ["shimtest"]})
        assert status == 200 and body["success"] and body["content_hash"] == sha

        status, body = _req("GET", "/api/memories/hashes", token)
        assert status == 200 and sha in body["hashes"]

        status, row = _req("GET", f"/api/memories/{sha}", token)
        assert status == 200 and row["tags"] == ["shimtest"]

        status, body = _req("POST", "/api/search", token,
                            {"query": "活引擎往返测试", "limit": 3})
        assert status == 200 and body["results"]
        top = body["results"][0]
        assert "similarity_score" in top and top["memory"]["content"]
    finally:
        status, body = _req("DELETE", f"/api/memories/{sha}", token)
        assert status == 200 and body["success"]
    status, body = _req("GET", "/api/memories/hashes", token)
    assert sha not in body["hashes"]
