import json
from pathlib import Path

from dev_tools.event_datamine_fixture import TABLES, _ints, write_fixture
from module.event_datamine.source import ShareCfgLoader


def _manifest() -> dict:
    return {
        "fixture_schema_version": 1,
        "kind": "derived_sharecfg_subset",
        "event_id": "en:test",
        "source": {
            "provider": "AzurLaneLuaScripts",
            "repository": "AzurLaneTools/AzurLaneLuaScripts",
            "revision": "a" * 40,
            "server": "EN",
        },
        "records": {table: 0 for table in TABLES},
    }


def test_write_fixture_skips_empty_mandatory_tables_but_keeps_explicit_optional(tmp_path: Path):
    write_fixture(tmp_path, {table: {} for table in TABLES}, _manifest())

    table_root = tmp_path / "EN" / "sharecfgjson"
    assert not (table_root / "chapter_template.json").exists()
    for table in ShareCfgLoader.EMPTY_JSON_TABLES:
        path = table_root / f"{table}.json"
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == {}

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "EN/sharecfgjson/chapter_template.json" not in manifest["sha256"]
    for table in ShareCfgLoader.EMPTY_JSON_TABLES:
        assert f"EN/sharecfgjson/{table}.json" in manifest["sha256"]


def test_ints_ignores_non_numeric_and_nested_non_integer_values():
    assert _ints([1, "2", {"nested": [3, "bad", {"deep": 4}]}, None]) == {1, 3, 4}
