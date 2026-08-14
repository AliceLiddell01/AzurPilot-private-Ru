"""Regression checks for the EN Operation Siren Data Logger template."""

from pathlib import Path

import cv2
import numpy as np

from module.device.screenshot import Screenshot


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = (
    ROOT / "assets/en/os_handler/TEMPLATE_STORAGE_LOGGER_UNLOCK.png"
)
FIXTURE_DIR = ROOT / "tests/fixtures"
SOURCE_RESOLUTIONS = (
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
)
FIXTURE_ORIGIN = (400, 240)


def _read_grayscale(path: Path):
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    assert image is not None, f"failed to load image: {path}"
    return image


def _read_color(path: Path):
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    assert image is not None, f"failed to load image: {path}"
    return image


def _best_match(fixture_name: str):
    template = _read_grayscale(TEMPLATE_PATH)
    fixture = _read_grayscale(FIXTURE_DIR / fixture_name)
    result = cv2.matchTemplate(fixture, template, cv2.TM_CCOEFF_NORMED)
    _, similarity, _, location = cv2.minMaxLoc(result)
    return template, similarity, location


def _normalized_best_match(fixture_name: str, source_resolution):
    template = _read_grayscale(TEMPLATE_PATH)
    fixture = _read_color(FIXTURE_DIR / fixture_name)
    fixture_height, fixture_width = fixture.shape[:2]
    origin_x, origin_y = FIXTURE_ORIGIN

    native = np.zeros((720, 1280, 3), dtype=np.uint8)
    assert origin_x + fixture_width <= native.shape[1]
    assert origin_y + fixture_height <= native.shape[0]
    native[
        origin_y:origin_y + fixture_height,
        origin_x:origin_x + fixture_width,
    ] = fixture

    source = cv2.resize(
        native,
        source_resolution,
        interpolation=cv2.INTER_CUBIC,
    )
    normalized = Screenshot.resize_screenshot_to_720p(source)
    normalized_gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
    result = cv2.matchTemplate(
        normalized_gray,
        template,
        cv2.TM_CCOEFF_NORMED,
    )
    _, similarity, _, location = cv2.minMaxLoc(result)
    return similarity, location


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


def test_en_template_survives_supported_non_native_normalization():
    fixtures = (
        ("opsi_data_logger_storage_en_current.png", (22, 21)),
        ("opsi_data_logger_storage_en_legacy.png", (0, 0)),
    )

    for fixture_name, fixture_location in fixtures:
        expected_location = (
            FIXTURE_ORIGIN[0] + fixture_location[0],
            FIXTURE_ORIGIN[1] + fixture_location[1],
        )
        for source_resolution in SOURCE_RESOLUTIONS:
            similarity, location = _normalized_best_match(
                fixture_name,
                source_resolution,
            )
            localization_error = (
                abs(location[0] - expected_location[0]),
                abs(location[1] - expected_location[1]),
            )

            assert similarity > 0.75, (
                fixture_name,
                source_resolution,
                similarity,
            )
            assert localization_error[0] <= 1, (
                fixture_name,
                source_resolution,
                location,
                expected_location,
            )
            assert localization_error[1] <= 1, (
                fixture_name,
                source_resolution,
                location,
                expected_location,
            )
