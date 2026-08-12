from __future__ import annotations

import types

import numpy as np

from module.dock_inventory.card_grid import (
    DockCardGridScanner,
    DockCardPresenceEvidence,
)


PRESENT = DockCardPresenceEvidence(
    luma_std=40.0,
    edge_density=0.2,
    chroma_mean=30.0,
)
ABSENT = DockCardPresenceEvidence(
    luma_std=0.0,
    edge_density=0.0,
    chroma_mean=0.0,
)


def _frame() -> np.ndarray:
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _use_candidates(scanner: DockCardGridScanner, *origins: int) -> None:
    scanner._candidate_row_origins = types.MethodType(
        lambda _self, _frame: tuple(origins),
        scanner,
    )


def test_visible_preceding_row_is_recovered_from_proven_phase_and_presence() -> None:
    scanner = DockCardGridScanner()
    _use_candidates(scanner, 283, 510)
    scanner.measure_presence = types.MethodType(
        lambda _self, _frame, _area: PRESENT,
        scanner,
    )

    assert scanner.register_rows(_frame()) == (56, 283, 510)


def test_preceding_phase_candidate_is_not_recovered_without_presence() -> None:
    scanner = DockCardGridScanner()
    _use_candidates(scanner, 283, 510)

    def measure(_self, _frame, area):
        return ABSENT if area[1] == 56 else PRESENT

    scanner.measure_presence = types.MethodType(measure, scanner)

    assert scanner.register_rows(_frame()) == (283, 510)


def test_top_baseline_does_not_infer_row_outside_supported_area() -> None:
    scanner = DockCardGridScanner()
    _use_candidates(scanner, 77, 304)
    scanner.measure_presence = types.MethodType(
        lambda _self, _frame, _area: PRESENT,
        scanner,
    )

    assert scanner.register_rows(_frame()) == (77, 304)
