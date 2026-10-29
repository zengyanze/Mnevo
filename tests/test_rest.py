"""mnevo_rest 测试：secrets 闸门 + 桩引擎上的 store/search/recent。"""

import importlib.util
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mnevo_rest.py"
_spec = importlib.util.spec_from_file_location("mnevo_rest", _SCRIPT)
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


def test_secret_gate_hits_and_force():
    assert mr.secret_hits("key is sk-abcdefghijklmnopqrstuvwx")
    assert mr.secret_hits("-----BEGIN RSA PRIVATE KEY-----")
    assert mr.secret_hits("api_key=abcdefghij12345")
    assert not mr.secret_hits("普通经历：联调结论在跟进清单")  # 中文内容零误伤


def test_read_content_stdin_encodings(monkeypatch):
    args = type("A", (), {"content": None})()
    for payload in (b"\xef\xbb\xbf" + "中文经历：UTF-8 管道".encode("utf-8"),
                    "中文经历：GBK 管道".encode("gbk")):
        monkeypatch.setattr(sys, "stdin",
                            io.TextIOWrapper(io.BytesIO(payload)))
        text = mr._read_content(args)
        assert "中文经历" in text
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(
        io.BytesIO(b"   ")))
    with pytest.raises(SystemExit):
        mr._read_content(args)


class _Handler(BaseHTTPRequestHandler):
    posts: list[dict] = []

    def _send(self, code, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/api/memories":
            _Handler.posts.append(body)
            self._send(200, {"success": True, "content_hash": "a" * 64})
        elif self.path == "/api/search":
            self._send(200, {"results": [{
                "memory": {"content": "命中记忆", "tags": ["work"],
                           "created_at_iso": "2026-10-29T00:00:00Z"},
                "similarity_score": 0.91}]})
        else:
            self._send(404, {})

    def do_GET(self):
        if self.path.startswith("/api/memories"):
            self._send(200, {"memories": [{
                "content": "最近一条", "tags": ["work", "agent:codex"],
                "created_at_iso": "2026-10-29T00:00:00Z"}]})
        else:
            self._send(404, {})

    def log_message(self, *args):
        return


@pytest.fixture()
def stub(monkeypatch):
    _Handler.posts = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("MNEVO_TEST_REST_TOKEN", "stub")
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_store_appends_agent_tag(stub, monkeypatch, capsys):
    rc = mr.main(["store", "--url", stub, "--token-env", "MNEVO_TEST_REST_TOKEN",
                 "--content", "适配验证记忆", "--tags", "work,mnevo"])
    assert rc == 0
    assert _Handler.posts[0]["tags"] == ["work", "mnevo", "agent:codex"]
    assert _Handler.posts[0]["content"] == "适配验证记忆"
    assert "store: created" in capsys.readouterr().out


def test_store_refuses_secrets_without_force(stub, monkeypatch, capsys):
    rc = mr.main(["store", "--url", stub, "--token-env", "MNEVO_TEST_REST_TOKEN",
                 "--content", "token=ghp_" + "a" * 30, "--tags", "work"])
    assert rc == 2 and _Handler.posts == []
    assert "拒存" in capsys.readouterr().err
    rc = mr.main(["store", "--url", stub, "--token-env", "MNEVO_TEST_REST_TOKEN",
                  "--content", "确认是占位符 ghp_" + "a" * 30, "--tags", "work",
                  "--force"])
    assert rc == 0 and len(_Handler.posts) == 1


def test_search_and_recent_render(stub, monkeypatch, capsys):
    assert mr.main(["search", "--url", stub, "--token-env",
                    "MNEVO_TEST_REST_TOKEN", "验证"]) == 0
    out = capsys.readouterr().out
    assert "[0.910]" in out and "命中记忆" in out
    assert mr.main(["search", "--url", stub, "--token-env",
                    "MNEVO_TEST_REST_TOKEN", "--query", "验证"]) == 0
    assert mr.main(["recent", "--url", stub, "--token-env",
                    "MNEVO_TEST_REST_TOKEN", "--n", "3"]) == 0
    assert "最近一条" in capsys.readouterr().out
