import json
from hashlib import sha256
from pathlib import Path

import pytest

from module.event_datamine.source import ShareCfgError, ShareCfgLoader, SourceSnapshot

REVISION = "f44b48853d48b400b92738b1f1cf6fcdf1d69169"
REPOSITORY = "AzurLaneTools/AzurLaneLuaScripts"


def snapshot(root: Path, server: str = "EN") -> SourceSnapshot:
    return SourceSnapshot(root, server, REPOSITORY, REVISION)


def declare_json_fixture(root: Path, tables: dict[str, str], server: str = "EN") -> None:
    """Создать manifest для явно производного JSON-fixture теста."""

    records = {}
    hashes = {}
    for table, text in tables.items():
        path = root / server / "sharecfgjson" / f"{table}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        records[table] = len(json.loads(text))
        hashes[path.relative_to(root).as_posix()] = sha256(path.read_bytes()).hexdigest()
    manifest = {
        "event_id": "en:1",
        "fixture_schema_version": 1,
        "kind": "derived_sharecfg_subset",
        "permitted_empty_tables": sorted(
            table for table, count in records.items() if count == 0
        ),
        "records": records,
        "sha256": hashes,
        "source": {
            "provider": "AzurLaneLuaScripts",
            "repository": REPOSITORY,
            "revision": REVISION,
            "server": server,
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def test_source_snapshot_requires_full_pinned_revision(tmp_path: Path):
    with pytest.raises(ShareCfgError, match="полный закреплённый"):
        SourceSnapshot(tmp_path, "EN", "repo", "master")


def test_direct_pg_base_rows_decode_without_lua_execution(tmp_path: Path):
    folder = tmp_path / "EN" / "sharecfg"
    folder.mkdir(parents=True)
    marker = tmp_path / "executed"
    (folder / "activity_template.lua").write_text(
        'pg.base.activity_template[7] = { id = 7, name = "safe" }\n'
        f'os.execute("touch {marker}")\n',
        encoding="utf-8",
    )

    rows = ShareCfgLoader(snapshot(tmp_path)).load_table("activity_template")

    assert rows[7]["name"] == "safe"
    assert not marker.exists()


def test_streamed_sharecfg_uses_matching_sharecfgdata_companion(tmp_path: Path):
    wrapper = tmp_path / "EN" / "sharecfg"
    data = tmp_path / "EN" / "sharecfgdata"
    wrapper.mkdir(parents=True)
    data.mkdir(parents=True)
    (wrapper / "chapter_template.lua").write_text(
        "pg.chapter_template = pg.chapter_template or {}\n"
        "pg.chapter_template.__stream__ = true\n",
        encoding="utf-8",
    )
    (data / "chapter_template.lua").write_text(
        'pg.base.chapter_template[2050001] = { id = 2050001, chapter_name = "A1" }\n',
        encoding="utf-8",
    )

    assert (
        ShareCfgLoader(snapshot(tmp_path)).load_table("chapter_template")[2050001][
            "chapter_name"
        ]
        == "A1"
    )


def test_streamed_sharecfg_without_companion_is_structured_unsupported(tmp_path: Path):
    wrapper = tmp_path / "EN" / "sharecfg"
    wrapper.mkdir(parents=True)
    (wrapper / "chapter_template.lua").write_text(
        "pg.chapter_template.__stream__ = true\n", encoding="utf-8"
    )

    with pytest.raises(ShareCfgError) as caught:
        ShareCfgLoader(snapshot(tmp_path)).load_table("chapter_template")

    assert caught.value.code == "stream_companion_missing"
    assert caught.value.table == "chapter_template"


def test_loader_instances_do_not_leak_source_roots(tmp_path: Path):
    roots = []
    for name, value in (("one", 1), ("two", 2)):
        root = tmp_path / name
        folder = root / "EN" / "sharecfg"
        folder.mkdir(parents=True)
        (folder / "activity_template.lua").write_text(
            f"pg.base.activity_template[{value}] = {{ id = {value} }}\n",
            encoding="utf-8",
        )
        roots.append(root)

    first = ShareCfgLoader(snapshot(roots[0]))
    second = ShareCfgLoader(snapshot(roots[1]))
    assert set(first.load_table("activity_template")) == {1}
    assert set(second.load_table("activity_template")) == {2}


def test_manifest_explicitly_allows_empty_json_fixture(tmp_path: Path):
    declare_json_fixture(tmp_path, {"map_event_list": "{}"})

    assert ShareCfgLoader(snapshot(tmp_path)).load_table("map_event_list") == {}


def test_empty_json_fixture_requires_explicit_manifest_permission(tmp_path: Path):
    declare_json_fixture(tmp_path, {"map_event_list": "{}"})
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["permitted_empty_tables"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ShareCfgError) as caught:
        ShareCfgLoader(snapshot(tmp_path)).load_table("map_event_list")

    assert caught.value.code == "fixture_empty_table_not_permitted"


def test_json_fixture_restores_lua_numeric_float_keys(tmp_path: Path):
    declare_json_fixture(
        tmp_path,
        {"chapter_template": '{"1": {"weights": {"1.0": -50}}}'},
    )

    row = ShareCfgLoader(snapshot(tmp_path)).load_table("chapter_template")[1]

    assert list(row["weights"]) == [1.0]


def test_sharecfgjson_without_fixture_manifest_does_not_override_lua(tmp_path: Path):
    json_folder = tmp_path / "EN" / "sharecfgjson"
    lua_folder = tmp_path / "EN" / "sharecfg"
    json_folder.mkdir(parents=True)
    lua_folder.mkdir(parents=True)
    (json_folder / "activity_template.json").write_text(
        '{"99": {"id": 99, "name": "fixture"}}', encoding="utf-8"
    )
    (lua_folder / "activity_template.lua").write_text(
        'pg.base.activity_template[7] = { id = 7, name = "source" }\n',
        encoding="utf-8",
    )

    rows = ShareCfgLoader(snapshot(tmp_path)).load_table("activity_template")

    assert set(rows) == {7}
    assert rows[7]["name"] == "source"


def test_declared_json_fixture_verifies_hash(tmp_path: Path):
    declare_json_fixture(tmp_path, {"chapter_template": '{"1": {"id": 1}}'})
    path = tmp_path / "EN" / "sharecfgjson" / "chapter_template.json"
    path.write_text('{"2": {"id": 2}}', encoding="utf-8")

    with pytest.raises(ShareCfgError) as caught:
        ShareCfgLoader(snapshot(tmp_path)).load_table("chapter_template")

    assert caught.value.code == "fixture_hash_mismatch"


def test_declared_json_fixture_verifies_source_identity(tmp_path: Path):
    declare_json_fixture(tmp_path, {"chapter_template": '{"1": {"id": 1}}'})
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"]["revision"] = "0" * 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ShareCfgError) as caught:
        ShareCfgLoader(snapshot(tmp_path))

    assert caught.value.code == "fixture_source_mismatch"
