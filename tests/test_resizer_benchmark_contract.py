import numpy as np

from module.device.screenshot import Screenshot
from tools.benchmark_resizer_non_native import (
    BASELINE_CURRENT,
    CANDIDATES,
    DETECTOR_CASES,
    INTER_AREA_CURRENT_BLUR,
    INTER_AREA_ONLY,
    INTER_CUBIC_ONLY,
    INTER_LANCZOS4_ONLY,
    INTER_LINEAR_ONLY,
    SOURCE_RESOLUTIONS,
)


def test_resizer_benchmark_candidate_set_stays_bounded():
    assert tuple(CANDIDATES) == (
        BASELINE_CURRENT,
        INTER_CUBIC_ONLY,
        INTER_AREA_ONLY,
        INTER_AREA_CURRENT_BLUR,
        INTER_LINEAR_ONLY,
        INTER_LANCZOS4_ONLY,
    )
    assert SOURCE_RESOLUTIONS == (
        (1600, 900),
        (1920, 1080),
        (2560, 1440),
        (3840, 2160),
    )
    assert tuple(case.name for case in DETECTOR_CASES) == (
        "data_logger_current",
        "data_logger_legacy",
        "zone_dangerous_vs_safe",
        "zone_safe_vs_dangerous",
        "tiny_meowfficer_percentage",
        "localization_map_world",
        "get_items_1_vs_2",
    )


def test_resizer_benchmark_baseline_is_exact_production_normalizer():
    image = np.arange(900 * 1600 * 3, dtype=np.uint8).reshape((900, 1600, 3))

    expected = Screenshot.resize_screenshot_to_720p(image)
    actual = CANDIDATES[BASELINE_CURRENT](image)

    np.testing.assert_array_equal(actual, expected)
