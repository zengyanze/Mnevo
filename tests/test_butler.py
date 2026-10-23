"""mnevo_butler v0.1 测试：桩引擎上 scan→提案→review accept→引擎删除→审计 全链。"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mnevo_butler.py"
_spec = importlib.util.spec_from_file_location("mnevo_butler", _SCRIPT)
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)

TOKEN_ENV = "MNEVO_TEST_BUTLER_TOKEN"

# 桩库：三条记忆，A 与 B 高相似（>阈值），C 无关
A = {"content": "上传接口联调结论：dev 环境 8080", "content_hash": "a" * 64,
     "tags": ["work"], "memory_type": None,
     "created_at_iso": "2026-10-19T01:00:00Z", "updated_at_iso": "2026-10-19T01:00:00Z"}
B = {"content": "上传接口联调结论：dev 环境 8080 端口", "content_hash": "b" * 64,
     "tags": ["work"], "memory_type": None,
     "created_at_iso": "2026-10-20T01:00:00Z", "updated_at_iso": "2026-10-20T01:00:00Z"}
C = {"content": "完全无关的第三条", "content_hash": "c" * 64,
     "tags": ["work"], "memory_type": None,
     "created_at_iso": "2026-10-21T01:00:00Z", "updated_at_iso": "2026-10-21T01:00:00Z"}
LIB = {m["content_hash"]: m for m in (A, B, C)}


class _EngineStub(BaseHTTPRequestHandler):
    deleted: list[str] = []

    def _send(self, code: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/memories"):
            self._send(200, {"memories": [m for h, m in LIB.items()
                                          if h not in _EngineStub.deleted]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/api/search":
            results = []
            for h, m in LIB.items():
                if h in _EngineStub.deleted:
                    continue
                score = 0.95 if m is B and A["content"] in body["query"] else (
                    0.95 if m is A and B["content"] in body["query"] else 0.10)
                if body["query"] == m["content"]:
                    score = 1.0
                results.append({"memory": m, "similarity_score": score})
            results.sort(key=lambda r: -r["similarity_score"])
            self._send(200, {"results": results[: body.get("limit", 5)]})
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        h = self.path.rsplit("/", 1)[-1]
        _EngineStub.deleted.append(h)
        self._send(200, {"success": True})

    def log_message(self, *args):
        return


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    _EngineStub.deleted = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EngineStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(TOKEN_ENV, "stub-token")
    butler = mb.Butler(f"http://127.0.0.1:{server.server_port}", "stub-token",
                       "default", tmp_path / "home")
    yield butler
    server.shutdown()


def test_scan_writes_proposal_for_near_dup(engine):
    engine.scan()
    proposals = list((engine.home / "proposals").glob("*.md"))
    assert len(proposals) == 1, "A/B 近对应产生一条提案，C 不参与"
    text = proposals[0].read_text(encoding="utf-8")
    assert "status: open" in text
    assert f"delete: {'b'*12}" in text      # created 晚者（B）为删除候选
    assert f"keep: {'a'*12}" in text

    engine.scan()                             # 幂等：同 pair 不重复提案
    assert len(list((engine.home / "proposals").glob("*.md"))) == 1


def test_review_accept_deletes_and_audits(engine, capsys):
    engine.scan()
    rc = engine.review_decide("merge", "accept")
    out = capsys.readouterr().out
    assert rc == 0 and "accept" in out
    assert _EngineStub.deleted == [B["content_hash"]], "accept=删除晚创建的重复条"
    md = list((engine.home / "proposals").glob("*.md"))[0]
    assert "status: accept" in md.read_text(encoding="utf-8")

    audit = (engine.home / "audit" / "butler.jsonl").read_text(encoding="utf-8")
    assert '"event": "delete-duplicate"' in audit
    assert '"event": "review"' in audit
    assert '"event": "scan"' in audit


def test_review_dismiss_keeps_engine(engine):
    engine.scan()
    rc = engine.review_decide("merge", "dismiss")
    assert rc == 0
    assert _EngineStub.deleted == [], "dismiss 不得动引擎"
    md = list((engine.home / "proposals").glob("*.md"))[0]
    assert "status: dismiss" in md.read_text(encoding="utf-8")


def test_scan_after_delete_finds_nothing(engine):
    engine.scan()
    engine.review_decide("merge", "accept")
    for p in list((engine.home / "proposals").glob("*.md")):
        p.unlink()                        # 模拟归档已决提案
    engine.scan()                         # 库中只剩 A/C，无新提案
    assert list((engine.home / "proposals").glob("*.md")) == []
