import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import dev_tools.dock_identity_catalog as catalog_generator
from dev_tools.dock_identity_catalog import (
    CatalogGenerationError,
    build_catalog,
    build_from_git,
    canonical_json_bytes,
    extract_supplemental_records,
    read_supplemental_records_from_git,
)
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


def _supplemental_lua(*, name: str = "Nürnberg META", closing: str = "}") -> str:
    return (
        "pg.base.fleet_tech_ship_class[970213] = {\n"
        "\tshiptype = 2,\n"
        f'\tname = "{name}",\n'
        "\tid = 970213,\n"
        "\tships = {\n"
        "\t\t970213\n"
        "\t}\n"
        f"{closing}\n"
    )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return completed.stdout.strip()


def _supplemental_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "supplemental"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    source = repo / "EN" / "sharecfg" / "fleet_tech_ship_class.lua"
    source.parent.mkdir(parents=True)
    source.write_text(_supplemental_lua(), encoding="utf-8")
    _git(repo, "add", "EN/sharecfg/fleet_tech_ship_class.lua")
    _git(repo, "commit", "-m", "тестовый источник")
    commit = _git(repo, "rev-parse", "HEAD")
    blob = _git(repo, "rev-parse", f"{commit}:EN/sharecfg/fleet_tech_ship_class.lua")
    return repo, commit, blob


def test_supplemental_source_correct_commit_and_blob_pass(tmp_path: Path) -> None:
    repo, commit, blob = _supplemental_repo(tmp_path)

    records, actual_blob, resolved_commit = read_supplemental_records_from_git(
        repo,
        commit,
        expected_commit=commit,
        expected_blob_sha=blob,
    )

    assert actual_blob == blob
    assert resolved_commit == commit
    assert records == (
        {
            "canonical_id": "azur_lane_ship_group:970213",
            "canonical_name": "Nürnberg META",
            "aliases": [],
        },
    )


def test_supplemental_source_wrong_commit_fails(tmp_path: Path) -> None:
    repo, commit, blob = _supplemental_repo(tmp_path)

    with pytest.raises(CatalogGenerationError, match="ожидался точный коммит"):
        read_supplemental_records_from_git(
            repo,
            commit,
            expected_commit="0" * 40,
            expected_blob_sha=blob,
        )


def test_supplemental_source_wrong_blob_fails(tmp_path: Path) -> None:
    repo, commit, _blob = _supplemental_repo(tmp_path)

    with pytest.raises(CatalogGenerationError, match="ожидался"):
        read_supplemental_records_from_git(
            repo,
            commit,
            expected_commit=commit,
            expected_blob_sha="0" * 40,
        )


def test_supplemental_source_missing_path_fails(tmp_path: Path) -> None:
    repo, commit, blob = _supplemental_repo(tmp_path)

    with pytest.raises(CatalogGenerationError, match="git rev-parse"):
        read_supplemental_records_from_git(
            repo,
            commit,
            expected_commit=commit,
            expected_blob_sha=blob,
            source_path="EN/sharecfg/missing.lua",
        )


def test_supplemental_source_target_group_absent_fails() -> None:
    with pytest.raises(CatalogGenerationError, match="должен встречаться ровно один раз"):
        extract_supplemental_records(b"return {}\n")


def test_supplemental_source_name_mismatch_fails() -> None:
    source = _supplemental_lua(name="Wrong Name").encode("utf-8")

    with pytest.raises(CatalogGenerationError, match="имя EN"):
        extract_supplemental_records(source)


def test_supplemental_source_malformed_lua_fails() -> None:
    source = _supplemental_lua(closing="").encode("utf-8")

    with pytest.raises(CatalogGenerationError, match="некорректную Lua"):
        extract_supplemental_records(source)


def _main_source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "main-source"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    source = repo / "assets" / "ship" / "ship_data.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "100001": {
                    "group_type": 10000,
                    "name": {"en": "Fixture Ship"},
                    "is_retrofit": False,
                    "is_type2": False,
                }
            }
        ),
        encoding="utf-8",
    )
    generator = repo / "dev_tools" / "ship_data_extractor.py"
    generator.parent.mkdir()
    generator.write_text("# fixture\n", encoding="utf-8")
    _git(repo, "add", "assets/ship/ship_data.json", "dev_tools/ship_data_extractor.py")
    _git(repo, "commit", "-m", "тестовый источник")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_build_from_symbolic_refs_persists_resolved_commits(tmp_path: Path) -> None:
    source_repo, source_commit = _main_source_repo(tmp_path)
    supplemental_repo, supplemental_commit, supplemental_blob = _supplemental_repo(
        tmp_path
    )

    payload = build_from_git(
        source_repo,
        "HEAD",
        supplemental_repo,
        "HEAD",
        expected_source_commit=source_commit,
        expected_supplemental_commit=supplemental_commit,
        expected_supplemental_blob_sha=supplemental_blob,
    )

    assert payload["provenance"]["source_commit"] == source_commit
    assert payload["provenance"]["supplemental_source_commit"] == supplemental_commit
    DockIdentityCatalog.from_mapping(payload)


def test_cli_reports_resolved_commits_for_symbolic_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_repo, source_commit = _main_source_repo(tmp_path)
    supplemental_repo, supplemental_commit, supplemental_blob = _supplemental_repo(
        tmp_path
    )
    monkeypatch.setattr(catalog_generator, "SOURCE_COMMIT", source_commit)
    monkeypatch.setattr(
        catalog_generator,
        "SUPPLEMENTAL_SOURCE_COMMIT",
        supplemental_commit,
    )
    monkeypatch.setattr(
        catalog_generator,
        "SUPPLEMENTAL_SOURCE_BLOB_SHA",
        supplemental_blob,
    )

    exit_code = catalog_generator.main(
        [
            "--repo",
            str(source_repo),
            "--source-commit",
            "HEAD",
            "--supplemental-repo",
            str(supplemental_repo),
            "--supplemental-source-commit",
            "HEAD",
            "--output",
            str(tmp_path / "catalog.json"),
        ]
    )

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert f"source_commit={source_commit}" in stdout.split()
    assert f"supplemental_source_commit={supplemental_commit}" in stdout.split()
    assert "source_commit=HEAD" not in stdout.split()
    assert "supplemental_source_commit=HEAD" not in stdout.split()
