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


# ---------- PAM v1.0 映射 ----------

def _load_pack_module():
    from pathlib import Path as _P
    spec = importlib.util.spec_from_file_location(
        "mnevo_pack", _P(__file__).resolve().parents[1] / "scripts" / "mnevo_pack.py")
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)
    return mp


def test_pam_normalize_and_hash():
    mp = _load_pack_module()
    assert mp.pam_normalize("  Hello   WORLD  ") == "hello world"
    assert mp.pam_content_hash("AbC  d") == mp.pam_content_hash("abc d")
    assert mp.pam_content_hash("x").startswith("sha256:")


def test_pam_tag_sanitize():
    mp = _load_pack_module()
    out = mp._pam_tags(["Work", "agent:pi", "agent:pi", "_bad", "中文"])
    assert out == ["work", "agent-pi"]


def test_to_pam_roundtrip(tmp_path):
    mp = _load_pack_module()
    rows = [
        {"content": "记忆一 content", "content_hash": "h1",
         "tags": ["work", "skill"], "memory_type": None,
         "created_at_iso": "2026-10-26T10:00:00Z", "updated_at_iso": None},
        {"content": "memory two", "content_hash": "h2",
         "tags": ["agent:pi"], "memory_type": None,
         "created_at_iso": "2026-10-26T11:00:00Z", "updated_at_iso": None},
    ]
    mp.write_pack(tmp_path / "pack", rows, [], "work", "test")
    store = mp.build_pam_store(rows, "work", export_id="id-1")
    assert store["schema"] == "portable-ai-memory"
    assert store["integrity"]["total_memories"] == 2
    # type 映射：含 skill tag 的条目应为 skill
    by_content = {m["content"]: m for m in store["memories"]}
    assert by_content["记忆一 content"]["type"] == "skill"
    assert by_content["memory two"]["tags"] == ["agent-pi"]
    # 写盘读回：校验和通过
    mp.write_pam(store, tmp_path / "pam")
    store2, mems = mp.read_pam(tmp_path / "pam")
    assert len(mems) == 2
    # 逆向：active 条目回 rows，内容/标签保真
    back = mp.pam_to_rows(mems)
    assert {r["content"] for r in back} == {"记忆一 content", "memory two"}
    # 篡改检测
    store3 = json.loads(json.dumps(store))
    store3["memories"][0]["content"] = "tampered"
    mp.write_pam(store3, tmp_path / "pam3")
    try:
        mp.read_pam(tmp_path / "pam3")
        assert False, "should raise"
    except ValueError as e:
        assert "integrity failure" in str(e)


# ---------------------------------------------- from-claude / to-gemini（v0.2）


def _claude_zip(tmp: Path, nested: bool = False) -> Path:
    """构造三代格式合体的 Claude 导出 zip（含嵌套目录选项）。"""
    import zipfile
    memories = [{
        "account_uuid": "acc-1",
        "memory_files": [
            {"path": "/profile.md", "content": "Sam, 31, pediatric nurse.",
             "updated_at": "2026-08-01T10:12:00Z"},
            {"path": "/people/jane.md",
             "content": "段落一。\n\n" + ("很长的记忆。" * 120) + "\n\n段落末尾。",
             "updated_at": "2026-08-02T10:12:00Z"},
        ],
        "conversations_memory": "User prefers concise answers.",
        "project_memories": {"p1": "项目 p1 的记忆。"},
    }]
    projects = [{"uuid": "p1", "name": "My Project"}]
    zp = tmp / "claude-export.zip"
    prefix = "data/" if nested else ""
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr(f"{prefix}memories.json", json.dumps(memories))
        zf.writestr(f"{prefix}projects.json", json.dumps(projects))
    return zp


def test_from_claude_zip(tmp_path):
    rows, stats = mp.parse_claude_export(_claude_zip(tmp_path))
    assert stats["memory_files"] == 2 and stats["legacy"] == 1
    assert stats["projects"] == 1 and stats["project_names"] == 1
    # 短文件 1 行；长文件切块≥2；legacy 1 行；项目 1 行
    assert len(rows) >= 5
    tags_all = [t for r in rows for t in r["tags"]]
    assert "claude-file:profile.md" in tags_all
    assert "claude-file:jane.md" in tags_all
    assert "claude:conversations-memory" in tags_all
    assert "claude-project:My Project" in tags_all
    # 长文件无损：拼接全部 [part] 块 ≈ 原文（去掉 part 头）
    jane = [r for r in rows if "claude-file:jane.md" in r["tags"]]
    assert len(jane) >= 2
    joined = "\n\n".join(
        r["content"].split("]\n", 1)[1] for r in jane)
    assert "段落一" in joined and "段落末尾" in joined
    # hash 口径 = sha256(content)
    for r in rows:
        assert r["content_hash"] == mp.hashlib.sha256(
            r["content"].encode("utf-8")).hexdigest()
    # 成包可往返
    mp.write_pack(tmp_path / "pk", rows, [], "personal", "claude.ai-export")
    manifest, back = mp.read_pack(tmp_path / "pk")
    assert len(back) == len(rows)
    assert manifest["namespace"] == "personal"


def test_from_claude_nested_and_legacy_only(tmp_path):
    # 嵌套目录 zip 同样能找到
    rows, stats = mp.parse_claude_export(_claude_zip(tmp_path, nested=True))
    assert stats["memory_files"] == 2
    # 裸 json（最老一代：仅 project_memories）
    bare = tmp_path / "memories.json"
    bare.write_text(json.dumps(
        [{"project_memories": {"x": "旧项目记忆"}, "account_uuid": "a"}]),
        encoding="utf-8")
    rows2, stats2 = mp.parse_claude_export(bare)
    assert len(rows2) == 1 and stats2["memory_files"] == 0
    assert rows2[0]["tags"] == ["claude", "claude-project:x"]


def test_from_claude_empty(tmp_path):
    bare = tmp_path / "memories.json"
    bare.write_text(json.dumps([{"account_uuid": "a"}]), encoding="utf-8")
    rows, _ = mp.parse_claude_export(bare)
    assert rows == []


def test_to_gemini(tmp_path):
    rows = [
        {"content": "偏好直接了当的回答", "content_hash": "h1",
         "tags": ["test"], "memory_type": "preference",
         "created_at_iso": None, "updated_at_iso": None},
        {"content": "在忙 Mnevo 记忆系统", "content_hash": "h2",
         "tags": ["test"], "memory_type": "goal",
         "created_at_iso": None, "updated_at_iso": None},
        {"content": "普通一条备注", "content_hash": "h3",
         "tags": ["test"], "memory_type": None,
         "created_at_iso": None, "updated_at_iso": None},
    ]
    mp.write_pack(tmp_path / "pk", rows, [], "personal", "t")
    (tmp_path / "pk" / "identity").mkdir(exist_ok=True)
    (tmp_path / "pk" / "identity" / "persona.md").write_text(
        "工程师，实用主义", encoding="utf-8")
    # 重写 manifest 计入 identity 文件
    manifest, _ = mp.read_pack(tmp_path / "pk")
    text = mp.build_gemini_text(
        mp.read_pack(tmp_path / "pk")[1],
        {"persona.md": "工程师，实用主义"})
    assert "# About me" in text
    assert "## Who I am" in text and "工程师，实用主义" in text
    assert "## Preferences" in text and "偏好直接了当" in text
    assert "## Current goals" in text and "Mnevo" in text
    assert "## Notes" in text and "普通一条备注" in text
    assert manifest["format"] == "mnevo.mempack"


# ---------------- v2 谱系字段（PLAN §4.4：谱系优先于破坏性合并） ----------------

def test_pack_row_lineage_projection():
    engine_mem = {
        "content": "偏好 X 后改 Y（合并稿）",
        "content_hash": "h2",
        "tags": ["work", "distilled"],
        "memory_type": None,
        "metadata": {"mnevo_valid_from": "2026-10-01T00:00:00Z",
                     "mnevo_supersedes": ["a" * 64, "b" * 64],
                     "access_count": 9},          # 遥测仍不入包
        "created_at_iso": "2026-10-01T00:00:00Z",
        "updated_at_iso": "2026-10-01T00:00:00Z",
    }
    row = mp.pack_row(engine_mem)
    assert row["valid_from"] == "2026-10-01T00:00:00Z"
    assert row["supersedes"] == ["a" * 64, "b" * 64]
    assert "valid_to" not in row and "metadata" not in row
    plain = mp.pack_row({**engine_mem, "metadata": {}})
    assert "valid_from" not in plain and "supersedes" not in plain


def test_pack_v2_roundtrip_and_v1_backcompat(tmp_path):
    rows = _rows(1)
    rows[0]["valid_from"] = "2026-10-01T00:00:00Z"
    rows[0]["supersedes"] = ["a" * 64]
    pack = mp.write_pack(tmp_path / "v2.mempack", rows, [], "work", "t")
    manifest, back = mp.read_pack(pack)
    assert manifest["format_version"] == 2
    assert back[0]["supersedes"] == ["a" * 64]

    mpath = pack / "manifest.json"                     # v1 备份仍可读（升级路径）
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m["format_version"] = 1
    mpath.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    manifest1, rows1 = mp.read_pack(pack)
    assert manifest1["format_version"] == 1 and rows1 == back

    m["format_version"] = 99                           # 未知版本拒读
    mpath.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported pack version"):
        mp.read_pack(pack)


def test_read_pack_rejects_bad_lineage_types(tmp_path):
    d = tmp_path / "bad.mempack"
    d.mkdir()
    bad = {"content": "x", "content_hash": "h" * 64, "tags": [],
           "memory_type": None, "created_at_iso": None,
           "updated_at_iso": None, "supersedes": "notaList"}
    (d / "memories.jsonl").write_text(
        json.dumps(bad, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = {"format": "mnevo.mempack", "format_version": 2,
                "files": {"memories.jsonl":
                          mp._sha256_bytes((d / "memories.jsonl").read_bytes())}}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="supersedes"):
        mp.read_pack(d)


def test_post_memory_restores_lineage_metadata(stub_server):
    row = {"content": "谱系记忆", "content_hash": "l" * 64, "tags": [],
           "valid_from": "2026-10-01T00:00:00Z", "supersedes": ["a" * 64]}
    outcome, _, resp = mp.post_memory_full(stub_server, "t", row)
    assert outcome == "created" and resp.get("content_hash")
    body = _StubHandler.posts[-1]["body"]
    assert body["metadata"] == {"mnevo_valid_from": "2026-10-01T00:00:00Z",
                                "mnevo_supersedes": ["a" * 64]}
