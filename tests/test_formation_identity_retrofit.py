import numpy as np
import pytest

from module.dock_inventory.catalog import (
    DockCanonicalShip,
    DockCatalogProvenance,
    DockIdentityCatalog,
)
from module.dock_inventory.identity import (
    DockIdentityResolutionMethod,
    DockShipIdentityResolver,
)
from module.dock_inventory.model import IdentityStatus
from module.formation.model import FormationFleetSide
from module.formation.scanner import FormationFleetInfoScanner, FormationPresenceEvidence


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


def _retrofit_catalog() -> DockIdentityCatalog:
    return _catalog(
        DockCanonicalShip("azur_lane_ship_group:1", "San Diego"),
        DockCanonicalShip("azur_lane_ship_group:2", "Hammann"),
        DockCanonicalShip("azur_lane_ship_group:3", "Unicorn"),
        DockCanonicalShip("azur_lane_ship_group:4", "York"),
    )


@pytest.mark.parametrize(
    ("raw", "canonical_name"),
    (
        ("San Diego (Retro1", "San Diego"),
        ("Hammann (Retro", "Hammann"),
    ),
)
def test_observed_unmarked_retrofit_truncation_resolves_exact_base(
    raw: str,
    canonical_name: str,
) -> None:
    result = DockShipIdentityResolver(_retrofit_catalog()).resolve(raw)

    assert result.status is IdentityStatus.MATCHED
    assert result.method is DockIdentityResolutionMethod.TRUNCATED_PREFIX
    assert result.canonical_name == canonical_name
    assert result.reason == "retrofit_display_suffix"
    assert result.raw_name_ocr == raw


def test_full_and_explicit_ellipsis_retrofit_paths_do_not_regress() -> None:
    resolver = DockShipIdentityResolver(_retrofit_catalog())

    full = resolver.resolve("Unicorn (Retrofit)")
    ellipsis = resolver.resolve("York (Retrof..")

    assert full.status is IdentityStatus.MATCHED
    assert full.method is DockIdentityResolutionMethod.EXACT
    assert full.canonical_name == "Unicorn"
    assert ellipsis.status is IdentityStatus.MATCHED
    assert ellipsis.method is DockIdentityResolutionMethod.TRUNCATED_PREFIX
    assert ellipsis.canonical_name == "York"


@pytest.mark.parametrize(
    "raw",
    (
        "York (",
        "York (r",
        "York (retr",
        "York (retrox",
        "York (retro11",
        "York (Other",
    ),
)
def test_unmarked_parenthetical_suffix_requires_strong_bounded_retrofit_evidence(
    raw: str,
) -> None:
    result = DockShipIdentityResolver(_retrofit_catalog()).resolve(raw)

    assert result.status is IdentityStatus.UNRESOLVED
    assert result.canonical_name is None


def test_unmarked_retrofit_base_collision_remains_ambiguous() -> None:
    resolver = DockShipIdentityResolver(
        _catalog(
            DockCanonicalShip("azur_lane_ship_group:1", "Enterprise"),
            DockCanonicalShip("azur_lane_ship_group:2", "Enterprise"),
        )
    )

    result = resolver.resolve("Enterprise (Retro")

    assert result.status is IdentityStatus.AMBIGUOUS
    assert result.method is DockIdentityResolutionMethod.TRUNCATED_PREFIX
    assert result.reason == "ambiguous_retrofit_base"
    assert result.candidate_count == 2


def test_retrofit_fix_does_not_weaken_generic_fuzzy_thresholds() -> None:
    assert DockShipIdentityResolver.FUZZY_MIN_SCORE == 0.86
    assert DockShipIdentityResolver.FUZZY_MIN_MARGIN == 0.08


class _NameOcr:
    def __init__(self, raw: str) -> None:
        self.raw = raw

    def read_names(self, frame, areas):
        assert len(tuple(areas)) == 1
        return (self.raw,)


def test_formation_scanner_propagates_observed_retrofit_match(monkeypatch) -> None:
    scanner = FormationFleetInfoScanner(
        _retrofit_catalog(),
        name_ocr=_NameOcr("San Diego (Retro1"),
    )

    monkeypatch.setattr(
        scanner,
        "presence_evidence",
        lambda frame, geometry: FormationPresenceEvidence(
            stats_green_ratio=1.0 if (
                geometry.side is FormationFleetSide.VANGUARD and geometry.position == 2
            ) else 0.0,
            occupied=(
                geometry.side is FormationFleetSide.VANGUARD and geometry.position == 2
            ),
        ),
    )

    snapshot = scanner.scan(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        fleet_index=2,
    )
    slot = next(
        item
        for item in snapshot.slots
        if item.side is FormationFleetSide.VANGUARD and item.position == 2
    )

    assert slot.identity_status is IdentityStatus.MATCHED
    assert slot.raw_name_ocr == "San Diego (Retro1"
    assert slot.canonical_name == "San Diego"
    assert snapshot.complete is True
