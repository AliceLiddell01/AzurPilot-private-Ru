import cv2
import numpy as np
import pytest

from module.formation.navigation import (
    FormationFleetIndexOcr,
    FormationNavigationLayout,
    FormationUiStateDetector,
)


def _frame() -> np.ndarray:
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _paint_info_headers(frame: np.ndarray, layout: FormationNavigationLayout) -> None:
    for area in layout.info_header_probes:
        x1, y1, x2, y2 = area
        frame[y1:y2, x1:x2] = (255, 255, 255)


def test_fleet_row_maps_directly_from_game_index() -> None:
    layout = FormationNavigationLayout()

    assert layout.fleet_row(6) == layout.fleet_rows_top_to_bottom[0]
    assert layout.fleet_row(1) == layout.fleet_rows_top_to_bottom[5]

    with pytest.raises(ValueError, match="1..6"):
        layout.fleet_row(0)


def test_menu_detector_requires_multiple_gray_rows() -> None:
    layout = FormationNavigationLayout()
    detector = FormationUiStateDetector(layout)
    frame = _frame()

    for area in layout.fleet_menu_probes[:4]:
        x1, y1, x2, y2 = area
        frame[y1:y2, x1:x2] = (210, 210, 210)

    assert detector.fleet_menu_opened(frame) is True

    x1, y1, x2, y2 = layout.fleet_menu_probes[0]
    frame[y1:y2, x1:x2] = (0, 0, 0)
    assert detector.fleet_menu_opened(frame) is False


def test_info_detector_requires_selected_state_and_both_headers() -> None:
    layout = FormationNavigationLayout()
    detector = FormationUiStateDetector(layout)
    frame = _frame()
    x1, y1, x2, y2 = layout.info_state_probe

    orange_hsv = np.zeros((y2 - y1, x2 - x1, 3), dtype=np.uint8)
    orange_hsv[:, :, 0] = 15
    orange_hsv[:, :, 1] = 180
    orange_hsv[:, :, 2] = 235
    frame[y1:y2, x1:x2] = cv2.cvtColor(orange_hsv, cv2.COLOR_HSV2BGR)

    assert detector.info_opened(frame) is False

    _paint_info_headers(frame, layout)
    assert detector.info_opened(frame) is True

    hx1, hy1, hx2, hy2 = layout.info_header_probes[0]
    frame[hy1:hy2, hx1:hx2] = (0, 0, 0)
    assert detector.info_opened(frame) is False


def test_info_detector_rejects_headers_without_selected_info_state() -> None:
    layout = FormationNavigationLayout()
    detector = FormationUiStateDetector(layout)
    frame = _frame()

    _paint_info_headers(frame, layout)

    assert detector.info_opened(frame) is False


class _IndexModel:
    def __init__(self, value) -> None:
        self.value = value

    def ocr(self, frame):
        return self.value


@pytest.mark.parametrize(
    ("raw", "expected"),
    ((1, 1), (6, 6), (0, None), (7, None), (True, None), ("6", None)),
)
def test_fleet_index_ocr_is_domain_validated(raw, expected) -> None:
    reader = FormationFleetIndexOcr(model=_IndexModel(raw))

    assert reader.read(_frame()) == expected
