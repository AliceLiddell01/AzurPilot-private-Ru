import json
from datetime import datetime
from pathlib import Path

from dev_tools.event_datamine_build import build_current_event
from module.event_datamine.runtime_policy import load_generated_runtime_policy
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
    assert policy["runtime_policy_schema_version"] == 2
    assert policy["event_id"] == "en:51101"
    assert policy["map_evidence"]["repository"] == "wess09/AzurPilot"
    assert len(policy["map_evidence"]["revision"]) == 40
    assert len(policy["runtime_maps"]) == 13


def test_current_builder_applies_runtime_policy_without_leaking_source_icons(tmp_path: Path):
    _build(tmp_path)

    event_root = tmp_path / "maps" / "en_51101"
    a1 = (event_root / "a1.py").read_text(encoding="utf-8")
    b1 = (event_root / "b1.py").read_text(encoding="utf-8")
    sp = (event_root / "sp.py").read_text(encoding="utf-8")

    assert "MAP_HAS_SIREN = True" in a1
    assert "MAP_SIREN_TEMPLATE = []" in a1
    assert "MAP_SIREN_HAS_BOSS_ICON_SMALL = True" in a1
    assert "MOVABLE_ENEMY_TURN = (2,)" in a1
    assert "emotion_qz" not in a1

    assert "MAP_SIREN_TEMPLATE = ['BonhommeRichard_BB', 'BonhommeRichard_CV']" in b1
    assert "haorenlichade_m_zhanlie" not in b1
    assert "haorenlichade_m_hangmu" not in b1

    assert "MAP_SIREN_TEMPLATE = ['BonhommeRichard_SS']" in sp
    assert "haorenlichade_m_qianting" not in sp
    assert "MAP_IS_ONE_TIME_STAGE = True" in sp
    assert "MAP_HAS_MODE_SWITCH = False" in sp


def test_siren_maps_without_runtime_policy_fail_closed(tmp_path: Path):
    empty_policy_root = tmp_path / "empty-policy"
    empty_policy_root.mkdir()
    _build(tmp_path, runtime_policy_root=empty_policy_root)

    artifact = json.loads(
        (tmp_path / "data" / "production" / "en-51101.json").read_text(
            encoding="utf-8"
        )
    )
    records = artifact["metadata"]["generated_maps"]
    siren_maps = {
        item["id"]
        for item in artifact["event_spec"]["maps"]
        if any(row.get("siren", 0) for row in item["spawn_data"])
    }
    by_id = {item["map_id"]: item for item in records}

    for map_id in siren_maps:
        assert by_id[map_id]["source_status"] == "verified"
        assert by_id[map_id]["runtime_status"] == "unsupported"
        assert by_id[map_id]["runtime_reason"] == "siren_recognition_missing"
        assert by_id[map_id]["module"] == ""

    event_root = tmp_path / "maps" / "en_51101"
    assert not (event_root / "a1.py").exists()
    assert (event_root / "extra.py").is_file()


def test_event_siren_templates_use_canonical_generated_asset_registry():
    expected = {
        "BB": "TEMPLATE_SIREN_BonhommeRichard_BB.gif",
        "CV": "TEMPLATE_SIREN_BonhommeRichard_CV.gif",
        "SS": "TEMPLATE_SIREN_BonhommeRichard_SS.gif",
    }

    for suffix, filename in expected.items():
        name = f"TEMPLATE_SIREN_BonhommeRichard_{suffix}"
        path = ROOT / "assets" / "en" / "template" / filename
        assert path.is_file()
        assert not hasattr(utils_assets, name)

        template = getattr(template_assets, name)
        assert Path(template.file).as_posix().endswith(f"assets/en/template/{filename}")
