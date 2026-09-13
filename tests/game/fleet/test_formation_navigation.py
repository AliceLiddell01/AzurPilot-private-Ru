import cv2
import numpy as np
import pytest

from module.formation.navigation import (
    FormationFleetController,
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
    frame[y1:y2, x1:x2] = cv2.cvtColor(orange_hsv, cv2.COLOR_HSV2RGB)

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


def test_scan_stage_logs_fleet_stage_type_and_preserves_exception(monkeypatch) -> None:
    warnings: list[str] = []
    error = RuntimeError("scanner diagnostic")

    def fail() -> None:
        raise error

    monkeypatch.setattr("module.formation.navigation.logger.warning", warnings.append)

    with pytest.raises(RuntimeError) as raised:
        FormationFleetController._run_scan_stage(4, "scanner_scan", fail)

    assert raised.value is error
    assert warnings == [
        "[Построение — сканер] physical_scan_failure "
        "fleet=4 stage=scanner_scan type=RuntimeError "
        "diagnostic=RuntimeError: scanner diagnostic"
    ]


def test_exception_diagnostic_keeps_type_when_message_is_empty() -> None:
    diagnostic = FormationFleetController._format_exception_diagnostic(RuntimeError())

    assert diagnostic == "RuntimeError"


class _TransitionState:
    @staticmethod
    def info_opened(frame: str) -> bool:
        return frame == "info"

    @staticmethod
    def fleet_menu_opened(frame: str) -> bool:
        return False


class _TransitionDevice:
    def __init__(self) -> None:
        self.clicks: list[str] = []

    def click(self, button) -> None:
        self.clicks.append(button.name)


class _TransitionController(FormationFleetController):
    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self._frame: str | None = None
        self.frames_seen = 0
        self.device = _TransitionDevice()
        self._state = _TransitionState()
        self._layout = FormationNavigationLayout()

    @property
    def formation_state(self) -> _TransitionState:
        return self._state

    @property
    def formation_navigation_layout(self) -> FormationNavigationLayout:
        return self._layout

    def loop(self, skip_first=True, timeout=None):
        del skip_first, timeout
        for frame in self._frames:
            self._frame = frame
            self.frames_seen += 1
            yield frame

    def _current_frame(self) -> str:
        assert self._frame is not None
        return self._frame

    def ui_page_appear(self, page, offset=(30, 30), interval=0) -> bool:
        del page, offset, interval
        return True


def test_open_info_rejects_one_frame_transition_false_positive() -> None:
    controller = _TransitionController(
        ["fleet", "info", "fleet", "info", "info", "info"]
    )

    controller._open_info()

    assert controller.frames_seen == 6
    assert controller.device.clicks == ["FORMATION_OPEN_INFO", "FORMATION_OPEN_INFO"]


def test_close_info_requires_stable_formation_boundary() -> None:
    controller = _TransitionController(
        ["info", "fleet", "info", "fleet", "fleet", "fleet"]
    )

    controller._close_info()

    assert controller.frames_seen == 6
    assert controller.device.clicks == ["FORMATION_CLOSE_INFO", "FORMATION_CLOSE_INFO"]


class _InterruptingScanner:
    def scan(self, frame, *, fleet_index):
        del frame, fleet_index
        raise KeyboardInterrupt("cancelled")


class _CleanupFailureController(FormationFleetController):
    def __init__(self) -> None:
        self._scanner = _InterruptingScanner()

    @property
    def formation_fleet_scanner(self) -> _InterruptingScanner:
        return self._scanner

    def ensure_formation_page(self) -> None:
        return None

    def ensure_surface_fleet(self, fleet_index: int) -> None:
        del fleet_index

    def _open_info(self) -> None:
        return None

    def _capture_scan_frame(self) -> np.ndarray:
        return _frame()

    def _validate_scan_info_state(self, frame: np.ndarray) -> None:
        del frame

    def _close_info(self) -> None:
        raise RuntimeError("close failed")


def test_cleanup_failure_does_not_replace_keyboard_interrupt() -> None:
    controller = _CleanupFailureController()

    with pytest.raises(KeyboardInterrupt) as raised:
        controller.scan_surface_fleet(1)

    assert str(raised.value) == "cancelled"
    assert raised.value.__notes__ == [
        "Дополнительно не удалось закрыть Formation Info: RuntimeError: close failed"
    ]
