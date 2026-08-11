from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from module.dock_inventory.card_grid import (
    DockCardGridCollector,
    DockCardGridFrameError,
    DockCardGridRegistrationError,
    DockCardGridScanner,
    DockCardLayoutScanResult,
    DockCardPresence,
    DockCardPresenceEvidence,
    DockCardSlotObservation,
    DockViewportCardScan,
    scan_dock_card_layouts,
)
from module.dock_inventory.navigation import (
    DockInventoryNavigator,
    DockInventoryStage2Result,
    DockPrerequisiteEvidence,
)
from module.dock_inventory.traversal import (
    DockInventoryTraversalError,
    DockTraversalResult,
    DockTraversalViewport,
)
from module.game_settings.model import GameSettingState
from module.game_settings.snapshot import GameSettingsSnapshotAccessSource
from module.retire.dock import CARD_GRIDS


def _paint_slot(
    frame: np.ndarray,
    area: tuple[int, int, int, int],
    state: str,
    *,
    seed: int,
) -> None:
    x1, y1, x2, y2 = area
    y1_clip = max(0, y1)
    y2_clip = min(frame.shape[0], y2)
    if y1_clip >= y2_clip:
        return
    shape = (y2_clip - y1_clip, x2 - x1, 3)
    if state == "P":
        rng = np.random.default_rng(seed)
        crop = rng.integers(0, 256, size=shape, dtype=np.uint8)
        # Universal structural borders, independent of rarity color.
        crop[:4] = (220, 220, 220)
        crop[-4:] = (35, 35, 35)
        crop[:, :4] = (210, 210, 210)
        crop[:, -4:] = (25, 25, 25)
    elif state == "A":
        yy, xx = np.indices(shape[:2])
        value = (30 + ((xx // 24 + yy // 24) % 2) * 4).astype(np.uint8)
        crop = np.repeat(value[:, :, None], 3, axis=2)
    elif state == "U":
        yy, xx = np.indices(shape[:2])
        value = (35 + ((xx // 10 + yy // 10) % 2) * 38).astype(np.uint8)
        crop = np.repeat(value[:, :, None], 3, axis=2)
    else:
        raise ValueError(state)
    frame[y1_clip:y2_clip, x1:x2] = crop


def _make_frame(
    row_origins: tuple[int, ...],
    occupancy: tuple[str, ...] | None = None,
    *,
    extra_card_origins: tuple[int, ...] = (),
) -> np.ndarray:
    scanner = DockCardGridScanner()
    yy, xx = np.indices((720, 1280))
    background = (18 + ((xx // 6 + yy // 9) % 2) * 54).astype(np.uint8)
    frame = np.repeat(background[:, :, None], 3, axis=2)
    left = scanner.X_ORIGIN
    right = scanner._column_area(scanner.COLUMN_COUNT - 1, scanner.SAFE_SCAN_TOP)[2]

    for origin in row_origins:
        center = origin - scanner.GAP_CENTER_TO_ROW_ORIGIN
        start = center - 7
        end = center + 8
        frame[max(0, start) : min(frame.shape[0], end + 1), left:right] = (24, 24, 28)

    states = occupancy or ("P",) * (len(row_origins) * scanner.COLUMN_COUNT)
    if len(states) != len(row_origins) * scanner.COLUMN_COUNT:
        raise ValueError("occupancy length does not match rows")
    for row, origin in enumerate(row_origins):
        for column in range(scanner.COLUMN_COUNT):
            _paint_slot(
                frame,
                scanner._column_area(column, origin),
                states[row * scanner.COLUMN_COUNT + column],
                seed=abs(origin * 10 + column),
            )
    for origin in extra_card_origins:
        for column in range(scanner.COLUMN_COUNT):
            _paint_slot(
                frame,
                scanner._column_area(column, origin),
                "P",
                seed=abs(origin * 10 + column),
            )
    return frame


def _viewport(
    frame: np.ndarray,
    *,
    index: int = 0,
    position: float = 0.0,
    is_bottom: bool = False,
) -> DockTraversalViewport:
    return DockTraversalViewport(
        index=index,
        scroll_position=position,
        is_top=index == 0,
        is_bottom=is_bottom,
        frame=frame,
    )


def test_geometry_is_derived_from_canonical_card_grids() -> None:
    scanner = DockCardGridScanner()

    assert scanner.COLUMN_COUNT == int(CARD_GRIDS.grid_shape[0]) == 7
    assert scanner.X_ORIGIN == int(CARD_GRIDS.origin[0])
    assert scanner.X_DELTA == float(CARD_GRIDS.delta[0])
    assert scanner.CARD_WIDTH == int(CARD_GRIDS.button_shape[0])
    assert scanner.CARD_HEIGHT == int(CARD_GRIDS.button_shape[1])
    assert scanner.ROW_DELTA == round(float(CARD_GRIDS.delta[1]))
    assert scanner.EXPECTED_GAP == scanner.ROW_DELTA - scanner.CARD_HEIGHT


@pytest.mark.parametrize("first_origin", [76, 102, 141, 174, 263])
def test_dynamic_registration_tracks_vertical_offset(first_origin: int) -> None:
    scanner = DockCardGridScanner()
    expected = (first_origin, first_origin + scanner.ROW_DELTA)
    frame = _make_frame(expected)

    actual = scanner.register_rows(frame)

    assert abs(actual[0] - expected[0]) <= scanner.ROW_SPACING_TOLERANCE
    assert actual[1] - actual[0] == scanner.ROW_DELTA


def test_partial_top_and_bottom_rows_are_excluded() -> None:
    scanner = DockCardGridScanner()
    full = (174, 174 + scanner.ROW_DELTA)
    partial_top = full[0] - scanner.ROW_DELTA
    partial_bottom = full[-1] + scanner.ROW_DELTA
    frame = _make_frame(
        full + (partial_bottom,),
        extra_card_origins=(partial_top,),
    )

    actual = scanner.register_rows(frame)

    assert len(actual) == 2
    assert actual[1] - actual[0] == scanner.ROW_DELTA
    assert all(
        abs(actual_origin - expected_origin) <= scanner.ROW_SPACING_TOLERANCE
        for actual_origin, expected_origin in zip(actual, full)
    )


def test_spacing_within_tolerance_is_accepted() -> None:
    scanner = DockCardGridScanner()
    rows = (100, 100 + scanner.ROW_DELTA + scanner.ROW_SPACING_TOLERANCE)

    actual = scanner.register_rows(_make_frame(rows))

    assert len(actual) == 2
    assert actual[1] - actual[0] == scanner.ROW_DELTA
    assert abs(actual[0] - rows[0]) <= scanner.ROW_SPACING_TOLERANCE


def test_impossible_row_spacing_fails_closed() -> None:
    scanner = DockCardGridScanner()
    rows = (100, 400)

    with pytest.raises(DockCardGridRegistrationError, match="расстояние"):
        scanner.register_rows(_make_frame(rows))


def test_unrelated_gap_bands_without_cards_fail_closed() -> None:
    scanner = DockCardGridScanner()
    frame = _make_frame((100,), occupancy=("A",) * scanner.COLUMN_COUNT)

    with pytest.raises(DockCardGridRegistrationError, match="ни одной"):
        scanner.register_rows(frame)


@pytest.mark.parametrize("origins", [(100, 100), (200, 100)])
def test_duplicate_or_reverse_row_origins_are_rejected(
    origins: tuple[int, ...],
) -> None:
    scanner = DockCardGridScanner()

    with pytest.raises(DockCardGridRegistrationError, match="возрастать"):
        scanner._validate_row_origins(origins, height=720)


def test_all_registered_areas_are_in_frame_and_row_major() -> None:
    scanner = DockCardGridScanner()
    scan = scanner.scan_viewport(_viewport(_make_frame((102, 329))))

    assert scan.registered_row_origins[1] - scan.registered_row_origins[0] == 227
    assert [slot.slot_index for slot in scan.slots] == list(range(14))
    assert [slot.column for slot in scan.slots] == list(range(7)) * 2
    assert [slot.row for slot in scan.slots] == [0] * 7 + [1] * 7
    for slot in scan.slots:
        x1, y1, x2, y2 = slot.area
        assert 0 <= x1 < x2 <= 1280
        assert scanner.SAFE_SCAN_TOP <= y1 < y2 <= 719


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_presence_present_uses_generic_structure(seed: int) -> None:
    scanner = DockCardGridScanner()
    frame = _make_frame((100,))
    area = scanner._column_area(seed % scanner.COLUMN_COUNT, 100)
    evidence = scanner.measure_presence(frame, area)

    assert scanner.classify_presence(evidence) is DockCardPresence.PRESENT


def test_presence_absent_requires_strong_background_evidence() -> None:
    scanner = DockCardGridScanner()
    states = ("P",) + ("A",) * 6
    frame = _make_frame((100,), occupancy=states)
    evidence = scanner.measure_presence(frame, scanner._column_area(1, 100))

    assert scanner.classify_presence(evidence) is DockCardPresence.ABSENT


@pytest.mark.parametrize(
    "evidence",
    [
        DockCardPresenceEvidence(40.0, 0.01, 20.0),
        DockCardPresenceEvidence(10.0, 0.25, 20.0),
        DockCardPresenceEvidence(24.0, 0.08, 20.0),
    ],
)
def test_ambiguous_or_conflicting_presence_is_unknown(
    evidence: DockCardPresenceEvidence,
) -> None:
    assert DockCardGridScanner().classify_presence(evidence) is DockCardPresence.UNKNOWN


def test_mixed_final_row_counts_are_exact() -> None:
    scanner = DockCardGridScanner()
    states = ("P",) * 5 + ("A",) * 2
    scan = scanner.scan_viewport(
        _viewport(_make_frame((400,), occupancy=states), is_bottom=True)
    )

    assert scan.present_count == 5
    assert scan.absent_count == 2
    assert scan.unknown_count == 0


def test_scan_uses_one_supplied_frame_and_does_not_mutate_it() -> None:
    scanner = DockCardGridScanner()
    frame = _make_frame((102, 329))
    before = frame.copy()

    scanner.scan_viewport(_viewport(frame))

    assert np.array_equal(frame, before)
    assert not hasattr(scanner, "device")


@pytest.mark.parametrize(
    "frame",
    [
        np.zeros((720, 1280), dtype=np.uint8),
        np.zeros((720, 1280, 4), dtype=np.uint8),
        np.zeros((720, 1280, 3), dtype=np.float32),
        np.zeros((200, 200, 3), dtype=np.uint8),
    ],
)
def test_invalid_frame_contract_fails_closed(frame: np.ndarray) -> None:
    with pytest.raises(DockCardGridFrameError):
        DockCardGridScanner().register_rows(frame)


def test_slot_area_outside_frame_is_rejected() -> None:
    frame = _make_frame((100,))

    with pytest.raises(DockCardGridFrameError, match="выходит"):
        DockCardGridScanner().measure_presence(frame, (0, 0, 2000, 2000))


def test_collector_keeps_top_middle_and_final_bottom_viewports() -> None:
    collector = DockCardGridCollector()
    frame = _make_frame((102, 329))

    collector(_viewport(frame, index=0, position=0.0))
    collector(_viewport(frame.copy(), index=1, position=0.5))
    collector(_viewport(frame.copy(), index=2, position=1.0, is_bottom=True))

    assert [scan.viewport_index for scan in collector.viewports] == [0, 1, 2]
    assert collector.viewports[-1].scroll_position == 1.0


def test_cross_viewport_duplicate_cards_are_preserved() -> None:
    collector = DockCardGridCollector()
    frame = _make_frame((102, 329))

    collector(_viewport(frame, index=0, position=0.0))
    collector(_viewport(frame.copy(), index=1, position=0.5))

    assert len(collector.viewports) == 2
    assert collector.viewports[0].present_count == 14
    assert collector.viewports[1].present_count == 14


def test_production_module_has_no_semantic_ship_scanner_dependency() -> None:
    path = Path(__file__).parents[1] / "module" / "dock_inventory" / "card_grid.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = {
        "FleetNameScanner",
        "ShipNameMatcher",
        "LevelOcr",
        "DigitCounter",
        "RarityScanner",
    }

    assert imports.isdisjoint(forbidden)


class _FakeNavigator(DockInventoryNavigator):
    def __init__(self, viewports: tuple[DockTraversalViewport, ...]) -> None:
        self.viewports = viewports
        self.run_calls = 0

    def run_stage2(self, visitor, **kwargs) -> DockInventoryStage2Result:
        self.run_calls += 1
        for viewport in self.viewports:
            visitor(viewport)
        prerequisite = DockPrerequisiteEvidence(
            snapshot_path=Path("config/state/game_settings_snapshot.json"),
            snapshot_source=GameSettingsSnapshotAccessSource.SNAPSHOT,
            cache_status=None,
            scanned_at=datetime.now(timezone.utc),
            detected=GameSettingState.OFF,
            required=GameSettingState.OFF,
            compatible=True,
        )
        traversal = DockTraversalResult(
            visited_viewports=len(self.viewports),
            positions=tuple(viewport.scroll_position for viewport in self.viewports),
            reached_bottom=True,
            final_viewport_visited=True,
            no_progress_retries=0,
        )
        return DockInventoryStage2Result(prerequisite, traversal)


def test_high_level_layout_scan_reuses_stage2_workflow() -> None:
    frame = _make_frame((102, 329))
    navigator = _FakeNavigator(
        (
            _viewport(frame, index=0, position=0.0),
            _viewport(frame.copy(), index=1, position=1.0, is_bottom=True),
        )
    )

    result = scan_dock_card_layouts(navigator)

    assert isinstance(result, DockCardLayoutScanResult)
    assert navigator.run_calls == 1
    assert len(result.viewports) == result.traversal.visited_viewports == 2


class _NoScrollNavigator(DockInventoryNavigator):
    def __init__(self) -> None:
        pass

    def run_stage2(self, visitor, **kwargs):
        raise DockInventoryTraversalError("Полоса прокрутки отсутствует")


def test_full_capacity_no_scroll_ambiguity_remains_fail_closed() -> None:
    with pytest.raises(DockInventoryTraversalError, match="отсутствует"):
        scan_dock_card_layouts(_NoScrollNavigator())


def test_viewport_result_rejects_nondeterministic_row_order() -> None:
    with pytest.raises(ValueError, match="возрастать"):
        DockViewportCardScan(
            viewport_index=0,
            scroll_position=0.0,
            registered_row_origins=(300, 100),
            slots=(),
        )


def test_slot_observation_rejects_boolean_indexes() -> None:
    evidence = DockCardPresenceEvidence(1.0, 0.0, 1.0)

    with pytest.raises(TypeError, match="slot_index"):
        DockCardSlotObservation(
            slot_index=True,  # type: ignore[arg-type]
            column=0,
            row=0,
            area=(0, 0, 1, 1),
            presence=DockCardPresence.ABSENT,
            evidence=evidence,
        )
