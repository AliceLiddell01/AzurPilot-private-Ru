from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import dev_tools.dock_progression_catalog as dock_progression_catalog
from dev_tools.dock_progression_catalog import (
    ProgressionGenerationError,
    build_catalog,
    canonical_json_bytes,
    extract_blueprint_groups,
    extract_maximum_level,
    extract_supplemental_templates,
)
from module.dock_inventory.catalog import DockIdentityCatalogError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPOSITORY_ROOT / "assets" / "ship" / "dock_progression_catalog.json"


def _row(
    group: int,
    star: int,
    total: int,
    *,
    retrofit: bool = False,
    type_ii: bool = False,
) -> dict[str, object]:
    return {
        "group_type": group,
        "star": star,
        "star_max": total,
        "max_level": 100,
        "is_retrofit": retrofit,
        "is_type2": type_ii,
    }


def _source() -> dict[str, object]:
    source: dict[str, object] = {}
    for group, base, total, type_ii in (
        (100, 1, 4, False),
        (200, 2, 5, False),
        (300, 3, 6, True),
        (400, 3, 6, False),
    ):
        for index in range(4):
            source[str(group * 10 + index + 1)] = _row(
                group, base + index, total, type_ii=type_ii
            )
    source["5001"] = _row(500, 4, 4)
    source["9001"] = _row(200, 5, 5, retrofit=True)
    return source


def _provenance() -> dict[str, str]:
    return {
        "source_repository": "fixture/source",
        "source_commit": "1" * 40,
        "source_path": "ship.json",
        "source_blob_sha": "2" * 40,
        "source_sha256": "3" * 64,
        "supplemental_source_repository": "fixture/lua",
        "supplemental_source_commit": "4" * 40,
        "supplemental_template_path": "template.lua",
        "supplemental_template_blob_sha": "5" * 40,
        "blueprint_source_path": "blueprint.lua",
        "blueprint_source_blob_sha": "6" * 40,
        "level_source_path": "level.lua",
        "level_source_blob_sha": "7" * 40,
        "selection_contract": "fixture",
    }


def test_generator_classifies_standard_type2_blueprint_single_and_retrofit() -> None:
    payload = build_catalog(
        _source(),
        canonical_ids=tuple(
            f"azur_lane_ship_group:{group}" for group in (100, 200, 300, 400, 500)
        ),
        identity_fingerprint="8" * 64,
        supplemental_templates={},
        blueprint_groups={400},
        maximum_observed_level=125,
        provenance=_provenance(),
    )
    records = {record["canonical_id"]: record for record in payload["records"]}

    assert records["azur_lane_ship_group:100"]["family_type"] == "ordinary"
    assert [
        state["filled"] for state in records["azur_lane_ship_group:100"]["states"]
    ] == [1, 2, 3, 4]
    assert records["azur_lane_ship_group:300"]["family_type"] == "type_ii"
    assert all(
        state["kind"] == "standard_limit_break"
        for state in records["azur_lane_ship_group:300"]["states"]
    )
    assert records["azur_lane_ship_group:400"]["family_type"] == "blueprint"
    assert all(
        state["kind"] == "nonstandard"
        for state in records["azur_lane_ship_group:400"]["states"]
    )
    assert records["azur_lane_ship_group:500"]["family_type"] == "single_state"
    retrofit_states = records["azur_lane_ship_group:200"]["states"]
    assert (
        records["azur_lane_ship_group:200"]["family_type"] == "ordinary_with_retrofit"
    )
    assert retrofit_states[-1]["semantic_id"] == "retrofit:9001"
    assert retrofit_states[-1]["stage_index"] is None


def test_generator_uses_supplemental_group_and_serializes_deterministically() -> None:
    supplemental = {
        970213: [
            {"id": 9702130 + index, **_row(970213, index + 1, 5)}
            for index in range(1, 5)
        ]
    }
    kwargs = {
        "canonical_ids": ("azur_lane_ship_group:970213",),
        "identity_fingerprint": "8" * 64,
        "supplemental_templates": supplemental,
        "blueprint_groups": set(),
        "maximum_observed_level": 125,
        "provenance": _provenance(),
    }
    first = build_catalog({}, **kwargs)
    second = build_catalog({}, **kwargs)

    record = first["records"][0]
    assert record["canonical_id"] == "azur_lane_ship_group:970213"
    assert record["family_type"] == "ordinary"
    states = record["states"]
    assert [state["semantic_id"] for state in states] == [
        "limit_break:0",
        "limit_break:1",
        "limit_break:2",
        "limit_break:3",
    ]
    assert [state["filled"] for state in states] == [2, 3, 4, 5]
    assert all(state["total"] == 5 for state in states)
    assert all(state["kind"] == "standard_limit_break" for state in states)
    assert [state["stage_index"] for state in states] == [0, 1, 2, 3]
    assert all(state["stage_count"] == 4 for state in states)
    assert [state["is_max"] for state in states] == [False, False, False, True]
    assert first == second
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert (
        hashlib.sha256(canonical_json_bytes(first)).hexdigest()
        == hashlib.sha256(canonical_json_bytes(second)).hexdigest()
    )


def _supplemental_lua() -> bytes:
    blocks = []
    for index in range(1, 5):
        ship_id = 9702130 + index
        blocks.append(
            f"_G.pg.base.ship_data_template[{ship_id}] = {{\n"
            f" id = {ship_id},\n group_type = 970213,\n star = {index + 1},\n"
            " star_max = 5,\n max_level = 100\n}\n"
        )
    return "\n".join(blocks).encode()


def test_supplemental_template_parser_reads_all_four_states() -> None:
    result = extract_supplemental_templates(_supplemental_lua())
    assert [row["star"] for row in result[970213]] == [2, 3, 4, 5]
    assert all(row["star_max"] == 5 for row in result[970213])


def test_blueprint_and_level_sources_are_structurally_proven() -> None:
    blueprint = b"""
pg.ship_data_blueprint.all = { 29901, 39901 }
pg.base.ship_data_blueprint[29901] = {
 id = 29901
}
pg.base.ship_data_blueprint[39901] = {
 id = 39901
}
"""
    levels = b"""
pg.ship_level.all = { 1, 2, 3 }
pg.base.ship_level[1] = { level = 1, level_limit = 0 }
pg.base.ship_level[2] = { level = 2, level_limit = 0 }
pg.base.ship_level[3] = {
 level = 3,
 level_limit = 1
}
"""
    assert extract_blueprint_groups(blueprint) == {29901, 39901}
    assert extract_maximum_level(levels) == 3


def test_level_source_rejects_gaps_and_unproven_final_limit() -> None:
    with pytest.raises(ProgressionGenerationError, match="непрерывным"):
        extract_maximum_level(
            b"pg.ship_level.all = {1, 3}\npg.base.ship_level[3] = { level = 3, level_limit = 1 }"
        )
    with pytest.raises(ProgressionGenerationError, match="level limit"):
        extract_maximum_level(
            b"pg.ship_level.all = {1}\npg.base.ship_level[1] = {\nlevel = 1,\nlevel_limit = 0\n}"
        )


def test_generator_rejects_missing_canonical_group() -> None:
    with pytest.raises(ProgressionGenerationError, match="не содержит progression"):
        build_catalog(
            {},
            canonical_ids=("azur_lane_ship_group:999",),
            identity_fingerprint="8" * 64,
            supplemental_templates={},
            blueprint_groups=set(),
            maximum_observed_level=125,
            provenance=_provenance(),
        )


def test_build_from_git_uses_explicit_identity_catalog_and_types_loader_failure(
    tmp_path: Path, monkeypatch
) -> None:
    upstream_repo = tmp_path / "upstream"
    supplemental_repo = tmp_path / "supplemental"
    identity_catalog_path = tmp_path / "fork" / "dock_identity_catalog.json"
    seen_identity_paths: list[Path] = []

    def pinned_blob(
        _repo: Path,
        *,
        commit: str,
        expected_commit: str,
        path: str,
        expected_blob_sha: str | None = None,
    ) -> tuple[bytes, str, str]:
        del commit, expected_blob_sha
        content = b"{}" if path == dock_progression_catalog.SOURCE_PATH else b"fixture"
        return content, "a" * 40, expected_commit

    def fail_identity_catalog(path: Path):
        seen_identity_paths.append(path)
        raise DockIdentityCatalogError("fixture identity failure")

    monkeypatch.setattr(dock_progression_catalog, "_read_pinned_blob", pinned_blob)
    monkeypatch.setattr(
        dock_progression_catalog, "load_dock_identity_catalog", fail_identity_catalog
    )

    with pytest.raises(ProgressionGenerationError, match="Identity catalog"):
        dock_progression_catalog.build_from_git(
            upstream_repo,
            dock_progression_catalog.SOURCE_COMMIT,
            supplemental_repo,
            dock_progression_catalog.SUPPLEMENTAL_SOURCE_COMMIT,
            identity_catalog_path,
        )

    assert seen_identity_paths == [identity_catalog_path]


def test_generator_write_oserror_is_typed_cli_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    payload = {
        "records": [],
        "maximum_observed_level": 125,
        "provenance": {
            "source_commit": "1" * 40,
            "supplemental_source_commit": "2" * 40,
        },
    }
    monkeypatch.setattr(
        dock_progression_catalog,
        "build_from_git",
        lambda *_args, **_kwargs: payload,
    )

    def fail_write(_path: Path, _data: bytes) -> int:
        raise OSError("fixture write failure")

    monkeypatch.setattr(Path, "write_bytes", fail_write)

    exit_code = dock_progression_catalog.main(
        [
            "--supplemental-repo",
            str(tmp_path),
            "--output",
            str(tmp_path / "dock_progression_catalog.json"),
        ]
    )

    assert exit_code == 1
    assert "FAIL: Не удалось записать progression catalog" in capsys.readouterr().err


def test_tracked_payload_has_exact_schema_and_no_npc_records() -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version",
        "identity_scheme",
        "identity_fingerprint",
        "maximum_observed_level",
        "provenance",
        "records",
    }
    assert all(
        not record["canonical_id"].endswith(":900184") for record in payload["records"]
    )