import hashlib
import json
from pathlib import Path

import pytest

from dev_tools.dock_identity_catalog import build_catalog, canonical_json_bytes
from module.dock_inventory.catalog import (
    CATALOG_IDENTITY_SCHEME,
    DockCanonicalShip,
    DockCatalogProvenance,
    DockIdentityCatalog,
    DockIdentityCatalogError,
    load_dock_identity_catalog,
    normalize_ship_name,
)


def _provenance() -> DockCatalogProvenance:
    return DockCatalogProvenance(
        source_repository="fixture/repo",
        source_commit="1" * 40,
        source_path="assets/ship/ship_data.json",
        source_blob_sha="2" * 40,
        source_sha256="3" * 64,
        source_generator_path="dev_tools/ship_data_extractor.py",
        source_generator_blob_sha="4" * 40,
        supplemental_source_repository="fixture/lua",
        supplemental_source_commit="5" * 40,
        supplemental_source_path="EN/sharecfg/fleet_tech_ship_class.lua",
        supplemental_source_blob_sha="6" * 40,
        selection_contract="fixture selection",
    )


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "language": "en",
        "identity_scheme": CATALOG_IDENTITY_SCHEME,
        "provenance": {
            field: getattr(_provenance(), field)
            for field in _provenance().__dataclass_fields__
        },
        "records": [
            {
                "canonical_id": "azur_lane_ship_group:1",
                "canonical_name": "Enterprise",
                "aliases": ["Enterprise (Retrofit)"],
            },
            {
                "canonical_id": "azur_lane_ship_group:2",
                "canonical_name": "Neptune",
                "aliases": [],
            },
        ],
    }


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_tracked_catalog_has_provenance_collisions_and_stable_fingerprint() -> None:
    catalog = load_dock_identity_catalog()

    assert len(catalog.records) == 875
    assert catalog.alias_count == 35
    assert catalog.language == "en"
    assert catalog.identity_scheme == CATALOG_IDENTITY_SCHEME
    assert catalog.provenance.source_commit == "42ffc9566870ce3074c12d4faabf19bfaaafaf71"
    assert catalog.provenance.source_blob_sha == "6f3bd2c21966a40b40c91b2c5f889019f83063fa"
    assert catalog.provenance.supplemental_source_commit == (
        "89048396054a2ad908dc12f14ef6f29a2bd552c9"
    )
    assert catalog.provenance.supplemental_source_blob_sha == (
        "fcdd46ac985dcf5478a9685bdc5b248076b68ae0"
    )
    assert catalog.fingerprint == "52958a52a0e4c73265f9f73d839ad5b60e26a0b8c5ebf3ffb5e6a6e197535f90"
    collisions = catalog.normalized_collisions
    assert set(collisions) == {"enterprise", "fubuki", "kasumi", "neptune"}
    assert len(collisions["enterprise"]) == 2
    assert catalog.candidates_for_exact_name("nürnbergmeta")[0].canonical_id == (
        "azur_lane_ship_group:970213"
    )


def test_catalog_normalization_preserves_punctuation_suffixes_and_collisions() -> None:
    assert normalize_ship_name("  Ａ  B  ") == "ab"
    assert normalize_ship_name("U-556 META") == "u-556meta"
    assert normalize_ship_name("U556 META") != normalize_ship_name("U-556 META")
    assert normalize_ship_name("Laffey II") != normalize_ship_name("Laffey")

    catalog = DockIdentityCatalog(
        records=(
            DockCanonicalShip("azur_lane_ship_group:1", "A B"),
            DockCanonicalShip("azur_lane_ship_group:2", "AB"),
        ),
        provenance=_provenance(),
    )
    assert len(catalog.candidates_for_exact_name("ab")) == 2


def test_catalog_fingerprint_ignores_mapping_noise_but_changes_with_semantics() -> None:
    first = DockIdentityCatalog.from_mapping(_payload())
    reordered = json.loads(json.dumps(_payload(), sort_keys=True))
    second = DockIdentityCatalog.from_mapping(reordered)
    changed = _payload()
    changed["records"][0]["canonical_name"] = "Enterprise II"  # type: ignore[index]
    third = DockIdentityCatalog.from_mapping(changed)

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != third.fingerprint


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(schema_version=2),
        lambda value: value.update(language="jp"),
        lambda value: value.update(records={}),
        lambda value: value["records"][0].update(canonical_name=" "),
        lambda value: value["records"].append(dict(value["records"][0])),
        lambda value: value["records"][0].update(aliases="alias"),
        lambda value: value.update(extra=True),
    ],
)
def test_catalog_loader_rejects_invalid_schema(tmp_path: Path, mutation) -> None:
    payload = _payload()
    mutation(payload)
    path = tmp_path / "catalog.json"
    _write(path, payload)

    with pytest.raises(DockIdentityCatalogError):
        load_dock_identity_catalog(path)


def test_catalog_loader_rejects_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(DockIdentityCatalogError, match="не найден"):
        load_dock_identity_catalog(tmp_path / "missing.json")
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(DockIdentityCatalogError, match="неверный JSON"):
        load_dock_identity_catalog(malformed)


def test_compact_generator_collapses_progression_and_keeps_real_variants_separate() -> None:
    source = {
        "107061": {
            "group_type": 10706,
            "name": {"en": "Enterprise"},
            "is_retrofit": False,
            "is_type2": False,
        },
        "107062": {
            "group_type": 10706,
            "name": {"en": "Enterprise"},
            "is_retrofit": False,
            "is_type2": False,
        },
        "107964": {
            "group_type": 10706,
            "name": {"en": "Enterprise (Retrofit)"},
            "is_retrofit": True,
            "is_type2": False,
        },
        "202321": {
            "group_type": 20232,
            "name": {"en": "Enterprise"},
            "is_retrofit": False,
            "is_type2": False,
        },
        "900184": {
            "group_type": 10706,
            "name": {"en": "NPC Enterprise"},
            "is_retrofit": False,
            "is_type2": False,
        },
    }
    provenance = {
        field: getattr(_provenance(), field)
        for field in _provenance().__dataclass_fields__
    }

    generated = build_catalog(source, provenance=provenance)
    records = generated["records"]

    assert records == [
        {
            "canonical_id": "azur_lane_ship_group:10706",
            "canonical_name": "Enterprise",
            "aliases": ["Enterprise (Retrofit)"],
        },
        {
            "canonical_id": "azur_lane_ship_group:20232",
            "canonical_name": "Enterprise",
            "aliases": [],
        },
    ]
    assert b"NPC Enterprise" not in canonical_json_bytes(generated)


def test_generator_serialization_is_deterministic() -> None:
    first = canonical_json_bytes(_payload())
    second = canonical_json_bytes(json.loads(first))

    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
