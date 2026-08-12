from __future__ import annotations

import cv2
import numpy as np
import pytest

from module.dock_inventory.attributes import (
    DockAttributeInputError,
    DockLevelScanner,
    DockStarCvError,
    DockStarScanner,
    DockStarStatus,
)
from module.dock_inventory.card_grid import (
    DockCardPresence,
    DockCardPresenceEvidence,
    DockCardSlotObservation,
)


def _slot(y: int = 77) -> DockCardSlotObservation:
    return DockCardSlotObservation(
        slot_index=0,
        column=0,
        row=0,
        area=(93, y, 231, y + 204),
        presence=DockCardPresence.PRESENT,
        evidence=DockCardPresenceEvidence(
            luma_std=40.0,
            edge_density=0.2,
            chroma_mean=30.0,
        ),
    )


def test_level_roi_outside_frame_is_operational_input_error() -> None:
    scanner = DockLevelScanner(125)

    with pytest.raises(DockAttributeInputError, match="level ROI выходит за frame"):
        scanner.scan(
            np.zeros((720, 1280, 3), dtype=np.uint8),
            (_slot(700),),
        )


def test_star_scan_wraps_canny_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    scanner = DockStarScanner()

    def raise_cv_error(*_args, **_kwargs):
        raise cv2.error("fixture canny failure")

    monkeypatch.setattr(cv2, "Canny", raise_cv_error)

    with pytest.raises(DockStarCvError, match="fixture canny failure"):
        scanner.scan(
            np.full((720, 1280, 3), 70, dtype=np.uint8),
            (_slot(),),
        )


def test_star_fill_match_score_is_clamped_to_observation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = DockStarScanner()
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)

    monkeypatch.setattr(
        cv2,
        "matchTemplate",
        lambda *_args, **_kwargs: np.full((8, 120), 1.0001, dtype=np.float32),
    )
    monkeypatch.setattr(
        scanner,
        "_first_filled_star",
        lambda *_args, **_kwargs: (48, 13, 4),
    )

    def fixed_alignment(
        _edges,
        _edge_distance,
        center_x,
        center_y,
        _outline_x,
        _outline_y,
    ):
        half = scanner.STAR_TEMPLATE_SIZE // 2
        return center_x - half, center_y - half, 0.9

    monkeypatch.setattr(scanner, "_best_shape_alignment", fixed_alignment)

    result = scanner.scan(frame, (_slot(),))[0]

    assert result.status is DockStarStatus.OBSERVED
    assert result.glyphs
    assert all(glyph.fill_match_score == 1.0 for glyph in result.glyphs)
