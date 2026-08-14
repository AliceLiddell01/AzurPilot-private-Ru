"""Stage 4 benchmark for high-resolution -> 1280x720 screenshot normalization.

The benchmark is intentionally separated from production behavior. Repository PNG
fixtures are loaded through OpenCV (BGR), embedded into a canonical 1280x720
frame, synthetically rendered at supported 16:9 source resolutions, and then
normalized by each bounded candidate.

Detector metrics are the primary synthetic signal. OCR is evaluated semantically
against the native 1280x720 reference on representative repository crops.
Timing is informational only and must not be used as a strict CI gate.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from module.device.screenshot import Screenshot
from module.ocr.ocr import Digit, Ocr


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SIZE = (1280, 720)
SOURCE_RESOLUTIONS = (
    (1600, 900),
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
)
EMBED_ORIGIN = (360, 220)
DEFAULT_TIMING_ITERATIONS = 12

BASELINE_CURRENT = "BASELINE_CURRENT"
INTER_CUBIC_ONLY = "INTER_CUBIC_ONLY"
INTER_AREA_ONLY = "INTER_AREA_ONLY"
INTER_AREA_CURRENT_BLUR = "INTER_AREA_CURRENT_BLUR"
INTER_LINEAR_ONLY = "INTER_LINEAR_ONLY"
INTER_LANCZOS4_ONLY = "INTER_LANCZOS4_ONLY"


@dataclass(frozen=True)
class DetectorCase:
    name: str
    fixture_path: Path
    correct_template_path: Path
    threshold: float
    expected_fixture_location: tuple[int, int] = (0, 0)
    wrong_template_path: Path | None = None


DETECTOR_CASES = (
    DetectorCase(
        name="data_logger_current",
        fixture_path=ROOT / "tests/fixtures/opsi_data_logger_storage_en_current.png",
        correct_template_path=ROOT / "assets/en/os_handler/TEMPLATE_STORAGE_LOGGER_UNLOCK.png",
        threshold=0.75,
        expected_fixture_location=(22, 21),
    ),
    DetectorCase(
        name="data_logger_legacy",
        fixture_path=ROOT / "tests/fixtures/opsi_data_logger_storage_en_legacy.png",
        correct_template_path=ROOT / "assets/en/os_handler/TEMPLATE_STORAGE_LOGGER_UNLOCK.png",
        threshold=0.75,
        expected_fixture_location=(0, 0),
    ),
    DetectorCase(
        name="zone_dangerous_vs_safe",
        fixture_path=ROOT / "assets/en/os/ZONE_DANGEROUS.png",
        correct_template_path=ROOT / "assets/en/os/ZONE_DANGEROUS.png",
        wrong_template_path=ROOT / "assets/en/os/ZONE_SAFE.png",
        threshold=0.65,
    ),
    DetectorCase(
        name="zone_safe_vs_dangerous",
        fixture_path=ROOT / "assets/en/os/ZONE_SAFE.png",
        correct_template_path=ROOT / "assets/en/os/ZONE_SAFE.png",
        wrong_template_path=ROOT / "assets/en/os/ZONE_DANGEROUS.png",
        threshold=0.65,
    ),
    DetectorCase(
        name="tiny_meowfficer_percentage",
        fixture_path=ROOT / "assets/en/os/MEOWFFICER_SEARCHING_PERCENTAGE.png",
        correct_template_path=ROOT / "assets/en/os/MEOWFFICER_SEARCHING_PERCENTAGE.png",
        threshold=0.75,
    ),
    DetectorCase(
        name="localization_map_world",
        fixture_path=ROOT / "assets/en/os_handler/MAP_WORLD.png",
        correct_template_path=ROOT / "assets/en/os_handler/MAP_WORLD.png",
        threshold=0.75,
    ),
    DetectorCase(
        name="get_items_1_vs_2",
        fixture_path=ROOT / "assets/en/combat/GET_ITEMS_1.png",
        correct_template_path=ROOT / "assets/en/combat/GET_ITEMS_1.png",
        wrong_template_path=ROOT / "assets/en/combat/GET_ITEMS_2.png",
        threshold=0.75,
    ),
)


def _read_color(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(f"failed to load image: {path}")
    return image


def _read_gray(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise FileNotFoundError(f"failed to load image: {path}")
    return image


def _resize_only(image: np.ndarray, interpolation: int) -> np.ndarray:
    return cv2.resize(image, CANONICAL_SIZE, interpolation=interpolation)


def _current_blur(image: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(image, (0, 0), sigmaX=1.0, sigmaY=1.0)
    return cv2.addWeighted(image, 0.90, blur, 0.10, 0)


def _baseline(image: np.ndarray) -> np.ndarray:
    return Screenshot.resize_screenshot_to_720p(image)


def _area_current_blur(image: np.ndarray) -> np.ndarray:
    return _current_blur(_resize_only(image, cv2.INTER_AREA))


CANDIDATES: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    BASELINE_CURRENT: _baseline,
    INTER_CUBIC_ONLY: lambda image: _resize_only(image, cv2.INTER_CUBIC),
    INTER_AREA_ONLY: lambda image: _resize_only(image, cv2.INTER_AREA),
    INTER_AREA_CURRENT_BLUR: _area_current_blur,
    INTER_LINEAR_ONLY: lambda image: _resize_only(image, cv2.INTER_LINEAR),
    INTER_LANCZOS4_ONLY: lambda image: _resize_only(image, cv2.INTER_LANCZOS4),
}


def _embed_fixture(fixture: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    origin_x, origin_y = EMBED_ORIGIN
    fixture_height, fixture_width = fixture.shape[:2]
    native = np.zeros((CANONICAL_SIZE[1], CANONICAL_SIZE[0], 3), dtype=np.uint8)
    if origin_x + fixture_width > native.shape[1]:
        raise ValueError(f"fixture is too wide for canonical frame: {fixture.shape}")
    if origin_y + fixture_height > native.shape[0]:
        raise ValueError(f"fixture is too tall for canonical frame: {fixture.shape}")
    native[
        origin_y:origin_y + fixture_height,
        origin_x:origin_x + fixture_width,
    ] = fixture
    return native, EMBED_ORIGIN


def _synthetic_source(native: np.ndarray, resolution: tuple[int, int]) -> np.ndarray:
    return cv2.resize(native, resolution, interpolation=cv2.INTER_CUBIC)


def _match(image: np.ndarray, template_path: Path) -> tuple[float, tuple[int, int]]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    template = _read_gray(template_path)
    result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
    _, similarity, _, location = cv2.minMaxLoc(result)
    return float(similarity), (int(location[0]), int(location[1]))


def _localization_error(
    location: tuple[int, int],
    expected: tuple[int, int],
) -> float:
    dx = location[0] - expected[0]
    dy = location[1] - expected[1]
    return float((dx * dx + dy * dy) ** 0.5)


def detector_reference_rows() -> list[dict]:
    """Native 1280x720 reference; production bypasses the high-res resizer."""
    rows: list[dict] = []
    for case in DETECTOR_CASES:
        fixture = _read_color(case.fixture_path)
        native, origin = _embed_fixture(fixture)
        expected = (
            origin[0] + case.expected_fixture_location[0],
            origin[1] + case.expected_fixture_location[1],
        )
        correct, location = _match(native, case.correct_template_path)
        wrong = None
        if case.wrong_template_path is not None:
            wrong, _ = _match(native, case.wrong_template_path)
        rows.append(
            {
                "candidate": "NATIVE_REFERENCE",
                "resolution": [1280, 720],
                "case": case.name,
                "correct_similarity": correct,
                "wrong_similarity": wrong,
                "margin": None if wrong is None else correct - wrong,
                "threshold": case.threshold,
                "threshold_margin": correct - case.threshold,
                "match_location": list(location),
                "expected_location": list(expected),
                "localization_error_px": _localization_error(location, expected),
                "pass": bool(
                    correct > case.threshold
                    and _localization_error(location, expected) <= 1.0
                    and (wrong is None or correct > wrong)
                ),
            }
        )
    return rows


def detector_candidate_rows() -> list[dict]:
    rows: list[dict] = []
    for case in DETECTOR_CASES:
        fixture = _read_color(case.fixture_path)
        native, origin = _embed_fixture(fixture)
        expected = (
            origin[0] + case.expected_fixture_location[0],
            origin[1] + case.expected_fixture_location[1],
        )
        sources = {
            resolution: _synthetic_source(native, resolution)
            for resolution in SOURCE_RESOLUTIONS
        }
        for candidate_name, normalizer in CANDIDATES.items():
            for resolution, source in sources.items():
                normalized = normalizer(source)
                correct, location = _match(normalized, case.correct_template_path)
                wrong = None
                if case.wrong_template_path is not None:
                    wrong, _ = _match(normalized, case.wrong_template_path)
                loc_error = _localization_error(location, expected)
                rows.append(
                    {
                        "candidate": candidate_name,
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
                )
    return rows


@dataclass(frozen=True)
class OcrCase:
    name: str
    fixture_path: Path
    kind: str
    letter: tuple[int, int, int]
    threshold: int
    semantic_name: str


OCR_CASES = (
    OcrCase(
        name="opsi_map_name",
        fixture_path=ROOT / "assets/en/os/MAP_NAME.png",
        kind="text",
        letter=(206, 223, 247),
        threshold=96,
        semantic_name="OCR_OS_MAP_NAME",
    ),
    OcrCase(
        name="action_point_os",
        fixture_path=ROOT / "assets/en/os_handler/ACTION_POINT_REMAIN_OS.png",
        kind="digit",
        letter=(239, 239, 239),
        threshold=160,
        semantic_name="OCR_SHOP_YELLOW_COINS_OS",
    ),
)


def _ocr_value(image: np.ndarray, case: OcrCase, area: tuple[int, int, int, int]):
    if case.kind == "text":
        recognizer = Ocr(
            area,
            lang="azur_lane",
            letter=case.letter,
            threshold=case.threshold,
            name=case.semantic_name,
        )
        return str(recognizer.ocr(image)).strip()
    if case.kind == "digit":
        recognizer = Digit(
            area,
            lang="azur_lane",
            letter=case.letter,
            threshold=case.threshold,
            name=case.semantic_name,
        )
        return int(recognizer.ocr(image))
    raise ValueError(f"unsupported OCR kind: {case.kind}")


def ocr_rows() -> tuple[list[dict], list[dict]]:
    """Return native references and semantic candidate comparisons."""
    references: list[dict] = []
    rows: list[dict] = []
    for case in OCR_CASES:
        fixture = _read_color(case.fixture_path)
        native, origin = _embed_fixture(fixture)
        height, width = fixture.shape[:2]
        area = (
            origin[0],
            origin[1],
            origin[0] + width,
            origin[1] + height,
        )
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
        for candidate_name, normalizer in CANDIDATES.items():
            for resolution, source in sources.items():
                normalized = normalizer(source)
                actual = _ocr_value(normalized, case, area)
                rows.append(
                    {
                        "candidate": candidate_name,
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
    rows: list[dict] = []
    rng = np.random.default_rng(20260815)
    for resolution in SOURCE_RESOLUTIONS:
        width, height = resolution
        source = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
        for candidate_name, normalizer in CANDIDATES.items():
            for _ in range(3):
                normalizer(source)
            samples_ms: list[float] = []
            for _ in range(iterations):
                started = time.perf_counter()
                normalizer(source)
                samples_ms.append((time.perf_counter() - started) * 1000.0)
            ordered = sorted(samples_ms)
            p95_index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
            rows.append(
                {
                    "candidate": candidate_name,
                    "resolution": list(resolution),
                    "iterations": iterations,
                    "median_ms": float(statistics.median(samples_ms)),
                    "p95_ms": float(ordered[p95_index]),
                }
            )
    return rows


def summarize(
    detector_rows: list[dict],
    ocr_measurements: list[dict],
    timings: list[dict],
) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for candidate_name in CANDIDATES:
        candidate_detector = [
            row for row in detector_rows if row["candidate"] == candidate_name
        ]
        margins = [
            row["margin"] for row in candidate_detector if row["margin"] is not None
        ]
        candidate_ocr = [
            row for row in ocr_measurements if row["candidate"] == candidate_name
        ]
        timing_4k = next(
            row
            for row in timings
            if row["candidate"] == candidate_name
            and row["resolution"] == [3840, 2160]
        )
        summary[candidate_name] = {
            "min_correct_similarity": min(
                row["correct_similarity"] for row in candidate_detector
            ),
            "min_threshold_margin": min(
                row["threshold_margin"] for row in candidate_detector
            ),
            "min_correct_vs_wrong_margin": min(margins) if margins else None,
            "max_localization_error_px": max(
                row["localization_error_px"] for row in candidate_detector
            ),
            "detector_pass_count": sum(bool(row["pass"]) for row in candidate_detector),
            "detector_total": len(candidate_detector),
            "ocr_pass_count": sum(bool(row["pass"]) for row in candidate_ocr),
            "ocr_total": len(candidate_ocr),
            "median_4k_ms": timing_4k["median_ms"],
            "p95_4k_ms": timing_4k["p95_ms"],
        }
    return summary


def run_benchmark(timing_iterations: int = DEFAULT_TIMING_ITERATIONS) -> dict:
    native_detector = detector_reference_rows()
    detector_rows = detector_candidate_rows()
    ocr_reference, ocr_measurements = ocr_rows()
    timings = timing_rows(iterations=timing_iterations)
    return {
        "metadata": {
            "canonical_resolution": list(CANONICAL_SIZE),
            "source_resolutions": [list(item) for item in SOURCE_RESOLUTIONS],
            "channel_contract": (
                "Synthetic repository PNG fixtures are read by cv2.imread as BGR. "
                "Baseline and every candidate receive the identical BGR arrays. "
                "This benchmark does not alter or infer screenshot-backend channel order."
            ),
            "synthetic_renderer": "cv2.INTER_CUBIC upscale from canonical fixture frame",
            "performance_note": "informational only; CI hardware timing is not a gate",
            "candidate_order": list(CANDIDATES),
            "detector_cases": [asdict(case) | {
                "fixture_path": case.fixture_path.relative_to(ROOT).as_posix(),
                "correct_template_path": case.correct_template_path.relative_to(ROOT).as_posix(),
                "wrong_template_path": (
                    case.wrong_template_path.relative_to(ROOT).as_posix()
                    if case.wrong_template_path is not None
                    else None
                ),
            } for case in DETECTOR_CASES],
            "ocr_cases": [asdict(case) | {
                "fixture_path": case.fixture_path.relative_to(ROOT).as_posix(),
            } for case in OCR_CASES],
        },
        "native_detector_reference": native_detector,
        "detector_measurements": detector_rows,
        "ocr_native_reference": ocr_reference,
        "ocr_measurements": ocr_measurements,
        "timings": timings,
        "summary": summarize(detector_rows, ocr_measurements, timings),
        "four_k_detector_rows": [
            row for row in detector_rows if row["resolution"] == [3840, 2160]
        ],
        "four_k_ocr_rows": [
            row for row in ocr_measurements if row["resolution"] == [3840, 2160]
        ],
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timing-iterations",
        type=int,
        default=DEFAULT_TIMING_ITERATIONS,
    )
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    payload = run_benchmark(timing_iterations=args.timing_iterations)
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
