from pathlib import Path

import numpy as np
import pytest

from module.dock_inventory.card_grid import (
    DockCardGridScanner,
    DockCardPresence,
    DockCardPresenceEvidence,
    DockCardSlotObservation,
    DockViewportCardScan,
)
from module.dock_inventory.catalog import (
    DockCanonicalShip,
    DockCatalogProvenance,
    DockIdentityCatalog,
)
from module.dock_inventory.identity import (
    DockIdentityCollector,
    DockIdentityIncompleteError,
    DockIdentityInputError,
    DockIdentityOcrError,
    DockIdentityResolutionMethod,
    DockIdentityScanner,
    DockShipIdentityResolver,
    _DockNameOcrModel,
)
from module.dock_inventory.model import IdentityStatus
from module.dock_inventory.traversal import DockTraversalViewport


def _catalog(*records: DockCanonicalShip) -> DockIdentityCatalog:
    return DockIdentityCatalog(
        records=tuple(records),
        provenance=DockCatalogProvenance(
            source_repository="fixture/repo",
            source_commit="1" * 40,
            source_path="ship_data.json",
            source_blob_sha="2" * 40,
            source_sha256="3" * 64,
            source_generator_path="extractor.py",
            source_generator_blob_sha="4" * 40,
            supplemental_source_repository="fixture/lua",
            supplemental_source_commit="5" * 40,
            supplemental_source_path="fleet_tech_ship_class.lua",
            supplemental_source_blob_sha="6" * 40,
            selection_contract="fixture",
        ),
    )


@pytest.fixture
def catalog() -> DockIdentityCatalog:
    return _catalog(
        DockCanonicalShip("azur_lane_ship_group:1", "Enterprise"),
        DockCanonicalShip("azur_lane_ship_group:2", "Sovetsky Soyuz"),
        DockCanonicalShip("azur_lane_ship_group:3", "Sovetskaya Rossiya"),
        DockCanonicalShip("azur_lane_ship_group:4", "Laffey II"),
    )


def test_exact_case_nfkc_and_whitespace_resolution(catalog: DockIdentityCatalog) -> None:
    resolver = DockShipIdentityResolver(catalog)

    for raw in ("Enterprise", "enterprise", "Ｅｎｔｅｒｐｒｉｓｅ", "Enter prise"):
        result = resolver.resolve(raw)
        assert result.status is IdentityStatus.MATCHED
        assert result.method is DockIdentityResolutionMethod.EXACT
        assert result.canonical_name == "Enterprise"
        assert result.raw_name_ocr == raw


def test_normalized_exact_collision_is_ambiguous_without_fuzzy_override() -> None:
    resolver = DockShipIdentityResolver(
        _catalog(
            DockCanonicalShip("azur_lane_ship_group:1", "A B"),
            DockCanonicalShip("azur_lane_ship_group:2", "AB"),
        )
    )

    result = resolver.resolve("AB")

    assert result.status is IdentityStatus.AMBIGUOUS
    assert result.method is DockIdentityResolutionMethod.EXACT
    assert result.candidate_count == 2
    assert result.reason == "normalized_exact_collision"


def test_unique_truncated_prefix_matches_but_ambiguous_and_short_do_not() -> None:
    resolver = DockShipIdentityResolver(
        _catalog(
            DockCanonicalShip("azur_lane_ship_group:1", "Sovetsky Soyuz"),
            DockCanonicalShip("azur_lane_ship_group:2", "Sovetskaya Rossiya"),
            DockCanonicalShip("azur_lane_ship_group:3", "Sovereign"),
        )
    )

    unique = resolver.resolve("Sovetsky So..")
    ambiguous = resolver.resolve("Sovetsk…")
    short = resolver.resolve("Sov..")

    assert unique.status is IdentityStatus.MATCHED
    assert unique.method is DockIdentityResolutionMethod.TRUNCATED_PREFIX
    assert unique.canonical_name == "Sovetsky Soyuz"
    assert ambiguous.status is IdentityStatus.AMBIGUOUS
    assert ambiguous.method is DockIdentityResolutionMethod.TRUNCATED_PREFIX
    assert short.status is IdentityStatus.UNRESOLVED
    assert short.reason == "truncated_prefix_too_short"


def test_retrofit_ui_suffix_maps_only_through_an_exact_unique_base() -> None:
    resolver = DockShipIdentityResolver(
        _catalog(
            DockCanonicalShip("azur_lane_ship_group:1", "San Diego"),
            DockCanonicalShip("azur_lane_ship_group:2", "York"),
            DockCanonicalShip("azur_lane_ship_group:3", "Enterprise"),
            DockCanonicalShip("azur_lane_ship_group:4", "Enterprise"),
        )
    )

    truncated = resolver.resolve("San Diego (..")
    full = resolver.resolve("York (Retrofit)")
    ambiguous = resolver.resolve("Enterprise (Retrof..")
    arbitrary = resolver.resolve("York (Other..")

    assert truncated.status is IdentityStatus.MATCHED
    assert truncated.method is DockIdentityResolutionMethod.TRUNCATED_PREFIX
    assert truncated.reason == "retrofit_display_suffix"
    assert full.status is IdentityStatus.MATCHED
    assert full.method is DockIdentityResolutionMethod.EXACT
    assert full.reason == "retrofit_display_suffix"
    assert ambiguous.status is IdentityStatus.AMBIGUOUS
    assert ambiguous.reason == "ambiguous_retrofit_base"
    assert arbitrary.status is IdentityStatus.UNRESOLVED


def test_fuzzy_requires_score_and_runner_up_margin() -> None:
    strong = DockShipIdentityResolver(
        _catalog(
            DockCanonicalShip("azur_lane_ship_group:1", "Enterprise"),
            DockCanonicalShip("azur_lane_ship_group:2", "Belfast"),
        )
    ).resolve("Enterprlse")
    weak = DockShipIdentityResolver(
        _catalog(
            DockCanonicalShip("azur_lane_ship_group:1", "Enterprise"),
            DockCanonicalShip("azur_lane_ship_group:2", "Belfast"),
        )
    ).resolve("XQZ")
    close = DockShipIdentityResolver(
        _catalog(
            DockCanonicalShip("azur_lane_ship_group:1", "Enterprxse"),
            DockCanonicalShip("azur_lane_ship_group:2", "Enterpryse"),
        )
    ).resolve("Enterprase")

    assert strong.status is IdentityStatus.MATCHED
    assert strong.method is DockIdentityResolutionMethod.FUZZY
    assert strong.best_score >= DockShipIdentityResolver.FUZZY_MIN_SCORE
    assert strong.best_score - strong.runner_up_score >= DockShipIdentityResolver.FUZZY_MIN_MARGIN
    assert weak.status is IdentityStatus.UNRESOLVED
    assert weak.reason == "fuzzy_below_threshold"
    assert close.status is IdentityStatus.AMBIGUOUS
    assert close.reason == "fuzzy_margin_too_small"


def test_fuzzy_tie_is_deterministic_but_never_forced() -> None:
    resolver = DockShipIdentityResolver(
        _catalog(
            DockCanonicalShip("azur_lane_ship_group:10", "Enterprxse"),
            DockCanonicalShip("azur_lane_ship_group:20", "Enterpryse"),
        )
    )

    result = resolver.resolve("Enterprase")

    assert result.status is IdentityStatus.AMBIGUOUS
    assert result.candidates[:2] == (
        "azur_lane_ship_group:10",
        "azur_lane_ship_group:20",
    )


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_ocr_is_unresolved(raw: str, catalog: DockIdentityCatalog) -> None:
    result = DockShipIdentityResolver(catalog).resolve(raw)

    assert result.status is IdentityStatus.UNRESOLVED
    assert result.method is DockIdentityResolutionMethod.NONE
    assert result.raw_name_ocr == raw


class _FakeOcr:
    def __init__(self, values: tuple[str, ...], *, mutate: bool = False) -> None:
        self.values = values
        self.mutate = mutate
        self.calls: list[tuple[tuple[int, int, int, int], ...]] = []
        self.frames: list[np.ndarray] = []

    def read_names(self, frame, areas):
        areas = tuple(areas)
        self.calls.append(areas)
        self.frames.append(frame)
        if self.mutate:
            frame[0, 0] = 255
        return self.values


class _FailingOcr:
    def __init__(self, *, mutate: bool = False) -> None:
        self.mutate = mutate

    def read_names(self, frame, areas):
        if self.mutate:
            frame[0, 0] = 255
        raise RuntimeError("fixture engine failure")


def _viewport(index: int = 0, position: float = 0.0) -> DockTraversalViewport:
    return DockTraversalViewport(
        index=index,
        scroll_position=position,
        is_top=position == 0.0,
        is_bottom=position == 1.0,
        frame=np.zeros((720, 1280, 3), dtype=np.uint8),
    )


def _card_scan(
    states: tuple[DockCardPresence, ...],
    *,
    index: int = 0,
    position: float = 0.0,
    row_origin: int = 76,
) -> DockViewportCardScan:
    assert len(states) == 7
    geometry = DockCardGridScanner()
    evidence = DockCardPresenceEvidence(40.0, 0.2, 10.0)
    slots = tuple(
        DockCardSlotObservation(
            slot_index=column,
            column=column,
            row=0,
            area=geometry._column_area(column, row_origin),
            presence=state,
            evidence=evidence,
        )
        for column, state in enumerate(states)
    )
    return DockViewportCardScan(
        viewport_index=index,
        scroll_position=position,
        registered_row_origins=(row_origin,),
        slots=slots,
    )


def test_dynamic_slot_relative_roi_first_last_columns_and_present_only(
    catalog: DockIdentityCatalog,
) -> None:
    fake = _FakeOcr(("Enterprise", "Laffey II"))
    scanner = DockIdentityScanner(catalog, name_ocr=fake)
    viewport = _viewport()
    card_scan = _card_scan(
        (
            DockCardPresence.PRESENT,
            DockCardPresence.ABSENT,
            DockCardPresence.ABSENT,
            DockCardPresence.ABSENT,
            DockCardPresence.ABSENT,
            DockCardPresence.ABSENT,
            DockCardPresence.PRESENT,
        )
    )

    result = scanner.scan_viewport(viewport, card_scan)

    assert len(fake.calls) == 1
    assert len(fake.calls[0]) == 2
    assert fake.calls[0][0] == (83, 236, 235, 266)
    assert fake.calls[0][1] == (1071, 236, 1223, 266)
    assert [item.slot_index for item in result.observations] == [0, 6]
    assert result.matched_count == 2
    assert not np.shares_memory(fake.frames[0], viewport.frame)


def test_name_roi_tracks_dynamic_row_y(catalog: DockIdentityCatalog) -> None:
    fake = _FakeOcr(("Enterprise",))
    scanner = DockIdentityScanner(catalog, name_ocr=fake)

    scanner.scan_viewport(
        _viewport(),
        _card_scan(
            (DockCardPresence.PRESENT,) + (DockCardPresence.ABSENT,) * 6,
            row_origin=300,
        ),
    )

    assert fake.calls[0][0][1:] == (460, 235, 490)


def test_unknown_blocks_before_ocr(catalog: DockIdentityCatalog) -> None:
    fake = _FakeOcr(())
    scanner = DockIdentityScanner(catalog, name_ocr=fake)

    with pytest.raises(DockIdentityIncompleteError, match="UNKNOWN"):
        scanner.scan_viewport(
            _viewport(),
            _card_scan((DockCardPresence.UNKNOWN,) + (DockCardPresence.ABSENT,) * 6),
        )

    assert fake.calls == []


def test_viewport_mismatch_and_operational_ocr_failure_are_rejected(
    catalog: DockIdentityCatalog,
) -> None:
    scanner = DockIdentityScanner(catalog, name_ocr=_FakeOcr(("Enterprise",)))
    with pytest.raises(DockIdentityInputError, match="index mismatch"):
        scanner.scan_viewport(
            _viewport(index=1),
            _card_scan((DockCardPresence.PRESENT,) + (DockCardPresence.ABSENT,) * 6),
        )

    wrong_count = DockIdentityScanner(catalog, name_ocr=_FakeOcr(()))
    with pytest.raises(DockIdentityOcrError, match="не совпало"):
        wrong_count.scan_viewport(
            _viewport(),
            _card_scan((DockCardPresence.PRESENT,) + (DockCardPresence.ABSENT,) * 6),
        )

    failed = DockIdentityScanner(catalog, name_ocr=_FailingOcr())
    with pytest.raises(DockIdentityOcrError, match="Операционный сбой"):
        failed.scan_viewport(
            _viewport(),
            _card_scan((DockCardPresence.PRESENT,) + (DockCardPresence.ABSENT,) * 6),
        )


def test_source_frame_is_immutable(catalog: DockIdentityCatalog) -> None:
    viewport = _viewport()
    before = viewport.frame.copy()
    fake = _FakeOcr(("Enterprise",), mutate=True)
    scanner = DockIdentityScanner(catalog, name_ocr=fake)

    result = scanner.scan_viewport(
        viewport,
        _card_scan((DockCardPresence.PRESENT,) + (DockCardPresence.ABSENT,) * 6),
    )

    assert result.matched_count == 1
    assert np.array_equal(viewport.frame, before)
    assert fake.frames[0][0, 0].tolist() == [255, 255, 255]
    assert not np.shares_memory(fake.frames[0], viewport.frame)


def test_source_frame_is_immutable_for_blank_and_ambiguity() -> None:
    cases = (
        (_catalog(DockCanonicalShip("azur_lane_ship_group:1", "Enterprise")), ""),
        (
            _catalog(
                DockCanonicalShip("azur_lane_ship_group:1", "A B"),
                DockCanonicalShip("azur_lane_ship_group:2", "AB"),
            ),
            "AB",
        ),
    )
    for catalog, raw_name in cases:
        viewport = _viewport()
        before = viewport.frame.copy()
        scanner = DockIdentityScanner(
            catalog,
            name_ocr=_FakeOcr((raw_name,), mutate=True),
        )

        scanner.scan_viewport(
            viewport,
            _card_scan((DockCardPresence.PRESENT,) + (DockCardPresence.ABSENT,) * 6),
        )

        assert np.array_equal(viewport.frame, before)


def test_source_frame_is_immutable_when_ocr_raises(catalog: DockIdentityCatalog) -> None:
    viewport = _viewport()
    before = viewport.frame.copy()
    scanner = DockIdentityScanner(catalog, name_ocr=_FailingOcr(mutate=True))

    with pytest.raises(DockIdentityOcrError, match="Операционный сбой"):
        scanner.scan_viewport(
            viewport,
            _card_scan((DockCardPresence.PRESENT,) + (DockCardPresence.ABSENT,) * 6),
        )

    assert np.array_equal(viewport.frame, before)


def test_duplicate_identity_appearances_are_preserved(catalog: DockIdentityCatalog) -> None:
    scanner = DockIdentityScanner(
        catalog,
        name_ocr=_FakeOcr(("Enterprise", "Enterprise")),
    )
    result = scanner.scan_viewport(
        _viewport(),
        _card_scan(
            (DockCardPresence.PRESENT, DockCardPresence.PRESENT)
            + (DockCardPresence.ABSENT,) * 5
        ),
    )

    assert len(result.observations) == 2
    assert (
        result.observations[0].resolution.canonical_identity
        == result.observations[1].resolution.canonical_identity
    )


def test_cross_viewport_duplicate_appearances_are_not_deduplicated(
    catalog: DockIdentityCatalog,
) -> None:
    identity_scanner = DockIdentityScanner(
        catalog,
        name_ocr=_FakeOcr(("Enterprise",)),
    )

    class _CardScanner:
        def scan_viewport(self, viewport):
            return _card_scan(
                (DockCardPresence.PRESENT,) + (DockCardPresence.ABSENT,) * 6,
                index=viewport.index,
                position=viewport.scroll_position,
            )

    collector = DockIdentityCollector(identity_scanner, card_scanner=_CardScanner())
    collector(_viewport(index=0, position=0.0))
    collector(_viewport(index=1, position=0.5))

    assert len(collector.viewports) == 2
    assert len(collector.viewports[0].observations) == 1
    assert len(collector.viewports[1].observations) == 1
    assert (
        collector.viewports[0].observations[0].resolution.canonical_identity
        == collector.viewports[1].observations[0].resolution.canonical_identity
    )


def test_stage4_source_has_no_network_screenshot_or_later_stage_dependencies() -> None:
    source = (
        Path(__file__).parents[1] / "module" / "dock_inventory" / "identity.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "device.screenshot",
        "requests",
        "urllib",
        "httpx",
        "LevelScanner",
        "RarityScanner",
        "EmotionScanner",
        "DHash",
    ):
        assert forbidden not in source


def test_white_and_pink_preprocessing_extracts_both_without_mutating_input() -> None:
    crop = np.full((30, 152, 3), (20, 40, 70), dtype=np.uint8)
    crop[8:16, 20:25] = (255, 255, 255)
    crop[8:16, 40:45] = _DockNameOcrModel.PINK_LETTER
    before = crop.copy()
    model = _DockNameOcrModel([(0, 0, 152, 30)], name="fixture")

    processed = model.pre_process(crop)

    assert processed.shape == (19, 148)
    assert processed[4:12, 16:21].min() < 120
    assert processed[4:12, 36:41].min() < 120
    assert processed[:, 60:80].min() >= 120
    assert np.array_equal(crop, before)


def test_edge_noise_removal_keeps_internal_glyph_and_drops_border_component() -> None:
    image = np.full((19, 30), 255, dtype=np.uint8)
    image[2:17, 0:2] = 0
    image[2:17, 10:12] = 0

    cleaned = _DockNameOcrModel._remove_edge_noise(image.copy())

    assert cleaned[:, :2].min() == 255
    assert cleaned[:, 10:12].min() == 0
