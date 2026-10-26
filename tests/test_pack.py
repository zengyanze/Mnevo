"""mnevo_pack v0.1 测试：离线单测 + 桩服务器 import + 真引擎只读导出（缺引擎自动 skip）。"""

import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mnevo_pack.py"
_spec = importlib.util.spec_from_file_location("mnevo_pack", _SCRIPT)
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)

ENGINE_URL = os.environ.get("MNEVO_TEST_ENGINE_URL", "http://127.0.0.1:8000")
TOKEN_ENV = "MNEVO_TEST_PACK_TOKEN"


def _rows(n: int = 2) -> list[dict]:
    return [
        {
            "content": f"测试记忆 {i}：记忆包往返内容 {i}",
            "content_hash": f"hash{i:064d}",
            "tags": ["test", f"t{i}"],
            "memory_type": None,
            "created_at_iso": f"2026-10-2{i}T00:00:0{i}Z",
            "updated_at_iso": f"2026-10-2{i}T00:00:0{i}Z",
        }
        for i in range(n)
    ]


def _identity_dir(tmp: Path, with_proposal: bool = True) -> Path:
    d = tmp / "live"
    d.mkdir()
    (d / "persona.md").write_text("# persona", encoding="utf-8")
    (d / "values.md").write_text("# values", encoding="utf-8")
    if with_proposal:
        (d / "goals.proposal.md").write_text("# 未确认提案", encoding="utf-8")
    return d


def test_pack_row_projection():
    engine_mem = {
        "content": "abc",
        "content_hash": "h1",
        "tags": ["a", "agent:pi"],
        "memory_type": None,
        "metadata": {"access_count": 3, "relevance_score": 1.5},
        "created_at": 1792000000.0,
        "created_at_iso": "2026-10-15T00:00:00Z",
        "updated_at_iso": "2026-10-15T00:00:00Z",
    }
    row = mp.pack_row(engine_mem)
    assert row["content"] == "abc"
    assert row["tags"] == ["a", "agent:pi"]
    assert "metadata" not in row          # 遥测/派生元数据不入包
    assert "created_at" not in row        # 只保留 iso 谱系时间
    assert row["created_at_iso"] == "2026-10-15T00:00:00Z"


def test_write_read_roundtrip_and_tamper_detection(tmp_path):
    identity = _identity_dir(tmp_path)
    pack = mp.write_pack(tmp_path / "p.mempack", _rows(2),
                         sorted(identity.glob("*.md")), "work", "test-engine")
    manifest, rows = mp.read_pack(pack)
    assert manifest["memory_count"] == 2
    assert manifest["identity_files"] == ["persona.md", "values.md"]  # proposal 被排除
    assert rows[1]["content"] == "测试记忆 1：记忆包往返内容 1"

    target = pack / "memories.jsonl"
    target.write_text(target.read_text(encoding="utf-8") + "tampered\n",
                      encoding="utf-8")
    with pytest.raises(ValueError, match="integrity failure"):
        mp.read_pack(pack)


class _StubHandler(BaseHTTPRequestHandler):
    posts: list[dict] = []
    fail_hashes: set[str] = set()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        _StubHandler.posts.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        })
        duplicate = any("dup" in t for t in body.get("tags", []))
        rejected = any("rej" in t for t in body.get("tags", []))
        if rejected:
            self._reply(500, {"error": "boom"})
        elif duplicate:
            self._reply(409, {"error": "already exists"})
        else:
            self._reply(200, {"success": True, "content_hash": "x"})

    def _reply(self, code: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默测试输出
        return


@pytest.fixture()
def stub_server():
    _StubHandler.posts = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_import_against_stub(tmp_path, stub_server, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "stub-token")
    rows = [
        {"content": "普通记忆", "content_hash": "h" * 64, "tags": ["a"]},
        {"content": "重复记忆", "content_hash": "d" * 64, "tags": ["dup"]},
        {"content": "拒收记忆", "content_hash": "r" * 64, "tags": ["rej"]},
    ]
    pack = mp.write_pack(tmp_path / "s.mempack", rows, [], "work", "stub")

    args = type("A", (), {
        "pack": str(pack), "dry_run": False,
        "url": stub_server, "token_env": TOKEN_ENV,
    })()
    rc = mp.cmd_import(args)

    assert rc == 1, "存在 rejected 时应返回非零"
    kinds = [p["body"]["content"] for p in _StubHandler.posts]
    assert kinds == ["普通记忆", "重复记忆", "拒收记忆"]
    assert all(p["auth"] == "Bearer stub-token" for p in _StubHandler.posts)
    assert all("charset=utf-8" in p["content_type"] for p in _StubHandler.posts)


def test_import_dry_run_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    pack = mp.write_pack(tmp_path / "d.mempack", _rows(1), [], "work", "stub")
    args = type("A", (), {"pack": str(pack), "dry_run": True,
                          "url": "http://127.0.0.1:1", "token_env": TOKEN_ENV})()
    assert mp.cmd_import(args) == 0  # dry-run 不需要 token/引擎


def test_live_export_readonly(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, os.environ.get("MNEVO_WORK_TOKEN", ""))
    token = os.environ.get(TOKEN_ENV, "")
    try:
        status, _ = mp.http_json(f"{ENGINE_URL}/api/health", token)
    except OSError:
        pytest.skip("engine unreachable (tunnel down?) — live read-only test skipped")
    if status != 200:
        pytest.skip(f"engine health HTTP {status} — live test skipped")

    rows = [mp.pack_row(m) for m in mp.fetch_all_memories(ENGINE_URL, token)]
    assert rows, "真实引擎应至少有种子记忆"
    pack = mp.write_pack(tmp_path / "live.mempack", rows, [], "work",
                         "mcp-memory-service")
    manifest, back = mp.read_pack(pack)  # 含完整性校验
    assert manifest["memory_count"] == len(rows) == len(back)
    assert {r["content_hash"] for r in back} == {r["content_hash"] for r in rows}


def test_import_passes_store_field(monkeypatch):
    """import --store acceptance 时 POST body 必须带 store 字段。"""
    import argparse
    from pathlib import Path as P
    # 复用既有桩：直接调 post_memory 断言 body
    spec = importlib.util.spec_from_file_location(
        "mnevo_pack", P(__file__).resolve().parents[1] / "scripts" / "mnevo_pack.py")
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)
    captured = {}

    def fake_http_json(url, token, method="GET", body=None, timeout=60):
        captured.update(body=body)
        return 200, {"success": True}

    monkeypatch.setattr(mp, "http_json", fake_http_json)
    outcome, _ = mp.post_memory("http://x", "t", {"content": "c", "tags": []},
                                store="acceptance")
    assert outcome == "created"
    assert captured["body"]["store"] == "acceptance"
