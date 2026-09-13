from __future__ import annotations

import numpy as np

from module.dock_inventory.card_grid import DockCardGridScanner
from module.dock_inventory.mumu_traversal import DockMuMuInventoryTraversal


def test_real_v16_3_short_shift_uses_direct_three_row_proof(monkeypatch) -> None:
    """Ручная top-фаза может требовать меньше 16 px, если итог уже доказан Stage 3."""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    monkeypatch.setattr(
        DockCardGridScanner,
        "register_rows",
        lambda _self, _frame: (60, 287, 514),
    )

    rows = DockMuMuInventoryTraversal._structural_short_nudge_proven(
        frame,
        shift_x=-0.008,
        shift_y=-14.713,
        response=0.963,
    )

    assert rows == (60, 287, 514)


def test_real_v16_3_short_shift_without_three_rows_is_not_accepted(monkeypatch) -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    monkeypatch.setattr(
        DockCardGridScanner,
        "register_rows",
        lambda _self, _frame: (62, 289),
    )

    rows = DockMuMuInventoryTraversal._structural_short_nudge_proven(
        frame,
        shift_x=-0.008,
        shift_y=-14.713,
        response=0.963,
    )

    assert rows == ()


def test_real_v16_1_overshift_stays_rejected_even_with_three_row_stub(monkeypatch) -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    monkeypatch.setattr(
        DockCardGridScanner,
        "register_rows",
        lambda _self, _frame: (60, 287, 514),
    )

    rows = DockMuMuInventoryTraversal._structural_short_nudge_proven(
        frame,
        shift_x=-0.003,
        shift_y=-36.991,
        response=0.945,
    )

    assert rows == ()
