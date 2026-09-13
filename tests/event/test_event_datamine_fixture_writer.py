import json
from pathlib import Path

from dev_tools.event_datamine_fixture import TABLES, _ints, write_fixture

PERMITTED_EMPTY_TABLES = (
    "activity_medal_group",
    "map_event_list",
    "map_event_template",
)


def _manifest() -> dict:
    return {
        "fixture_schema_version": 1,
        "kind": "derived_sharecfg_subset",
        "event_id": "en:test",
        "permitted_empty_tables": list(PERMITTED_EMPTY_TABLES),
        "source": {
            "provider": "AzurLaneLuaScripts",
            "repository": "AzurLaneTools/AzurLaneLuaScripts",
            "revision": "a" * 40,
            "server": "EN",
        },
        "records": {table: 0 for table in TABLES},
    }


def test_write_fixture_skips_empty_mandatory_tables_but_keeps_manifest_permitted(tmp_path: Path):
    write_fixture(tmp_path, {table: {} for table in TABLES}, _manifest())

    table_root = tmp_path / "EN" / "sharecfgjson"
    assert not (table_root / "chapter_template.json").exists()
    for table in PERMITTED_EMPTY_TABLES:
        path = table_root / f"{table}.json"
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == {}

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "EN/sharecfgjson/chapter_template.json" not in manifest["sha256"]
    for table in PERMITTED_EMPTY_TABLES:
        assert f"EN/sharecfgjson/{table}.json" in manifest["sha256"]


def test_write_fixture_removes_stale_mandatory_table_on_regeneration(tmp_path: Path):
    first = {table: {} for table in TABLES}
    first["chapter_template"] = {1: {"id": 1}}
    write_fixture(tmp_path, first, _manifest())

    chapter_path = tmp_path / "EN" / "sharecfgjson" / "chapter_template.json"
    assert chapter_path.exists()

    write_fixture(tmp_path, {table: {} for table in TABLES}, _manifest())

    assert not chapter_path.exists()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "EN/sharecfgjson/chapter_template.json" not in manifest["sha256"]


def test_write_fixture_rejects_unknown_permitted_empty_table(tmp_path: Path):
    manifest = _manifest()
    manifest["permitted_empty_tables"] = ["not_a_sharecfg_table"]

    try:
        write_fixture(tmp_path, {table: {} for table in TABLES}, manifest)
    except ValueError as exc:
        assert "permitted_empty_tables" in str(exc)
    else:
        raise AssertionError("Некорректный permitted_empty_tables должен отклоняться")


def test_ints_ignores_non_numeric_and_nested_non_integer_values():
    assert _ints([1, "2", {"nested": [3, "bad", {"deep": 4}]}, None]) == {1, 3, 4}
