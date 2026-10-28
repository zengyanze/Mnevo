"""冷启动注入器测试：确定性装配/预算截断/缺失与过时处理。"""

import importlib.util
import os
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mnevo_bootstrap.py"
_spec = importlib.util.spec_from_file_location("mnevo_bootstrap", _SCRIPT)
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


def _identity_dir(tmp_path: Path, drop: str | None = None) -> Path:
    d = tmp_path / "live"
    d.mkdir(parents=True)
    for name in mb.SECTION_ORDER:
        if name == drop:
            continue
        (d / name).write_text(f"# {name}\n\n{name} 的内容行。", encoding="utf-8")
    return d


def test_section_order_and_verbatim(tmp_path):
    d = _identity_dir(tmp_path)
    files = mb.read_identity_files(d)
    profile = mb.build_profile(files)
    pos = [profile.index(f"# {n}") for n in mb.SECTION_ORDER]
    assert pos == sorted(pos), "persona→values→goals→threads 固定顺序"
    assert "values.md 的内容行。" in profile, "内容原样注入，不改写"


def test_missing_file_placeholder_keeps_building(tmp_path):
    d = _identity_dir(tmp_path, drop="goals.md")
    files = mb.read_identity_files(d)
    assert files["goals.md"] is None
    profile = mb.build_profile(files)
    assert "缺失：goals.md" in profile
    assert "threads" in profile, "缺件不阻塞其余装配"


def test_budget_truncates_at_line_boundary():
    long_text = "\n".join(f"第{i}行内容填充" for i in range(60))
    files = {n: long_text for n in mb.SECTION_ORDER}
    profile = mb.build_profile(files, budget=1200)
    assert "超预算截断" in profile
    for line in profile.splitlines():
        if line.startswith("第"):
            assert line.endswith("填充"), "不切半行"


def test_stale_threads_marker(tmp_path):
    d = _identity_dir(tmp_path)
    old = time.time() - 20 * 86400
    os.utime(d / "threads.md", (old, old))
    assert mb.threads_stale(d, stale_days=14)
    profile = mb.build_profile(mb.read_identity_files(d),
                               stale_threads=mb.threads_stale(d, 14))
    assert "活跃线程较久未更新" in profile
    again = mb.build_profile(mb.read_identity_files(d),
                             stale_threads=mb.threads_stale(d, 14))
    assert profile == again, "同一状态 → 逐字节一致（确定性）"


def test_check_mode_exit_codes(tmp_path, capsys):
    ok_dir = _identity_dir(tmp_path / "a")
    args = type("A", (), {"identity": str(ok_dir), "stale_days": 14,
                          "strict": False})()
    assert mb.cmd_check(args) == 0
    missing_dir = _identity_dir(tmp_path / "b", drop="persona.md")
    args.identity = str(missing_dir)
    assert mb.cmd_check(args) == 1
    out = capsys.readouterr().out
    assert "MISSING  persona.md" in out
