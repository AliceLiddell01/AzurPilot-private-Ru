"""Benchmark high-resolution -> 1280x720 screenshot normalization.

This utility never changes production behavior. It compares a fixed set of
normalization candidates on the same repository assets and fixtures.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2
from module.device.screenshot import Screenshot
from module.ocr.ocr import Digit, Ocr
from module.os.assets import MAP_NAME, MEOWFFICER_SEARCHING_PERCENTAGE, ZONE_DANGEROUS, ZONE_SAFE
from module.os_handler.assets import ACTION_POINT_REMAIN_OS, MAP_WORLD


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SIZE = (1280, 720)
SOURCE_RESOLUTIONS = ((1600, 900), (1920, 1080), (2560, 1440), (3840, 2160))
EMBED_ORIGIN = (360, 220)
DEFAULT_TIMING_ITERATIONS = 12

BASELINE_CURRENT = "BASELINE_CURRENT"
INTER_CUBIC_ONLY = "INTER_CUBIC_ONLY"
INTER_AREA_ONLY = "INTER_AREA_ONLY"
INTER_AREA_CURRENT_BLUR = "INTER_AREA_CURRENT_BLUR"
INTER_LINEAR_ONLY = "INTER_LINEAR_ONLY"
INTER_LANCZOS4_ONLY = "INTER_LANCZOS4_ONLY"


BUTTON_ASSETS = {
    "zone_dangerous": ZONE_DANGEROUS,
    "zone_safe": ZONE_SAFE,
    "tiny_meowfficer_percentage": MEOWFFICER_SEARCHING_PERCENTAGE,
    "map_world": MAP_WORLD,
    "get_items_1": GET_ITEMS_1,
    "get_items_2": GET_ITEMS_2,
    "map_name": MAP_NAME,
    "action_point_os": ACTION_POINT_REMAIN_OS,
}

DIRECT_ASSETS = {
    "data_logger_current": ROOT / "tests/fixtures/opsi_data_logger_storage_en_current.png",
    "data_logger_legacy": ROOT / "tests/fixtures/opsi_data_logger_storage_en_legacy.png",
    "data_logger_template": ROOT / "assets/en/os_handler/TEMPLATE_STORAGE_LOGGER_UNLOCK.png",
}


@dataclass(frozen=True)
class DetectorCase:
    name: str
    fixture_ref: str
    correct_ref: str
    threshold: float
    expected_fixture_location: tuple[int, int] = (0, 0)
    wrong_ref: str | None = None


DETECTOR_CASES = (
    DetectorCase(
        "data_logger_current",
        "data_logger_current",
        "data_logger_template",
        0.75,
        expected_fixture_location=(22, 21),
    ),
    DetectorCase(
        "data_logger_legacy",
        "data_logger_legacy",
        "data_logger_template",
        0.75,
        expected_fixture_location=(0, 0),
    ),
    DetectorCase(
        "zone_dangerous_vs_safe",
        "zone_dangerous",
        "zone_dangerous",
        0.65,
        wrong_ref="zone_safe",
    ),
    DetectorCase(
        "zone_safe_vs_dangerous",
        "zone_safe",
        "zone_safe",
        0.65,
        wrong_ref="zone_dangerous",
    ),
    DetectorCase(
        "tiny_meowfficer_percentage",
        "tiny_meowfficer_percentage",
        "tiny_meowfficer_percentage",
        0.75,
    ),
    DetectorCase(
        "localization_map_world",
        "map_world",
        "map_world",
        0.75,
    ),
    DetectorCase(
        "get_items_1_vs_2",
        "get_items_1",
        "get_items_1",
        0.75,
        wrong_ref="get_items_2",
    ),
)


@dataclass(frozen=True)
class OcrCase:
    name: str
    fixture_ref: str
    kind: str
    letter: tuple[int, int, int]
    threshold: int
    semantic_name: str


OCR_CASES = (
    OcrCase(
        "opsi_map_name",
        "map_name",
        "text",
        (206, 223, 247),
        96,
        "OCR_OS_MAP_NAME",
    ),
    OcrCase(
        "action_point_os",
        "action_point_os",
        "digit",
        (239, 239, 239),
        160,
        "OCR_SHOP_YELLOW_COINS_OS",
    ),
)


def _asset_image(ref: str) -> np.ndarray:
    """Load all benchmark assets into the production RGB semantic contract."""
    if ref in DIRECT_ASSETS:
        path = DIRECT_ASSETS[ref]
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise FileNotFoundError(f"failed to load legacy fixture: {path}")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    else:
        button = BUTTON_ASSETS[ref]
        button.ensure_template()
        image = button.image
    if isinstance(image, list):
        raise TypeError(f"animated assets are not supported in this benchmark: {ref}")
    array = np.asarray(image)
    if array.ndim == 2:
        array = cv2.cvtColor(array, cv2.COLOR_GRAY2RGB)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError(f"unsupported image shape for {ref}: {array.shape}")
    return np.ascontiguousarray(array.copy())


def _resize_only(image: np.ndarray, interpolation: int) -> np.ndarray:
    return cv2.resize(image, CANONICAL_SIZE, interpolation=interpolation)


def _current_blur(image: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(image, (0, 0), sigmaX=1.0, sigmaY=1.0)
    return cv2.addWeighted(image, 0.90, blur, 0.10, 0)


def _area_current_blur(image: np.ndarray) -> np.ndarray:
    return _current_blur(_resize_only(image, cv2.INTER_AREA))


CANDIDATES: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    BASELINE_CURRENT: Screenshot.resize_screenshot_to_720p,
    INTER_CUBIC_ONLY: lambda image: _resize_only(image, cv2.INTER_CUBIC),
    INTER_AREA_ONLY: lambda image: _resize_only(image, cv2.INTER_AREA),
    INTER_AREA_CURRENT_BLUR: _area_current_blur,
    INTER_LINEAR_ONLY: lambda image: _resize_only(image, cv2.INTER_LINEAR),
    INTER_LANCZOS4_ONLY: lambda image: _resize_only(image, cv2.INTER_LANCZOS4),
}


def _embed_fixture(fixture: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    x, y = EMBED_ORIGIN
    height, width = fixture.shape[:2]
    native = np.zeros((CANONICAL_SIZE[1], CANONICAL_SIZE[0], 3), dtype=np.uint8)
    if x + width > native.shape[1] or y + height > native.shape[0]:
        raise ValueError(f"fixture is too large for canonical frame: {fixture.shape}")
    native[y:y + height, x:x + width] = fixture
    return native, EMBED_ORIGIN


def _synthetic_source(native: np.ndarray, resolution: tuple[int, int]) -> np.ndarray:
    return cv2.resize(native, resolution, interpolation=cv2.INTER_CUBIC)


def _match(image: np.ndarray, template: np.ndarray) -> tuple[float, tuple[int, int]]:
    result = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
    _, similarity, _, location = cv2.minMaxLoc(result)
    return float(similarity), (int(location[0]), int(location[1]))


def _localization_error(location: tuple[int, int], expected: tuple[int, int]) -> float:
    return float(np.linalg.norm(np.subtract(location, expected)))


def _detector_row(
    candidate: str,
    resolution: tuple[int, int],
    case: DetectorCase,
    image: np.ndarray,
    expected: tuple[int, int],
) -> dict:
    correct_template = _asset_image(case.correct_ref)
    correct, location = _match(image, correct_template)
    wrong = None
    if case.wrong_ref is not None:
        wrong, _ = _match(image, _asset_image(case.wrong_ref))
    loc_error = _localization_error(location, expected)
    return {
        "candidate": candidate,
        "resolution": list(resolution),
        "case": case.name,
        "correct_similarity": correct,
        "wrong_similarity": wrong,
        "margin": None if wrong is None else correct - wrong,
        "threshold": case.threshold,
        "threshold_margin": correct - case.threshold,
        "match_location": list(location),
        "expected_location": list(expected),
        "localization_error_px": loc_error,
        "pass": bool(
            correct > case.threshold
            and loc_error <= 1.0
            and (wrong is None or correct > wrong)
        ),
    }


def detector_reference_rows() -> list[dict]:
    rows = []
    for case in DETECTOR_CASES:
        native, origin = _embed_fixture(_asset_image(case.fixture_ref))
        expected = (
            origin[0] + case.expected_fixture_location[0],
            origin[1] + case.expected_fixture_location[1],
        )
        rows.append(_detector_row("NATIVE_REFERENCE", CANONICAL_SIZE, case, native, expected))
    return rows


def detector_candidate_rows() -> list[dict]:
    rows = []
    for case in DETECTOR_CASES:
        native, origin = _embed_fixture(_asset_image(case.fixture_ref))
        expected = (
            origin[0] + case.expected_fixture_location[0],
            origin[1] + case.expected_fixture_location[1],
        )
        sources = {
            resolution: _synthetic_source(native, resolution)
            for resolution in SOURCE_RESOLUTIONS
        }
        for candidate, normalizer in CANDIDATES.items():
            for resolution, source in sources.items():
                rows.append(
                    _detector_row(
                        candidate,
                        resolution,
                        case,
                        normalizer(source),
                        expected,
                    )
                )
    return rows


def _ocr_value(image: np.ndarray, case: OcrCase, area: tuple[int, int, int, int]):
    recognizer_class = Ocr if case.kind == "text" else Digit
    recognizer = recognizer_class(
        area,
        lang="azur_lane",
        letter=case.letter,
        threshold=case.threshold,
        name=case.semantic_name,
    )
    value = recognizer.ocr(image)
    return str(value).strip() if case.kind == "text" else int(value)


def ocr_rows() -> tuple[list[dict], list[dict]]:
    references = []
    rows = []
    for case in OCR_CASES:
        fixture = _asset_image(case.fixture_ref)
        native, origin = _embed_fixture(fixture)
        height, width = fixture.shape[:2]
        area = (origin[0], origin[1], origin[0] + width, origin[1] + height)
        expected = _ocr_value(native, case, area)
        native_valid = bool(expected) if case.kind == "text" else isinstance(expected, int)
        references.append(
            {
                "case": case.name,
                "kind": case.kind,
                "expected_native_semantic": expected,
                "native_valid": native_valid,
            }
        )
        sources = {
            resolution: _synthetic_source(native, resolution)
            for resolution in SOURCE_RESOLUTIONS
        }
        for candidate, normalizer in CANDIDATES.items():
            for resolution, source in sources.items():
                actual = _ocr_value(normalizer(source), case, area)
                rows.append(
                    {
                        "candidate": candidate,
                        "resolution": list(resolution),
                        "case": case.name,
                        "expected_native_semantic": expected,
                        "actual_semantic": actual,
                        "pass": bool(native_valid and actual == expected),
                    }
                )
    return references, rows


def timing_rows(iterations: int = DEFAULT_TIMING_ITERATIONS) -> list[dict]:
    if iterations < 3:
        raise ValueError("timing iterations must be >= 3")
    rng = np.random.default_rng(20260815)
    rows = []
    for resolution in SOURCE_RESOLUTIONS:
        width, height = resolution
        source = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
        for candidate, normalizer in CANDIDATES.items():
            for _ in range(3):
                normalizer(source)
            samples = []
            for _ in range(iterations):
                started = time.perf_counter()
                normalizer(source)
                samples.append((time.perf_counter() - started) * 1000.0)
            ordered = sorted(samples)
            p95_index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
            rows.append(
                {
                    "candidate": candidate,
                    "resolution": list(resolution),
                    "iterations": iterations,
                    "median_ms": float(statistics.median(samples)),
                    "p95_ms": float(ordered[p95_index]),
                }
            )
    return rows


def summarize(detector_rows: list[dict], ocr_measurements: list[dict], timings: list[dict]) -> dict:
    summary = {}
    for candidate in CANDIDATES:
        detector = [row for row in detector_rows if row["candidate"] == candidate]
        margins = [row["margin"] for row in detector if row["margin"] is not None]
        ocr = [row for row in ocr_measurements if row["candidate"] == candidate]
        timing_4k = next(
            row for row in timings
            if row["candidate"] == candidate and row["resolution"] == [3840, 2160]
        )
        summary[candidate] = {
            "min_correct_similarity": min(row["correct_similarity"] for row in detector),
            "min_threshold_margin": min(row["threshold_margin"] for row in detector),
            "min_correct_vs_wrong_margin": min(margins) if margins else None,
            "max_localization_error_px": max(row["localization_error_px"] for row in detector),
            "detector_pass_count": sum(bool(row["pass"]) for row in detector),
            "detector_total": len(detector),
            "ocr_pass_count": sum(bool(row["pass"]) for row in ocr),
            "ocr_total": len(ocr),
            "median_4k_ms": timing_4k["median_ms"],
            "p95_4k_ms": timing_4k["p95_ms"],
        }
    return summary


def run_benchmark(timing_iterations: int = DEFAULT_TIMING_ITERATIONS) -> dict:
    native_detector = detector_reference_rows()
    detector = detector_candidate_rows()
    ocr_reference, ocr = ocr_rows()
    timings = timing_rows(timing_iterations)
    return {
        "metadata": {
            "canonical_resolution": list(CANONICAL_SIZE),
            "source_resolutions": [list(value) for value in SOURCE_RESOLUTIONS],
            "channel_contract": (
                "Generated Button assets use production Button.ensure_template RGB semantics; "
                "legacy Data Logger fixtures are decoded with OpenCV then converted BGR->RGB; "
                "baseline and all candidates receive identical RGB arrays."
            ),
            "synthetic_renderer": "cv2.INTER_CUBIC upscale from canonical fixture frame",
            "performance_note": "informational only; CI hardware timing is not a gate",
            "candidate_order": list(CANDIDATES),
            "detector_cases": [
                {
                    "name": case.name,
                    "fixture_ref": case.fixture_ref,
                    "correct_ref": case.correct_ref,
                    "wrong_ref": case.wrong_ref,
                    "threshold": case.threshold,
                    "expected_fixture_location": list(case.expected_fixture_location),
                }
                for case in DETECTOR_CASES
            ],
            "ocr_cases": [
                {
                    "name": case.name,
                    "fixture_ref": case.fixture_ref,
                    "kind": case.kind,
                    "semantic_name": case.semantic_name,
                }
                for case in OCR_CASES
            ],
        },
        "native_detector_reference": native_detector,
        "detector_measurements": detector,
        "ocr_native_reference": ocr_reference,
        "ocr_measurements": ocr,
        "timings": timings,
        "summary": summarize(detector, ocr, timings),
        "four_k_detector_rows": [
            row for row in detector if row["resolution"] == [3840, 2160]
        ],
        "four_k_ocr_rows": [
            row for row in ocr if row["resolution"] == [3840, 2160]
        ],
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--timing-iterations", type=int, default=DEFAULT_TIMING_ITERATIONS)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    payload = run_benchmark(args.timing_iterations)
    print(json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
