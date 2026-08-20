from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import dev_tools.event_datamine_build as builder
from module.event_datamine.generator import (
    generate_map_module,
    map_module_name,
    map_module_path,
)
from module.event_datamine.runtime_policy import SirenRecognitionPolicy
from tests.event_runtime_policy_helpers import runtime_policy as _full_runtime_policy


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
    id: str = "en:7"


def _patch_builder(monkeypatch, spec, current):
    class _Compiler:
        SCHEMA_VERSION = 2

        def __init__(self, _loader):
            pass

        def compile(self, _activity_id):
            return spec

    monkeypatch.setattr(
        builder,
        "SourceSnapshot",
        lambda *args: object(),
    )
    monkeypatch.setattr(
        builder,
        "ShareCfgLoader",
        lambda _snapshot: object(),
    )
    monkeypatch.setattr(
        builder,
        "discover_major_events",
        lambda _loader: (),
    )
    monkeypatch.setattr(
        builder,
        "resolve_current_candidate",
        lambda *args, **kwargs: current,
    )
    monkeypatch.setattr(builder, "EventCompiler", _Compiler)
    policy = SimpleNamespace(
        siren_recognition=None,
        boss_clear=object(),
        camera_calibration=object(),
        detector_calibration=object(),
        battle_plan=object(),
    )
    monkeypatch.setattr(
        builder,
        "load_generated_runtime_policy",
        lambda *args, **kwargs: {"event_id": spec.id},
    )
    monkeypatch.setattr(
        builder,
        "runtime_map_policies",
        lambda _policy: {},
    )
    monkeypatch.setattr(
        builder,
        "map_runtime_policy",
        lambda *args, **kwargs: policy,
    )
    monkeypatch.setattr(
        builder,
        "validate_runtime_template_assets",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        builder,
        "generate_map_module",
        lambda _map, **_kwargs: "pass\n",
    )


def test_builder_preflights_late_map_collision_before_any_write(
    tmp_path: Path,
    monkeypatch,
):
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
            campaign_selector="event_fixture",
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
    tmp_path: Path,
    monkeypatch,
):
    spec = _FakeSpec(
        (
            _FakeMap(1, "A"),
            _FakeMap(99, "A_2"),
            _FakeMap(2, "A"),
        )
    )
    current = SimpleNamespace(
        activity_id=7,
        map_ids=(1, 99, 2),
    )
    _patch_builder(monkeypatch, spec, current)
    maps_output = tmp_path / "maps"
    output_root = tmp_path / "data"

    with pytest.raises(
        ValueError,
        match="Неуникальное имя generated map module: a_2",
    ):
        builder.build_current_event(
            source_root=tmp_path,
            server="EN",
            campaign_selector="event_fixture",
            repository="source",
            revision="revision",
            output_root=output_root,
            asset_root=tmp_path,
            maps_output=maps_output,
            overwrite=True,
            verify_git=False,
        )

    assert not maps_output.exists()
    assert not output_root.exists()


@pytest.mark.parametrize(
    "chapter_name",
    (
        "../../x",
        "A/B",
        r"A\B",
        "A B",
        "A:B",
        "",
    ),
)
def test_map_module_name_rejects_path_and_invalid_identifier_input(
    chapter_name: str,
):
    with pytest.raises(ValueError, match="chapter_name"):
        map_module_name(chapter_name)


def test_map_module_name_keeps_deterministic_safe_normalization():
    assert map_module_name("1-1") == "campaign_1_1"
    assert map_module_name("A.1") == "a1"
    assert map_module_name("EXTRA") == "extra"


@pytest.mark.parametrize(
    "module_name",
    ("../x", "a/b", r"a\b", "a b", "1bad", ""),
)
def test_map_module_path_rejects_unsafe_module_name(
    tmp_path: Path,
    module_name: str,
):
    with pytest.raises(ValueError, match="generated map module"):
        map_module_path(tmp_path, module_name)


def test_map_module_path_stays_inside_output_root(tmp_path: Path):
    root = tmp_path / "maps"
    assert map_module_path(root, "a1") == (root / "a1.py").resolve()


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
        siren_source_icons=(),
        movable_enemy_turns=(),
        star_requirements=(1, 2, 3),
        boss_refresh=0,
        unknown_grid_types=(),
        unknown_effects=(),
    )


def _runtime_policy(
    *,
    strategy: str = "campaign",
    siren: SirenRecognitionPolicy | None = None,
):
    return _full_runtime_policy(strategy=strategy, siren=siren)


def test_generator_does_not_infer_normal_movable_enemy_from_me_grid():
    generated = generate_map_module(
        _map_with("Me"),
        runtime_policy=_runtime_policy(),
    )

    assert "MAP_HAS_MOVABLE_NORMAL_ENEMY" not in generated
    assert "MOVABLE_NORMAL_ENEMY_TURN" not in generated
    assert "MAP_HAS_MOVABLE_ENEMY = False" in generated


def test_generator_rejects_siren_map_without_runtime_recognition_policy():
    spec = _map_with("--")
    spec.spawn_data = (
        {"battle": 0, "siren": 1, "boss": 1},
    )
    spec.siren_source_icons = ("sharecfg_icon",)

    with pytest.raises(
        ValueError,
        match="не имеет проверенной runtime-policy распознавания",
    ):
        generate_map_module(
            spec,
            runtime_policy=_runtime_policy(),
        )


def test_generator_rejects_map_without_boss_runtime_policy():
    spec = _map_with("--")

    with pytest.raises(
        ValueError,
        match="проверенной runtime-policy",
    ):
        generate_map_module(spec)


def test_generator_keeps_source_icon_separate_from_runtime_template():
    spec = _map_with("--")
    spec.spawn_data = (
        {"battle": 0, "siren": 1, "boss": 1},
    )
    spec.siren_source_icons = ("sharecfg_icon",)
    spec.movable_enemy_turns = (2,)
    policy = _runtime_policy(
        siren=SirenRecognitionPolicy(
            ("RuntimeTemplate",),
            False,
        )
    )

    generated = generate_map_module(
        spec,
        runtime_policy=policy,
    )

    assert "MAP_HAS_SIREN = True" in generated
    assert "MAP_SIREN_TEMPLATE = ['RuntimeTemplate']" in generated
    assert "sharecfg_icon" not in generated
    assert "MAP_HAS_MOVABLE_ENEMY = True" in generated
    assert "MOVABLE_ENEMY_TURN = (2,)" in generated


def test_generator_does_not_infer_boss_strategy_from_battle_number():
    spec = _map_with("--")
    spec.boss_refresh = 5

    campaign = generate_map_module(
        spec,
        runtime_policy=_runtime_policy(strategy="campaign"),
    )
    boss_fleet = generate_map_module(
        spec,
        runtime_policy=_runtime_policy(strategy="boss_fleet"),
    )

    assert "def battle_5(self):" in campaign
    assert "return self.clear_boss()" in campaign
    assert "fleet_boss.clear_boss()" not in campaign

    assert "def battle_5(self):" in boss_fleet
    assert "return self.fleet_boss.clear_boss()" in boss_fleet


def test_builder_overwrite_removes_only_stale_generated_modules(
    tmp_path: Path,
):
    event_root = tmp_path / "maps" / "en_7"
    event_root.mkdir(parents=True)
    keep = event_root / "a.py"
    stale = event_root / "extra.py"
    marker = event_root / "__init__.py"
    keep.write_text("keep\n", encoding="utf-8")
    stale.write_text("stale\n", encoding="utf-8")
    marker.write_text("marker\n", encoding="utf-8")

    builder._remove_stale_generated_modules(
        event_root.resolve(),
        {keep.resolve()},
    )

    assert keep.is_file()
    assert marker.is_file()
    assert not stale.exists()
