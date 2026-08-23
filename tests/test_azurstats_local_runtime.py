from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AZURSTATS = ROOT / "module/statistics/azurstats.py"


def test_azurstats_runtime_uses_application_storage_only():
    source = AZURSTATS.read_text(encoding="utf-8")

    assert "get_runtime_storage" in source
    assert "sqlite3.connect" not in source
    assert "azurstats_local.db" not in source
    assert "requests." not in source
    assert "urlopen(" not in source
    assert "urllib" not in source
