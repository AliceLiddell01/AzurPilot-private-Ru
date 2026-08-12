from __future__ import annotations

import json
from pathlib import Path

import pytest

import module.dock_inventory as dock_inventory
from module.dock_inventory.catalog import load_dock_identity_catalog
from module.dock_inventory.model import (
    CanonicalShipIdentity,
    IdentityStatus,
    StarObservation,
)
from module.dock_inventory.progression import (
    DockProgressionCatalog,
    DockProgressionCatalogError,
    DockProgressionFamily,
    DockProgressionObservation,
    DockProgressionProvenance,
    DockProgressionState,
    ProgressionKind,
    ProgressionStatus,
    derive_dock_progression,
    load_dock_progression_catalog,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPOSITORY_ROOT / "assets" / "ship" / "dock_progression_catalog.json"


def _provenance() -> DockProgressionProvenance:
    return DockProgressionProvenance(
        source_repository="example/source",
        source_commit="1" * 40,
        source_path="ship_data.json",
        source_blob_sha="2" * 40,
        source_sha256="3" * 64,
        supplemental_source_repository="example/lua",
        supplemental_source_commit="4" * 40,
        supplemental_template_path="template.lua",
        supplemental_template_blob_sha="5" * 40,
        blueprint_source_path="blueprint.lua",
        blueprint_source_blob_sha="6" * 40,
        level_source_path="ship_level.lua",
        level_source_blob_sha="7" * 40,
        selection_contract="fixture",
    )


def _standard_family(
    group: int,
    *,
    base: int,
    total: int,
    family_type: str = "ordinary",
) -> DockProgressionFamily:
    return DockProgressionFamily(
        canonical_id=f"azur_lane_ship_group:{group}",
        family_type=family_type,
        states=tuple(
            DockProgressionState(
                semantic_id=f"limit_break:{index}",
                kind=ProgressionKind.STANDARD_LIMIT_BREAK,
                filled=base + index,
                total=total,
                stage_index=index,
                stage_count=4,
                is_max=index == 3,
            )
            for index in range(4)
        ),
    )


def _catalog(*families: DockProgressionFamily) -> DockProgressionCatalog:
    return DockProgressionCatalog(
        records=tuple(
            sorted(
                families, key=lambda family: int(family.canonical_id.rsplit(":", 1)[1])
            )
        ),
        provenance=_provenance(),
        identity_fingerprint="8" * 64,
        maximum_observed_level=125,
    )


def _derive(
    catalog: DockProgressionCatalog,
    group: int | None,
    stars: StarObservation | None,
    *,
    status: IdentityStatus = IdentityStatus.MATCHED,
):
    return derive_dock_progression(
        identity_status=status,
        canonical_identity=(
            CanonicalShipIdentity(f"azur_lane_ship_group:{group}")
            if group is not None
            else None
        ),
        observed_stars=stars,
        catalog=catalog,
    )


def test_package_reexports_progression_provenance() -> None:
    assert dock_inventory.DockProgressionProvenance is DockProgressionProvenance


def test_known_progression_requires_observed_stars_and_unknown_semantics_stay_valid() -> None:
    stars = StarObservation(2, 3, 5)
    kwargs = {
        "status": ProgressionStatus.KNOWN,
        "kind": ProgressionKind.STANDARD_LIMIT_BREAK,
        "stage_index": 0,
        "stage_count": 4,
        "is_max": False,
        "matching_semantic_ids": ("limit_break:0",),
    }

    with pytest.raises(ValueError, match="observed_stars"):
        DockProgressionObservation(observed_stars=None, **kwargs)

    known = DockProgressionObservation(observed_stars=stars, **kwargs)
    assert known.observed_stars == stars

    with pytest.raises(ValueError, match="is_max"):
        DockProgressionObservation(
            observed_stars=stars,
            **{**kwargs, "is_max": None},
        )

    unknown_without_stars = DockProgressionObservation(
        status=ProgressionStatus.UNKNOWN,
        kind=ProgressionKind.UNKNOWN,
        observed_stars=None,
        reason="fixture_unknown",
    )
    unknown_with_stars = DockProgressionObservation(
        status=ProgressionStatus.UNKNOWN,
        kind=ProgressionKind.UNKNOWN,
        observed_stars=stars,
        reason="fixture_unknown",
    )
    assert unknown_without_stars.observed_stars is None
    assert unknown_with_stars.observed_stars == stars

    derived = _derive(_catalog(_standard_family(200, base=2, total=5)), 200, stars)
    assert derived.status is ProgressionStatus.KNOWN
    assert derived.observed_stars == stars


def test_standard_progression_supports_different_base_and_total_counts() -> None:
    catalog = _catalog(
        _standard_family(100, base=1, total=4),
        _standard_family(200, base=2, total=5),
        _standard_family(300, base=3, total=6, family_type="type_ii"),
    )

    base = _derive(catalog, 100, StarObservation(1, 3, 4))
    middle = _derive(catalog, 200, StarObservation(4, 1, 5))
    maximum = _derive(catalog, 300, StarObservation(6, 0, 6))

    assert (base.status, base.kind, base.stage_index, base.is_max) == (
        ProgressionStatus.KNOWN,
        ProgressionKind.STANDARD_LIMIT_BREAK,
        0,
        False,
    )
    assert middle.stage_index == 2
    assert (maximum.stage_index, maximum.is_max) == (3, True)


def test_progression_depends_on_family_metadata_not_global_empty_count() -> None:
    blueprint = DockProgressionFamily(
        canonical_id="azur_lane_ship_group:200",
        family_type="blueprint",
        states=(
            DockProgressionState(
                semantic_id="source_state:2001",
                kind=ProgressionKind.NONSTANDARD,
                filled=5,
                total=6,
            ),
        ),
    )
    catalog = _catalog(
        _standard_family(100, base=1, total=4),
        blueprint,
    )

    same_empty_a = _derive(catalog, 100, StarObservation(3, 1, 4))
    same_empty_b = _derive(catalog, 200, StarObservation(5, 1, 6))

    assert same_empty_a.stage_index == 2
    assert same_empty_b.kind is ProgressionKind.NONSTANDARD
    assert same_empty_b.stage_index is None
    conflict = _derive(catalog, 100, StarObservation(5, 1, 6))
    assert conflict.status is ProgressionStatus.UNKNOWN
    assert conflict.reason == "visual_static_conflict"


def test_single_state_and_blueprint_families_are_known_nonstandard() -> None:
    single = DockProgressionFamily(
        canonical_id="azur_lane_ship_group:10000",
        family_type="single_state",
        states=(
            DockProgressionState(
                semantic_id="source_state:100001",
                kind=ProgressionKind.NONSTANDARD,
                filled=4,
                total=4,
                is_max=True,
            ),
        ),
    )
    blueprint = DockProgressionFamily(
        canonical_id="azur_lane_ship_group:29901",
        family_type="blueprint",
        states=(
            DockProgressionState(
                semantic_id="source_state:299011",
                kind=ProgressionKind.NONSTANDARD,
                filled=3,
                total=6,
            ),
        ),
    )
    catalog = _catalog(single, blueprint)

    for group, stars in (
        (10000, StarObservation(4, 0, 4)),
        (29901, StarObservation(3, 3, 6)),
    ):
        result = _derive(catalog, group, stars)
        assert result.status is ProgressionStatus.KNOWN
        assert result.kind is ProgressionKind.NONSTANDARD
        assert result.stage_index is None


def test_retrofit_duplicate_is_ambiguous_and_never_becomes_lb4() -> None:
    ordinary = _standard_family(20101, base=2, total=5)
    family = DockProgressionFamily(
        canonical_id=ordinary.canonical_id,
        family_type="ordinary_with_retrofit",
        states=ordinary.states
        + (
            DockProgressionState(
                semantic_id="retrofit:201514",
                kind=ProgressionKind.NONSTANDARD,
                filled=5,
                total=5,
                is_max=True,
            ),
        ),
    )

    result = _derive(_catalog(family), 20101, StarObservation(5, 0, 5))

    assert result.status is ProgressionStatus.UNKNOWN
    assert result.reason == "ambiguous_static_mapping"
    assert result.stage_index is None
    assert result.matching_semantic_ids == ("limit_break:3", "retrofit:201514")


@pytest.mark.parametrize(
    "identity_status", [IdentityStatus.AMBIGUOUS, IdentityStatus.UNRESOLVED]
)
def test_non_unique_identity_preserves_raw_stars_but_progression_is_unknown(
    identity_status: IdentityStatus,
) -> None:
    stars = StarObservation(2, 3, 5)
    result = _derive(
        _catalog(_standard_family(200, base=2, total=5)),
        None,
        stars,
        status=identity_status,
    )

    assert result.status is ProgressionStatus.UNKNOWN
    assert result.reason == "identity_not_unique"
    assert result.observed_stars == stars


def test_unknown_star_evidence_blocks_progression_before_identity() -> None:
    result = _derive(_catalog(_standard_family(200, base=2, total=5)), 200, None)
    assert result.reason == "star_evidence_unknown"


def test_identity_without_progression_family_is_unknown() -> None:
    stars = StarObservation(2, 3, 5)
    result = _derive(
        _catalog(_standard_family(200, base=2, total=5)),
        300,
        stars,
    )

    assert result.status is ProgressionStatus.UNKNOWN
    assert result.reason == "canonical_family_missing"
    assert result.observed_stars == stars


def test_catalog_rejects_nonstandard_state_with_limit_break_index() -> None:
    with pytest.raises(DockProgressionCatalogError):
        DockProgressionState(
            semantic_id="retrofit:1",
            kind=ProgressionKind.NONSTANDARD,
            filled=5,
            total=5,
            stage_index=4,
        )


def test_progression_catalog_invalid_utf8_is_typed_error(tmp_path: Path) -> None:
    catalog_path = tmp_path / "dock_progression_catalog.json"
    catalog_path.write_bytes(b"\xff")

    with pytest.raises(DockProgressionCatalogError, match="UTF-8"):
        load_dock_progression_catalog(catalog_path)


@pytest.mark.parametrize("identity_fingerprint", [None, 123])
def test_progression_catalog_non_string_identity_fingerprint_is_typed_error(
    identity_fingerprint: object,
) -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["identity_fingerprint"] = identity_fingerprint

    with pytest.raises(DockProgressionCatalogError, match="identity_fingerprint"):
        DockProgressionCatalog.from_mapping(payload)


def test_tracked_progression_catalog_matches_identity_catalog_and_is_deterministic() -> (
    None
):
    identity = load_dock_identity_catalog()
    first = load_dock_progression_catalog()
    second = load_dock_progression_catalog()

    assert first.identity_fingerprint == identity.fingerprint
    assert len(first.records) == len(identity.records) == 875
    assert first.maximum_observed_level == 125
    assert first.fingerprint == second.fingerprint
    assert first.provenance.source_commit == "42ffc9566870ce3074c12d4faabf19bfaaafaf71"
    assert (
        first.provenance.supplemental_source_commit
        == "ef5a7ee5068e7a25b8abc0db67c2f185b87615cb"
    )


def test_catalog_json_does_not_embed_unrelated_upstream_payload() -> None:
    catalog_text = CATALOG_PATH.read_text(encoding="utf-8")
    payload = json.loads(catalog_text)
    # Regression guard against accidentally embedding the multi-megabyte upstream payload.
    assert CATALOG_PATH.stat().st_size < 1_100_000
    assert payload["records"]
    for record in payload["records"]:
        assert set(record) == {"canonical_id", "family_type", "states"}
    assert "attrs" not in catalog_text