from datetime import datetime
from pathlib import Path

from dev_tools.event_datamine_build import build_current_event
from module.map_detection import utils_assets

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "event_datamine" / "current_en"
REVISION = "a6b6f81f8fdd1220ef0ad1015a362a7361eb3d91"


def test_current_builder_applies_verified_runtime_siren_patches(tmp_path: Path):
    build_current_event(
        source_root=FIXTURE,
        server="EN",
        repository="AzurLaneTools/AzurLaneLuaScripts",
        revision=REVISION,
        output_root=tmp_path / "data",
        asset_root=ROOT / "assets",
        now=datetime(2026, 8, 13, 20),
        maps_output=tmp_path / "maps",
        verify_git=False,
    )

    event_root = tmp_path / "maps" / "en_51101"
    a1 = (event_root / "a1.py").read_text(encoding="utf-8")
    b1 = (event_root / "b1.py").read_text(encoding="utf-8")
    sp = (event_root / "sp.py").read_text(encoding="utf-8")

    assert "MAP_HAS_SIREN = True" in a1
    assert "MAP_SIREN_TEMPLATE = []" in a1
    assert "MAP_SIREN_HAS_BOSS_ICON_SMALL = True" in a1
    assert "MOVABLE_ENEMY_TURN = (2,)" in a1
    assert "Siren_emotion_qz" not in a1

    assert "MAP_SIREN_TEMPLATE = ['BonhommeRichard_BB', 'BonhommeRichard_CV']" in b1
    assert "MAP_SIREN_TEMPLATE = ['BonhommeRichard_SS']" in sp


def test_current_event_bonhomme_richard_templates_are_registered():
    expected = {
        "BB": "TEMPLATE_SIREN_BonhommeRichard_BB.gif",
        "CV": "TEMPLATE_SIREN_BonhommeRichard_CV.gif",
        "SS": "TEMPLATE_SIREN_BonhommeRichard_SS.gif",
    }

    for suffix, filename in expected.items():
        path = ROOT / "assets" / "en" / "template" / filename
        assert path.is_file()

        template = getattr(utils_assets, f"TEMPLATE_SIREN_BonhommeRichard_{suffix}")
        assert Path(template.file).as_posix().endswith(f"assets/en/template/{filename}")
