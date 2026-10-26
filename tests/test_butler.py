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
    posted: list[dict] = []
    events: list[tuple] = []          # ("post", 内容前20) / ("delete", hash) 时序
    post_replies: list[dict] = []     # 队列；空则默认 {"success": True}

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
        elif self.path == "/api/memories":
            _EngineStub.posted.append(body)
            _EngineStub.events.append(("post", body.get("content", "")[:20]))
            reply = (_EngineStub.post_replies.pop(0)
                     if _EngineStub.post_replies else {"success": True})
            self._send(200, reply)
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        h = self.path.rsplit("/", 1)[-1]
        _EngineStub.deleted.append(h)
        _EngineStub.events.append(("delete", h))
        self._send(200, {"success": True})

    def log_message(self, *args):
        return


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    _EngineStub.deleted = []
    _EngineStub.posted = []
    _EngineStub.events = []
    _EngineStub.post_replies = []
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


# ---------------- distill ----------------

def test_parse_llm_json_fenced_and_dirty():
    fenced = ('前置噪声\n```json\n{"merges":[{"hashes":["a"],"content":"x"}],'
              '"distills":[],"archives":[]}\n```\n后置噪声')
    out = mb._parse_llm_json(fenced)
    assert out["merges"][0]["hashes"] == ["a"]
    with pytest.raises(ValueError):
        mb._parse_llm_json("完全不是 JSON")
    with pytest.raises(ValueError):
        mb._parse_llm_json('{"merges":[{"hashes":[],"content":"x"}],'
                           '"distills":[],"archives":[]}')


def test_distill_skips_without_key(engine, monkeypatch):
    monkeypatch.delenv("MNEVO_LLM_API_KEY", raising=False)
    assert engine.distill() == 0
    audit = (engine.home / "audit" / "butler.jsonl").read_text(encoding="utf-8")
    assert '"skipped": "no_llm_key"' in audit


def test_distill_proposal_and_apply(engine, monkeypatch):
    monkeypatch.setenv("MNEVO_LLM_API_KEY", "test-key")
    fake = ('```json\n{"merges":[{"hashes":["' + "a" * 64 + '","' + "b" * 64 + '"],'
            + '"content":"上传接口联调结论（合并）：dev 环境 8080"}],'
            + '"distills":[],"archives":[{"hash":"' + "c" * 64 + '",'
            + '"reason":"一次性演示"}]}\n```')
    monkeypatch.setattr(mb, "llm_chat", lambda *a, **k: fake)

    assert engine.distill(all_memories=True) == 0
    props = list((engine.home / "proposals").glob("*-distill.md"))
    assert len(props) == 1
    text = props[0].read_text(encoding="utf-8")
    assert "action: apply-json" in text and "```json" in text
    assert (engine.home / mb.Butler.STATE_FILE).is_file(), "蒸馏水位已落盘"
    assert _EngineStub.posted == [], "提案阶段不得写引擎"

    rc = engine.review_decide("distill", "accept")
    assert rc == 0
    assert sorted(_EngineStub.deleted) == sorted(["a" * 64, "b" * 64, "c" * 64])
    assert len(_EngineStub.posted) == 1, "merge 存入一条新内容"
    assert "distilled" in _EngineStub.posted[0]["tags"]
    # 先删后存：删源条必须发生在存蒸馏稿之前（v12 语义查重会拒活源的近义稿）
    kinds = [e[0] for e in _EngineStub.events]
    assert kinds.index("post") > max(
        i for i, e in enumerate(_EngineStub.events)
        if e[0] == "delete" and e[1] in ("a" * 64, "b" * 64))
    assert "status: accept" in props[0].read_text(encoding="utf-8")
    audit = (engine.home / "audit" / "butler.jsonl").read_text(encoding="utf-8")
    assert '"event": "apply-store"' in audit
    assert '"event": "apply-done"' in audit


def test_apply_rolls_back_on_semantic_duplicate(engine, monkeypatch):
    """v12 语义查重拒存蒸馏稿（200+success:false）→回滚源条+提案留 open。"""
    monkeypatch.setenv("MNEVO_LLM_API_KEY", "test-key")
    fake = ('{"merges":[{"hashes":["' + "a" * 64 + '","' + "b" * 64 + '"],'
            + '"content":"合并稿"}],"distills":[],"archives":[]}')
    monkeypatch.setattr(mb, "llm_chat", lambda *a, **k: fake)
    _EngineStub.post_replies = [
        {"success": False,
         "message": "Duplicate content detected (semantically similar to x)"}]
    engine.distill(all_memories=True)

    rc = engine.review_decide("distill", "accept")
    assert rc == 2, "失败必须返 2 且提案留 open"
    assert sorted(_EngineStub.deleted) == sorted(["a" * 64, "b" * 64])
    contents = [p["content"] for p in _EngineStub.posted]
    assert contents[0] == "合并稿"
    assert contents[1:] == [A["content"], B["content"]], "源条已回滚存回"
    audit = (engine.home / "audit" / "butler.jsonl").read_text(encoding="utf-8")
    assert '"event": "apply-restore"' in audit
    props = list((engine.home / "proposals").glob("*-distill.md"))
    assert "status: open" in props[0].read_text(encoding="utf-8")


def test_post_memory_classifies_v12_semantic_dup(engine):
    _EngineStub.post_replies = [
        {"success": False,
         "message": "Duplicate content detected (semantically similar to x)"}]
    outcome, _ = mb.post_memory(engine.url, "t", A)
    assert outcome == "duplicate"


def test_post_memory_classifies_embed_failure(engine):
    """v12 嵌入失败也包成 200+success:false，但不是 duplicate。"""
    _EngineStub.post_replies = [
        {"success": False,
         "message": "Failed to store memory: Failed to generate embedding: "
                   "[ONNXRuntimeError] : 1 : FAIL"}]
    outcome, _ = mb.post_memory(engine.url, "t", A)
    assert outcome == "rejected"


def test_parse_llm_json_rejects_long_content():
    with pytest.raises(ValueError):
        mb._parse_llm_json('{"merges":[{"hashes":["a"],"content":"'
                           + "长" * 601 + '"}],"distills":[],"archives":[]}')


def test_distill_review_dismiss_touches_nothing(engine, monkeypatch):
    monkeypatch.setenv("MNEVO_LLM_API_KEY", "test-key")
    fake = ('{"merges":[],"distills":[{"hashes":["' + "a" * 64 + '"],'
            + '"content":"蒸馏稿","note":"测"}],"archives":[]}')
    monkeypatch.setattr(mb, "llm_chat", lambda *a, **k: fake)
    engine.distill(all_memories=True)
    assert engine.review_decide("distill", "dismiss") == 0
    assert _EngineStub.deleted == [] and _EngineStub.posted == []
