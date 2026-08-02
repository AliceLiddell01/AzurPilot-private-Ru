"""Regression checks for the EN Operation Siren Data Logger template."""

from pathlib import Path

import cv2


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = (
    ROOT / "assets/en/os_handler/TEMPLATE_STORAGE_LOGGER_UNLOCK.png"
)
FIXTURE_DIR = ROOT / "tests/fixtures"


def _read_grayscale(path: Path):
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    assert image is not None, f"failed to load image: {path}"
    return image


def _best_match(fixture_name: str):
    template = _read_grayscale(TEMPLATE_PATH)
    fixture = _read_grayscale(FIXTURE_DIR / fixture_name)
    result = cv2.matchTemplate(fixture, template, cv2.TM_CCOEFF_NORMED)
    _, similarity, _, location = cv2.minMaxLoc(result)
    return template, similarity, location


def test_en_template_matches_current_storage_card():
    template, similarity, location = _best_match(
        "opsi_data_logger_storage_en_current.png"
    )

    assert template.shape == (40, 40)
    assert similarity >= 0.85
    assert location == (22, 21)


def test_en_template_keeps_legacy_card_compatibility():
    _, similarity, location = _best_match(
        "opsi_data_logger_storage_en_legacy.png"
    )

    assert similarity >= 0.85
    assert location == (0, 0)
