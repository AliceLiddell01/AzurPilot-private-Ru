from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AZURSTATS = ROOT / "module/statistics/azurstats.py"


def test_azurstats_runtime_is_local_only():
    source = AZURSTATS.read_text(encoding="utf-8")

    assert "LOCAL_DB = './config/azurstats_local.db'" in source
    assert "sqlite3.connect" in source
    assert "requests." not in source
    assert "urlopen(" not in source
    assert "urllib" not in source
