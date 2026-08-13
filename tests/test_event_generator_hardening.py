from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import dev_tools.event_datamine_build as builder
from module.event_datamine.generator import generate_map_module


@dataclass(frozen=True)
class _FakeMap:
    id: int
    chapter_name: str
    unknown_grid_types: tuple = ()
    unknown_effects: tuple = ()
    source_status: str = "verified"


@dataclass(frozen=True)
class _FakeSpec:
    maps: tuple


def _patch_builder(monkeypatch, spec, current):
    class _Compiler:
        SCHEMA_VERSION = 2

        def __init__(self, _loader):
            pass

        def compile(self, _activity_id):
            return spec

    monkeypatch.setattr(builder, "SourceSnapshot", lambda *args: object())
    monkeypatch.setattr(builder, "ShareCfgLoader", lambda _snapshot: object())
    monkeypatch.setattr(builder, "discover_major_events", lambda _loader: ())
    monkeypatch.setattr(builder, "resolve_current_candidate", lambda *args, **kwargs: current)
    monkeypatch.setattr(builder, "EventCompiler", _Compiler)
    monkeypatch.setattr(builder, "generate_map_module", lambda _map: "pass\n")


def test_builder_preflights_late_map_collision_before_any_write(tmp_path: Path, monkeypatch):
    spec = _FakeSpec((_FakeMap(1, "A"), _FakeMap(2, "B")))
    current = SimpleNamespace(activity_id=7, map_ids=(1, 2))
    _patch_builder(monkeypatch, spec, current)

    maps_output = tmp_path / "maps"
    collision = maps_output / "en_7" / "b.py"
    collision.parent.mkdir(parents=True)
    collision.write_text("reserved\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        builder.build_current_event(
            source_root=tmp_path,
            server="EN",
            repository="source",
            revision="revision",
            output_root=tmp_path / "data",
            asset_root=tmp_path,
            now=SimpleNamespace(),
            maps_output=maps_output,
            verify_git=False,
        )

    assert collision.read_text(encoding="utf-8") == "reserved\n"
    assert not (maps_output / "__init__.py").exists()
    assert not (maps_output / "en_7" / "__init__.py").exists()
    assert list((maps_output / "en_7").glob("*.py")) == [collision]
    assert not (tmp_path / "data").exists()


def test_builder_rejects_derived_duplicate_module_name_before_any_write(
    tmp_path: Path, monkeypatch
):
    spec = _FakeSpec(
        (
            _FakeMap(1, "A"),
            _FakeMap(99, "A_2"),
            _FakeMap(2, "A"),
        )
    )
    current = SimpleNamespace(activity_id=7, map_ids=(1, 99, 2))
    _patch_builder(monkeypatch, spec, current)
    maps_output = tmp_path / "maps"
    output_root = tmp_path / "data"

    with pytest.raises(ValueError, match="Неуникальное имя generated map module: a_2"):
        builder.build_current_event(
            source_root=tmp_path,
            server="EN",
            repository="source",
            revision="revision",
            output_root=output_root,
            asset_root=tmp_path,
            now=SimpleNamespace(),
            maps_output=maps_output,
            overwrite=True,
            verify_git=False,
        )

    assert not maps_output.exists()
    assert not output_root.exists()


def _map_with(token: str):
    return SimpleNamespace(
        id=1,
        chapter_name="T",
        shape="A1",
        camera_data=(),
        camera_spawn_points=(),
        portals=(),
        map_data=((token,),),
        map_data_loop=None,
        land_based=(),
        spawn_data=({"battle": 0, "boss": 1},),
        spawn_data_loop=None,
        has_story=False,
        has_fleet_step=False,
        has_ambush=False,
        has_mystery=False,
        siren_templates=(),
        movable_enemy_turns=(),
        star_requirements=(1, 2, 3),
        boss_refresh=0,
        unknown_grid_types=(),
        unknown_effects=(),
    )


def test_generator_derives_normal_movable_enemy_from_me_grid():
    movable = generate_map_module(_map_with("Me"))
    static = generate_map_module(_map_with("--"))

    assert "MAP_HAS_MOVABLE_NORMAL_ENEMY = True" in movable
    assert "MAP_HAS_MOVABLE_NORMAL_ENEMY = True" not in static
