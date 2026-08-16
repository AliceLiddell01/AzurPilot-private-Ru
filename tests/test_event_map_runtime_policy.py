import json
from datetime import datetime
from pathlib import Path

from dev_tools.event_datamine_build import build_current_event
from module.event_datamine.runtime_policy import (
    load_generated_runtime_policy,
    runtime_map_policies,
)
from module.map_detection import utils_assets
from module.template import assets as template_assets

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "event_datamine" / "current_en"
REVISION = "a6b6f81f8fdd1220ef0ad1015a362a7361eb3d91"


def _build(tmp_path: Path, **kwargs):
    return build_current_event(
        source_root=FIXTURE,
        server="EN",
        repository="AzurLaneTools/AzurLaneLuaScripts",
        revision=REVISION,
        output_root=tmp_path / "data",
        asset_root=ROOT / "assets",
        now=datetime(2026, 8, 13, 20),
        maps_output=tmp_path / "maps",
        verify_git=False,
        **kwargs,
    )


def test_current_runtime_policy_is_typed_and_matches_generated_package():
    policy = load_generated_runtime_policy(("en_51101",))

    assert policy is not None
    assert policy["runtime_policy_schema_version"] == 3
    assert policy["event_id"] == "en:51101"
    assert policy["map_evidence"]["repository"] == "wess09/AzurPilot"
    assert len(policy["map_evidence"]["revision"]) == 40

    maps = runtime_map_policies(policy)
    assert len(maps) == 13
    assert maps[2050001].boss_clear.strategy == "campaign"
    assert maps[2050004].boss_clear.strategy == "campaign"
    assert maps[2050005].boss_clear.strategy == "boss_fleet"
    assert maps[2050023].boss_clear.strategy == "boss_fleet"
    assert maps[2050041].boss_clear.strategy == "boss_fleet"


def test_current_builder_applies_runtime_policy_without_leaking_source_icons(
    tmp_path: Path,
):
    result = _build(tmp_path)

    event_root = tmp_path / "maps" / "en_51101"
    a1 = (event_root / "a1.py").read_text(encoding="utf-8")
    b1 = (event_root / "b1.py").read_text(encoding="utf-8")
    b2 = (event_root / "b2.py").read_text(encoding="utf-8")
    sp = (event_root / "sp.py").read_text(encoding="utf-8")

    assert result["runtime_map_count"] == 13

    assert "MAP_HAS_SIREN = True" in a1
    assert "MAP_SIREN_TEMPLATE = []" in a1
    assert "MAP_SIREN_HAS_BOSS_ICON_SMALL = True" in a1
    assert "MOVABLE_ENEMY_TURN = (2,)" in a1
    assert "emotion_qz" not in a1
    assert "return self.clear_boss()" in a1
    assert "fleet_boss.clear_boss()" not in a1

    assert "MAP_SIREN_TEMPLATE = ['BonhommeRichard_BB', 'BonhommeRichard_CV']" in b1
    assert "haorenlichade_m_zhanlie" not in b1
    assert "haorenlichade_m_hangmu" not in b1
    assert "return self.clear_boss()" in b1
    assert "fleet_boss.clear_boss()" not in b1

    assert "return self.fleet_boss.clear_boss()" in b2

    assert "MAP_SIREN_TEMPLATE = ['BonhommeRichard_SS']" in sp
    assert "haorenlichade_m_qianting" not in sp
    assert "MAP_IS_ONE_TIME_STAGE = True" in sp
    assert "MAP_HAS_MODE_SWITCH = False" in sp
    assert "return self.fleet_boss.clear_boss()" in sp

    assert not (event_root / "extra.py").exists()
    assert not (event_root / "extra_2050052.py").exists()


def test_maps_without_runtime_policy_fail_closed(tmp_path: Path):
    empty_policy_root = tmp_path / "empty-policy"
    empty_policy_root.mkdir()
    result = _build(
        tmp_path,
        runtime_policy_root=empty_policy_root,
    )

    artifact = json.loads(
        (
            tmp_path
            / "data"
            / "production"
            / "en-51101.json"
        ).read_text(encoding="utf-8")
    )
    records = artifact["metadata"]["generated_maps"]
    siren_maps = {
        item["id"]
        for item in artifact["event_spec"]["maps"]
        if any(
            row.get("siren", 0)
            for row in item["spawn_data"]
        )
    }
    by_id = {
        item["map_id"]: item
        for item in records
    }

    assert result["runtime_map_count"] == 0
    for map_id, record in by_id.items():
        assert record["source_status"] == "verified"
        assert record["runtime_status"] == "unsupported"
        assert record["module"] == ""
        if map_id in siren_maps:
            assert (
                record["runtime_reason"]
                == "siren_recognition_missing"
            )
        else:
            assert record["runtime_reason"] == "boss_clear_missing"

    event_root = tmp_path / "maps" / "en_51101"
    assert not any(
        path.name != "__init__.py"
        for path in event_root.glob("*.py")
    )


def test_current_artifact_marks_maps_without_boss_evidence_unsupported():
    artifact = json.loads(
        (
            ROOT
            / "module"
            / "event_datamine"
            / "data"
            / "production"
            / "en-51101.json"
        ).read_text(encoding="utf-8")
    )
    records = {
        item["map_id"]: item
        for item in artifact["metadata"]["generated_maps"]
    }

    for map_id in (2050051, 2050052):
        assert records[map_id]["source_status"] == "verified"
        assert records[map_id]["runtime_status"] == "unsupported"
        assert records[map_id]["runtime_reason"] == "boss_clear_missing"
        assert records[map_id]["module"] == ""


def test_event_siren_templates_use_canonical_generated_asset_registry():
    expected = {
        "BB": "TEMPLATE_SIREN_BonhommeRichard_BB.gif",
        "CV": "TEMPLATE_SIREN_BonhommeRichard_CV.gif",
        "SS": "TEMPLATE_SIREN_BonhommeRichard_SS.gif",
    }

    for suffix, filename in expected.items():
        name = f"TEMPLATE_SIREN_BonhommeRichard_{suffix}"
        path = (
            ROOT
            / "assets"
            / "en"
            / "template"
            / filename
        )
        assert path.is_file()
        assert not hasattr(utils_assets, name)

        template = getattr(template_assets, name)
        assert Path(template.file).as_posix().endswith(
            f"assets/en/template/{filename}"
        )
