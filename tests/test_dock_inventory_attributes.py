from __future__ import annotations

import inspect
import math
from dataclasses import replace

import cv2
import numpy as np
import pytest

from module.dock_inventory import attributes
from module.dock_inventory.attributes import (
    DockAttributeIncompleteError,
    DockAttributeInputError,
    DockAttributeScanner,
    DockLevelOcrError,
    DockLevelScanner,
    DockLevelStatus,
    DockStarCvError,
    DockStarGlyphObservation,
    DockStarGlyphState,
    DockStarScanner,
    DockStarScanObservation,
    DockStarStatus,
    scan_dock_attributes,
)
from module.dock_inventory.card_grid import (
    DockCardPresence,
    DockCardPresenceEvidence,
    DockCardSlotObservation,
    DockViewportCardScan,
)
from module.dock_inventory.catalog import load_dock_identity_catalog
from module.dock_inventory.identity import (
    DockCardIdentityObservation,
    DockIdentityResolutionMethod,
    DockIdentityScanner,
    DockShipIdentityResolution,
    DockViewportIdentityScan,
)
from module.dock_inventory.model import (
    CanonicalShipIdentity,
    IdentityStatus,
    StarObservation,
)
from module.dock_inventory.navigation import DockInventoryNavigator
from module.dock_inventory.progression import (
    DockProgressionCatalog,
    DockProgressionFamily,
    DockProgressionProvenance,
    DockProgressionState,
    ProgressionKind,
    ProgressionStatus,
)
from module.dock_inventory.traversal import DockTraversalViewport

SLOT_X = (93, 258, 422, 587, 752, 916, 1081)


def _evidence() -> DockCardPresenceEvidence:
    return DockCardPresenceEvidence(luma_std=40.0, edge_density=0.2, chroma_mean=30.0)


def _slot(index: int, y: int, presence: DockCardPresence) -> DockCardSlotObservation:
    x = SLOT_X[index]
    return DockCardSlotObservation(
        slot_index=index,
        column=index,
        row=0,
        area=(x, y, x + 138, y + 204),
        presence=presence,
        evidence=_evidence(),
    )


def _card_scan(
    *,
    y: int = 77,
    present_indexes: tuple[int, ...] = (0,),
    unknown_indexes: tuple[int, ...] = (),
    viewport_index: int = 0,
    scroll_position: float = 0.0,
) -> DockViewportCardScan:
    slots = []
    for index in range(7):
        if index in present_indexes:
            presence = DockCardPresence.PRESENT
        elif index in unknown_indexes:
            presence = DockCardPresence.UNKNOWN
        else:
            presence = DockCardPresence.ABSENT
        slots.append(_slot(index, y, presence))
    return DockViewportCardScan(
        viewport_index=viewport_index,
        scroll_position=scroll_position,
        registered_row_origins=(y,),
        slots=tuple(slots),
    )


def _resolution(group: int = 100) -> DockShipIdentityResolution:
    return DockShipIdentityResolution(
        status=IdentityStatus.MATCHED,
        method=DockIdentityResolutionMethod.EXACT,
        raw_name_ocr="Fixture",
        displayed_name="Fixture",
        canonical_identity=CanonicalShipIdentity(f"azur_lane_ship_group:{group}"),
        canonical_name="Fixture",
        best_score=1.0,
        candidate_count=1,
        candidates=(f"azur_lane_ship_group:{group}",),
    )


def _identity_scan(card_scan: DockViewportCardScan) -> DockViewportIdentityScan:
    observations = tuple(
        DockCardIdentityObservation(
            viewport_index=card_scan.viewport_index,
            slot_index=slot.slot_index,
            row=slot.row,
            column=slot.column,
            area=slot.area,
            name_area=(
                slot.area[0],
                slot.area[1],
                slot.area[0] + 20,
                slot.area[1] + 20,
            ),
            resolution=_resolution(),
        )
        for slot in card_scan.slots
        if slot.presence is DockCardPresence.PRESENT
    )
    return DockViewportIdentityScan(
        viewport_index=card_scan.viewport_index,
        scroll_position=card_scan.scroll_position,
        card_scan=card_scan,
        observations=observations,
    )


def _provenance() -> DockProgressionProvenance:
    return DockProgressionProvenance(
        source_repository="fixture/source",
        source_commit="1" * 40,
        source_path="ship.json",
        source_blob_sha="2" * 40,
        source_sha256="3" * 64,
        supplemental_source_repository="fixture/lua",
        supplemental_source_commit="4" * 40,
        supplemental_template_path="template.lua",
        supplemental_template_blob_sha="5" * 40,
        blueprint_source_path="blueprint.lua",
        blueprint_source_blob_sha="6" * 40,
        level_source_path="level.lua",
        level_source_blob_sha="7" * 40,
        selection_contract="fixture",
    )


def _catalog() -> DockProgressionCatalog:
    family = DockProgressionFamily(
        canonical_id="azur_lane_ship_group:100",
        family_type="ordinary",
        states=tuple(
            DockProgressionState(
                semantic_id=f"limit_break:{index}",
                kind=ProgressionKind.STANDARD_LIMIT_BREAK,
                filled=2 + index,
                total=5,
                stage_index=index,
                stage_count=4,
                is_max=index == 3,
            )
            for index in range(4)
        ),
    )
    return DockProgressionCatalog(
        records=(family,),
        provenance=_provenance(),
        identity_fingerprint="8" * 64,
        maximum_observed_level=125,
    )


class _FakeLevelOcr:
    def __init__(self, values: tuple[object, ...], *, mutate: bool = False) -> None:
        self.values = values
        self.mutate = mutate
        self.areas: tuple[tuple[int, int, int, int], ...] = ()

    def read_levels(self, frame, areas):
        self.areas = tuple(areas)
        if self.mutate:
            frame[:] = 0
        return self.values


class _RaisingLevelOcr:
    def read_levels(self, frame, areas):
        raise RuntimeError("backend broke")


class _FakeStarScanner:
    def __init__(
        self,
        observations: tuple[DockStarScanObservation, ...],
        *,
        mutate: bool = False,
    ) -> None:
        self.observations = observations
        self.mutate = mutate
        self.slots: tuple[DockCardSlotObservation, ...] = ()

    def scan(self, frame, slots):
        self.slots = tuple(slots)
        if self.mutate:
            frame[:] = 0
        return self.observations


class _ForbiddenNavigator(DockInventoryNavigator):
    def __init__(self) -> None:
        self.stage2_called = False

    def run_stage2(self, *args, **kwargs):
        self.stage2_called = True
        raise AssertionError("Stage 2 navigation must not run for fingerprint mismatch")


def _observed_stars(
    slot: DockCardSlotObservation, filled: int = 2
) -> DockStarScanObservation:
    area = (slot.area[0], slot.area[1] + 177, slot.area[2], slot.area[1] + 203)
    return DockStarScanObservation(
        status=DockStarStatus.OBSERVED,
        area=area,
        stars=StarObservation(filled=filled, empty=5 - filled, total=5),
        detected_total=5,
        glyphs=tuple(
            # Runtime aggregation only needs typed, fully classified glyphs.
            _glyph(index, area, index < filled)
            for index in range(5)
        ),
    )


def _glyph(index: int, area: tuple[int, int, int, int], filled: bool):
    left = area[0] + index * 10
    return DockStarGlyphObservation(
        index=index,
        state=DockStarGlyphState.FILLED if filled else DockStarGlyphState.EMPTY,
        area=(left, area[1], left + 9, area[1] + 19),
        shape_score=0.9,
        fill_ratio=0.9 if filled else 0.0,
        upper_fill_ratio=0.9 if filled else 0.0,
        fill_match_score=0.9 if filled else 0.0,
    )


def _viewport(
    card_scan: DockViewportCardScan, frame: np.ndarray
) -> DockTraversalViewport:
    return DockTraversalViewport(
        index=card_scan.viewport_index,
        scroll_position=card_scan.scroll_position,
        is_top=card_scan.viewport_index == 0,
        is_bottom=False,
        frame=frame,
    )


def test_level_roi_is_slot_relative_for_first_last_and_shifted_rows() -> None:
    ocr = _FakeLevelOcr((12, 34))
    scanner = DockLevelScanner(125, ocr=ocr)
    slots = (
        _slot(0, 211, DockCardPresence.PRESENT),
        _slot(6, 438, DockCardPresence.PRESENT),
    )

    result = scanner.scan(np.zeros((720, 1280, 3), dtype=np.uint8), slots)

    assert ocr.areas == ((170, 211, 228, 242), (1158, 438, 1216, 469))
    assert [item.value for item in result] == [12, 34]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (0, "blank_or_nonpositive_ocr"),
        (-1, "blank_or_nonpositive_ocr"),
        (999, "outside_pinned_level_range"),
        ("125", "ocr_result_not_integer"),
    ],
)
def test_level_invalid_values_are_unknown_without_clamping(
    raw: object, reason: str
) -> None:
    scanner = DockLevelScanner(125, ocr=_FakeLevelOcr((raw,)))
    result = scanner.scan(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        (_slot(0, 77, DockCardPresence.PRESENT),),
    )[0]
    assert result.status is DockLevelStatus.UNKNOWN
    assert result.value is None
    assert result.reason == reason


def test_level_scanner_owns_private_ocr_frame_and_propagates_backend_errors() -> None:
    frame = np.full((720, 1280, 3), 77, dtype=np.uint8)
    before = frame.copy()
    scanner = DockLevelScanner(125, ocr=_FakeLevelOcr((125,), mutate=True))
    assert (
        scanner.scan(frame, (_slot(0, 77, DockCardPresence.PRESENT),))[0].value == 125
    )
    assert np.array_equal(frame, before)

    with pytest.raises(DockLevelOcrError, match="backend broke"):
        DockLevelScanner(125, ocr=_RaisingLevelOcr()).scan(
            frame, (_slot(0, 77, DockCardPresence.PRESENT),)
        )


def _star_polygon(center_x: float, center_y: float) -> np.ndarray:
    points = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        radius = 8.0 if index % 2 == 0 else 3.6
        points.append(
            (
                round(center_x + radius * math.cos(angle)),
                round(center_y + radius * math.sin(angle)),
            )
        )
    return np.array(points, dtype=np.int32)


def _draw_star_row(
    frame: np.ndarray,
    slot: DockCardSlotObservation,
    states: tuple[DockStarGlyphState, ...],
) -> None:
    total = len(states)
    first = DockStarScanner.SUPPORTED_TOTAL_FIRST_CENTERS[total]
    center_y = slot.area[1] + 193
    for index, state in enumerate(states):
        center_x = slot.area[0] + first + index * DockStarScanner.STAR_SPACING
        polygon = _star_polygon(center_x, center_y)
        cv2.fillPoly(frame, [polygon], (18, 22, 26))
        cv2.polylines(frame, [polygon], True, (135, 145, 155), 1, cv2.LINE_AA)
        if state is DockStarGlyphState.FILLED:
            inner = _star_polygon(center_x, center_y)
            cv2.fillPoly(frame, [inner], (245, 205, 60))
            cv2.polylines(frame, [inner], True, (80, 60, 20), 1, cv2.LINE_AA)
        elif state is DockStarGlyphState.UNKNOWN:
            cv2.circle(
                frame,
                (round(center_x), round(center_y)),
                2,
                (245, 205, 60),
                -1,
            )


@pytest.mark.parametrize(
    "states",
    [
        (DockStarGlyphState.FILLED,) * 4,
        (DockStarGlyphState.FILLED,) * 5,
        (DockStarGlyphState.FILLED,) * 6,
        (
            DockStarGlyphState.FILLED,
            DockStarGlyphState.FILLED,
            DockStarGlyphState.EMPTY,
            DockStarGlyphState.EMPTY,
            DockStarGlyphState.EMPTY,
        ),
    ],
)
def test_star_scanner_observes_different_totals_and_mixed_states(states) -> None:
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)
    slot = _slot(0, 211, DockCardPresence.PRESENT)
    _draw_star_row(frame, slot, states)
    before = frame.copy()

    result = DockStarScanner().scan(frame, (slot,))[0]

    assert result.status is DockStarStatus.OBSERVED
    assert result.stars == StarObservation(
        filled=states.count(DockStarGlyphState.FILLED),
        empty=states.count(DockStarGlyphState.EMPTY),
        total=len(states),
    )
    assert np.array_equal(frame, before)


def test_star_scanner_uses_dynamic_first_and_last_column_geometry() -> None:
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)
    slots = (
        _slot(0, 77, DockCardPresence.PRESENT),
        _slot(6, 304, DockCardPresence.PRESENT),
    )
    for slot in slots:
        _draw_star_row(frame, slot, (DockStarGlyphState.FILLED,) * 5)

    results = DockStarScanner().scan(frame, slots)

    assert [result.stars.total for result in results if result.stars] == [5, 5]
    assert results[0].area[:2] == (93, 254)
    assert results[1].area[:2] == (1081, 481)


@pytest.mark.parametrize("total", (4, 5))
def test_star_scanner_ignores_unproven_early_yellow_peak(total: int) -> None:
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)
    slot = _slot(0, 211, DockCardPresence.PRESENT)
    _draw_star_row(frame, slot, (DockStarGlyphState.FILLED,) * total)
    cv2.circle(
        frame,
        (slot.area[0] + 31, slot.area[1] + 185),
        5,
        (245, 205, 60),
        -1,
    )

    result = DockStarScanner().scan(frame, (slot,))[0]

    assert result.status is DockStarStatus.OBSERVED
    assert result.stars == StarObservation(filled=total, empty=0, total=total)


def test_ambiguous_glyph_clipped_roi_and_wrong_geometry_are_unknown() -> None:
    scanner = DockStarScanner()
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)
    ambiguous_slot = _slot(0, 77, DockCardPresence.PRESENT)
    _draw_star_row(
        frame,
        ambiguous_slot,
        (
            DockStarGlyphState.FILLED,
            DockStarGlyphState.UNKNOWN,
            DockStarGlyphState.EMPTY,
            DockStarGlyphState.EMPTY,
            DockStarGlyphState.EMPTY,
        ),
    )
    ambiguous = scanner.scan(frame, (ambiguous_slot,))[0]
    assert ambiguous.status is DockStarStatus.UNKNOWN
    assert ambiguous.reason == "ambiguous_star_glyph"
    assert ambiguous.stars is None

    clipped = scanner.scan(frame, (_slot(0, 550, DockCardPresence.PRESENT),))[0]
    assert clipped.status is DockStarStatus.UNKNOWN
    assert clipped.reason == "star_roi_clipped"

    blank = scanner.scan(
        np.full((720, 1280, 3), 70, dtype=np.uint8),
        (_slot(0, 77, DockCardPresence.PRESENT),),
    )[0]
    assert blank.status is DockStarStatus.UNKNOWN
    assert blank.reason == "first_filled_star_not_proven"


def test_cv_backend_failure_is_operational_error_not_visual_unknown(monkeypatch) -> None:
    scanner = DockStarScanner()
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)
    slot = _slot(0, 77, DockCardPresence.PRESENT)

    def broken_canny(*_args, **_kwargs):
        raise cv2.error("backend broke")

    monkeypatch.setattr(cv2, "Canny", broken_canny)

    with pytest.raises(DockStarCvError, match="backend broke"):
        scanner.scan(frame, (slot,))


def test_match_template_score_above_one_is_clamped(monkeypatch) -> None:
    scanner = DockStarScanner()
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)
    slot = _slot(0, 77, DockCardPresence.PRESENT)

    monkeypatch.setattr(
        cv2,
        "matchTemplate",
        lambda *_args, **_kwargs: np.full((8, 120), 1.0001, dtype=np.float32),
    )
    monkeypatch.setattr(
        scanner,
        "_first_filled_star",
        lambda *_args, **_kwargs: (49, 16, 4),
    )
    monkeypatch.setattr(
        scanner,
        "_best_shape_alignment",
        lambda *_args, **_kwargs: (40, 7, 1.0),
    )

    result = scanner.scan(frame, (slot,))[0]

    assert result.status is DockStarStatus.OBSERVED
    assert result.glyphs
    assert all(glyph.fill_match_score == 1.0 for glyph in result.glyphs)


def test_empty_match_neighborhood_fails_closed_and_preserves_frame(monkeypatch) -> None:
    scanner = DockStarScanner()
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)
    slot = _slot(0, 77, DockCardPresence.PRESENT)
    _draw_star_row(frame, slot, (DockStarGlyphState.FILLED,) * 4)
    before = frame.copy()

    monkeypatch.setattr(
        cv2,
        "matchTemplate",
        lambda *_args, **_kwargs: np.ones((1, 1), dtype=np.float32),
    )
    monkeypatch.setattr(
        scanner,
        "_first_filled_star",
        lambda *_args, **_kwargs: (49, 16, 4),
    )
    monkeypatch.setattr(
        scanner,
        "_best_shape_alignment",
        lambda *_args, **_kwargs: (40, 7, 1.0),
    )

    result = scanner.scan(frame, (slot,))[0]

    assert result.status is DockStarStatus.UNKNOWN
    assert result.reason == "star_geometry_clipped"
    assert result.stars is None
    assert result.detected_total == 4
    assert np.array_equal(frame, before)


@pytest.mark.parametrize("ambiguous_index", (1, 4))
def test_any_ambiguous_glyph_keeps_aggregate_unknown(ambiguous_index: int) -> None:
    frame = np.full((720, 1280, 3), 70, dtype=np.uint8)
    slot = _slot(0, 77, DockCardPresence.PRESENT)
    _draw_star_row(frame, slot, (DockStarGlyphState.FILLED,) * 5)
    scanner = DockStarScanner()
    original_alignment = scanner._best_shape_alignment
    alignment_index = 0

    def alignment_with_one_ambiguous_glyph(*args, **kwargs):
        nonlocal alignment_index
        aligned = original_alignment(*args, **kwargs)
        current_index = alignment_index
        alignment_index += 1
        if aligned is not None and current_index == ambiguous_index:
            return aligned[0], aligned[1], scanner.SHAPE_SCORE_MIN - 0.01
        return aligned

    scanner._best_shape_alignment = alignment_with_one_ambiguous_glyph
    result = scanner.scan(frame, (slot,))[0]

    assert result.status is DockStarStatus.UNKNOWN
    assert result.reason == "ambiguous_star_glyph"
    assert any(glyph.state is DockStarGlyphState.UNKNOWN for glyph in result.glyphs)
    assert result.stars is None


def test_composition_scans_only_present_blocks_unknown_and_preserves_duplicates() -> (
    None
):
    card_scan = _card_scan(present_indexes=(0, 1))
    identity_scan = _identity_scan(card_scan)
    present = tuple(
        slot for slot in card_scan.slots if slot.presence is DockCardPresence.PRESENT
    )
    level_ocr = _FakeLevelOcr((42, 42))
    level_scanner = DockLevelScanner(125, ocr=level_ocr)
    star_scanner = _FakeStarScanner(
        tuple(_observed_stars(slot) for slot in present),
        mutate=True,
    )
    scanner = DockAttributeScanner(
        _catalog(), level_scanner=level_scanner, star_scanner=star_scanner
    )
    frame = np.full((720, 1280, 3), 80, dtype=np.uint8)

    result = scanner.scan_viewport(
        _viewport(card_scan, frame), card_scan, identity_scan
    )

    assert result.level_attempts == result.star_attempts == 2
    assert len(result.observations) == 2
    assert [item.slot_index for item in result.observations] == [0, 1]
    assert [item.level.value for item in result.observations] == [42, 42]
    assert all(
        item.progression.status is ProgressionStatus.KNOWN
        for item in result.observations
    )
    assert len(level_ocr.areas) == len(star_scanner.slots) == 2
    assert np.all(frame == 80)

    blocked_cards = _card_scan(present_indexes=(0,), unknown_indexes=(1,))
    with pytest.raises(DockAttributeIncompleteError):
        scanner.scan_viewport(
            _viewport(blocked_cards, frame),
            blocked_cards,
            _identity_scan(blocked_cards),
        )


def test_composition_rejects_mixed_identity_geometry() -> None:
    card_scan = _card_scan(present_indexes=(0,))
    identity_scan = _identity_scan(card_scan)
    wrong_identity = replace(
        identity_scan,
        observations=(replace(identity_scan.observations[0], area=(94, 77, 232, 281)),),
    )
    scanner = DockAttributeScanner(
        _catalog(),
        level_scanner=DockLevelScanner(125, ocr=_FakeLevelOcr((1,))),
        star_scanner=_FakeStarScanner((_observed_stars(card_scan.slots[0]),)),
    )
    with pytest.raises(DockAttributeInputError, match="order/geometry"):
        scanner.scan_viewport(
            _viewport(card_scan, np.zeros((720, 1280, 3), dtype=np.uint8)),
            card_scan,
            wrong_identity,
        )


def test_ambiguous_identity_keeps_level_and_stars_but_progression_unknown() -> None:
    card_scan = _card_scan(present_indexes=(0,))
    identity_scan = _identity_scan(card_scan)
    ambiguous_resolution = replace(
        identity_scan.observations[0].resolution,
        status=IdentityStatus.AMBIGUOUS,
        method=DockIdentityResolutionMethod.FUZZY,
        canonical_identity=None,
        canonical_name=None,
        reason="fixture_ambiguity",
    )
    identity_scan = replace(
        identity_scan,
        observations=(
            replace(identity_scan.observations[0], resolution=ambiguous_resolution),
        ),
    )
    scanner = DockAttributeScanner(
        _catalog(),
        level_scanner=DockLevelScanner(125, ocr=_FakeLevelOcr((37,))),
        star_scanner=_FakeStarScanner((_observed_stars(card_scan.slots[0]),)),
    )

    observation = scanner.scan_viewport(
        _viewport(card_scan, np.zeros((720, 1280, 3), dtype=np.uint8)),
        card_scan,
        identity_scan,
    ).observations[0]

    assert observation.level.value == 37
    assert observation.stars.stars == StarObservation(2, 3, 5)
    assert observation.progression.status is ProgressionStatus.UNKNOWN
    assert observation.progression.reason == "identity_not_unique"


def test_scan_dock_attributes_rejects_catalog_fingerprint_mismatch_before_stage2() -> None:
    navigator = _ForbiddenNavigator()
    identity_catalog = load_dock_identity_catalog()
    progression_catalog = _catalog()
    assert progression_catalog.identity_fingerprint != identity_catalog.fingerprint

    with pytest.raises(DockAttributeInputError, match="semantic fingerprints"):
        scan_dock_attributes(
            navigator,
            identity_catalog=identity_catalog,
            progression_catalog=progression_catalog,
        )

    assert not navigator.stage2_called


def test_scan_dock_attributes_rejects_foreign_identity_scanner_before_stage2() -> None:
    navigator = _ForbiddenNavigator()
    identity_catalog = load_dock_identity_catalog()
    progression_catalog = replace(
        _catalog(), identity_fingerprint=identity_catalog.fingerprint
    )
    first = identity_catalog.records[0]
    foreign_catalog = replace(
        identity_catalog,
        records=(
            replace(first, canonical_name=f"{first.canonical_name} Foreign"),
            *identity_catalog.records[1:],
        ),
    )
    identity_scanner = DockIdentityScanner(foreign_catalog)
    assert identity_scanner.catalog.fingerprint != identity_catalog.fingerprint

    with pytest.raises(DockAttributeInputError, match="identity_scanner"):
        scan_dock_attributes(
            navigator,
            identity_catalog=identity_catalog,
            progression_catalog=progression_catalog,
            identity_scanner=identity_scanner,
        )

    assert not navigator.stage2_called


def test_scan_dock_attributes_rejects_foreign_attribute_scanner_before_stage2() -> None:
    navigator = _ForbiddenNavigator()
    identity_catalog = load_dock_identity_catalog()
    progression_catalog = replace(
        _catalog(), identity_fingerprint=identity_catalog.fingerprint
    )
    foreign_progression_catalog = replace(
        progression_catalog,
        maximum_observed_level=progression_catalog.maximum_observed_level - 1,
    )
    attribute_scanner = DockAttributeScanner(foreign_progression_catalog)
    assert (
        attribute_scanner.progression_catalog.fingerprint
        != progression_catalog.fingerprint
    )

    with pytest.raises(DockAttributeInputError, match="attribute_scanner"):
        scan_dock_attributes(
            navigator,
            identity_catalog=identity_catalog,
            progression_catalog=progression_catalog,
            attribute_scanner=attribute_scanner,
        )

    assert not navigator.stage2_called


def test_invalid_frame_is_operational_input_error_not_visual_unknown() -> None:
    slot = _slot(0, 77, DockCardPresence.PRESENT)
    with pytest.raises(DockAttributeInputError):
        DockStarScanner().scan(np.zeros((720, 1280), dtype=np.uint8), (slot,))


def test_stage5_production_module_has_no_stage6_affinity_authority() -> None:
    source = inspect.getsource(attributes)
    for forbidden in ("EmotionScanner", "Oath", "heart detector", "AffinityState"):
        assert forbidden not in source