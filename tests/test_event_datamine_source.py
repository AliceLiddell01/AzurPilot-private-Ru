from pathlib import Path

import pytest

from module.event_datamine.source import ShareCfgError, ShareCfgLoader, SourceSnapshot

REVISION = "f44b48853d48b400b92738b1f1cf6fcdf1d69169"


def snapshot(root: Path, server: str = "EN") -> SourceSnapshot:
    return SourceSnapshot(root, server, "AzurLaneTools/AzurLaneLuaScripts", REVISION)


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
