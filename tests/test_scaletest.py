"""scaletest 离线单测：确定性生成 + 指标/校准纯函数。"""

import hashlib
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mnevo_scaletest.py"
_spec = importlib.util.spec_from_file_location("mnevo_scaletest", _SCRIPT)
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)


def test_generate_deterministic_and_unique():
    a = st.generate_dataset(n=120, seed=7, pairs_per_level=4, probes=20)
    b = st.generate_dataset(n=120, seed=7, pairs_per_level=4, probes=20)
    assert a == b, "同 seed 逐字节一致"
    contents = [it["content"] for it in a["items"]]
    assert len(set(contents)) == len(contents), "内容零重复"
    hashes = [it["content_hash"] for it in a["items"]]
    assert len(set(hashes)) == len(hashes)
    levels = {p["level"] for p in a["pairs"]}
    assert levels == {"L1", "L2", "L3"}
    assert all(p["a"] != p["b"] for p in a["pairs"])


def test_hash_follows_engine_convention():
    assert st.content_hash(" ABC 记忆 \n") == hashlib.sha256(
        "abc 记忆".encode("utf-8")).hexdigest()


def test_probes_reference_existing_hashes():
    data = st.generate_dataset(n=80, seed=1, pairs_per_level=2, probes=10)
    known = {it["content_hash"] for it in data["items"]}
    assert data["probes"]
    assert all(p["expected"] in known and p["query"].strip() for p in data["probes"])


def test_retrieval_metrics_rank_math():
    def res(h, s):
        return {"memory": {"content_hash": h}, "similarity_score": s}

    hit1 = st.retrieval_metrics([res("x", 0.9), res("y", 0.8)], "x")
    assert hit1["hit1"] and hit1["hit3"] and abs(hit1["rr"] - 1.0) < 1e-9
    rank2 = st.retrieval_metrics([res("y", 0.9), res("x", 0.8)], "x")
    assert not rank2["hit1"] and rank2["hit3"] and abs(rank2["rr"] - 0.5) < 1e-9
    miss = st.retrieval_metrics([res("y", 0.9)], "x")
    assert not miss["hit5"] and miss["rr"] == 0.0


def test_calibration_table_renders_threshold_band():
    table = st.calibration_table(
        {"L1": [0.96, 0.94], "L2": [0.87, 0.83], "L3": [0.72, 0.70]},
        [0.65, 0.60])
    assert "L1" in table and "灰带" in table and "噪声底" in table
    assert "0/2" in table, "0.90 阈值下 L2 灰带命中数如实呈现"
