#!/usr/bin/env python3
"""Generate the compact deterministic Dock identity catalog from upstream data."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

SOURCE_REPOSITORY = "wess09/AzurPilot"
SOURCE_COMMIT = "42ffc9566870ce3074c12d4faabf19bfaaafaf71"
SOURCE_PATH = "assets/ship/ship_data.json"
SOURCE_GENERATOR_PATH = "dev_tools/ship_data_extractor.py"
SUPPLEMENTAL_SOURCE_REPOSITORY = "AzurLaneTools/AzurLaneLuaScripts"
SUPPLEMENTAL_SOURCE_COMMIT = "89048396054a2ad908dc12f14ef6f29a2bd552c9"
SUPPLEMENTAL_SOURCE_PATH = "EN/sharecfg/fleet_tech_ship_class.lua"
SUPPLEMENTAL_SOURCE_BLOB_SHA = "fcdd46ac985dcf5478a9685bdc5b248076b68ae0"
SUPPLEMENTAL_RECORDS = (
    {
        "canonical_id": "azur_lane_ship_group:970213",
        "canonical_name": "Nürnberg META",
        "aliases": [],
    },
)
SELECTION_CONTRACT = (
    "group_type with a canonical progression template (ship_id//10 == group_type), "
    "plus that group's retrofit/type-II records; EN names only; NPC/special records excluded; "
    "new EN fleet-tech groups absent from the derived source are appended from the exact "
    "supplemental Lua blob"
)
CATALOG_PATH = (
    Path(__file__).parents[1] / "assets" / "ship" / "dock_identity_catalog.json"
)


class CatalogGenerationError(RuntimeError):
    pass


def _clean_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def _eligible_record(ship_id: int, record: dict[str, Any]) -> bool:
    group_type = record.get("group_type")
    if isinstance(group_type, bool) or not isinstance(group_type, int) or group_type <= 0:
        return False
    return (
        ship_id // 10 == group_type
        or record.get("is_retrofit") is True
        or record.get("is_type2") is True
    )


def build_catalog(
    source: object,
    *,
    provenance: dict[str, str],
    supplemental_records: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    if not isinstance(source, dict):
        raise CatalogGenerationError("Upstream ship_data top level должен быть object.")
    groups: dict[int, list[tuple[int, str, bool]]] = defaultdict(list)
    for raw_id, raw_record in source.items():
        if not isinstance(raw_id, str) or not raw_id.isdigit():
            raise CatalogGenerationError(f"Недопустимый upstream ship id: {raw_id!r}.")
        if not isinstance(raw_record, dict):
            raise CatalogGenerationError(f"Ship record {raw_id} должен быть object.")
        ship_id = int(raw_id)
        if not _eligible_record(ship_id, raw_record):
            continue
        group_type = raw_record["group_type"]
        names = raw_record.get("name")
        if not isinstance(names, dict):
            raise CatalogGenerationError(f"Ship record {raw_id} не содержит name object.")
        english = names.get("en")
        if english is None:
            continue
        if not isinstance(english, str):
            raise CatalogGenerationError(f"Ship record {raw_id} содержит неверное EN name.")
        english = _clean_name(english)
        if not english:
            continue
        is_canonical_progression = ship_id // 10 == group_type and not raw_record.get(
            "is_retrofit", False
        )
        groups[group_type].append((ship_id, english, is_canonical_progression))

    records = []
    for group_type, entries in sorted(groups.items()):
        canonical_pool = [name for _ship_id, name, canonical in entries if canonical]
        if not canonical_pool:
            canonical_pool = [name for _ship_id, name, _canonical in entries]
        counts: dict[str, int] = {}
        for name in canonical_pool:
            counts[name] = counts.get(name, 0) + 1
        canonical_name = min(counts, key=lambda name: (-counts[name], name))
        aliases = sorted({name for _ship_id, name, _canonical in entries} - {canonical_name})
        records.append(
            {
                "canonical_id": f"azur_lane_ship_group:{group_type}",
                "canonical_name": canonical_name,
                "aliases": aliases,
            }
        )

    known_ids = {record["canonical_id"] for record in records}
    for supplemental in supplemental_records:
        if set(supplemental) != {"canonical_id", "canonical_name", "aliases"}:
            raise CatalogGenerationError("Supplemental record schema is invalid.")
        canonical_id = supplemental["canonical_id"]
        if canonical_id in known_ids:
            raise CatalogGenerationError(
                f"Supplemental canonical identity already exists: {canonical_id}."
            )
        records.append(
            {
                "canonical_id": canonical_id,
                "canonical_name": supplemental["canonical_name"],
                "aliases": list(supplemental["aliases"]),
            }
        )
        known_ids.add(canonical_id)

    records.sort(key=lambda record: int(str(record["canonical_id"]).rsplit(":", 1)[1]))

    if not records:
        raise CatalogGenerationError("Generated catalog unexpectedly empty.")
    return {
        "schema_version": 1,
        "language": "en",
        "identity_scheme": "azur_lane_ship_group",
        "provenance": provenance,
        "records": records,
    }


def canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")


def _git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise CatalogGenerationError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout if binary else completed.stdout.decode("utf-8").strip()


def build_from_git(repo: Path, commit: str) -> dict[str, object]:
    resolved = str(_git(repo, "rev-parse", f"{commit}^{{commit}}"))
    if resolved != commit:
        raise CatalogGenerationError(
            f"Source ref resolved to {resolved}, expected exact commit {commit}."
        )
    source_bytes = _git(repo, "show", f"{commit}:{SOURCE_PATH}", binary=True)
    generator_blob = str(_git(repo, "rev-parse", f"{commit}:{SOURCE_GENERATOR_PATH}"))
    source_blob = str(_git(repo, "rev-parse", f"{commit}:{SOURCE_PATH}"))
    try:
        source = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogGenerationError("Upstream ship_data is not valid UTF-8 JSON.") from exc
    provenance = {
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": commit,
        "source_path": SOURCE_PATH,
        "source_blob_sha": source_blob,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_generator_path": SOURCE_GENERATOR_PATH,
        "source_generator_blob_sha": generator_blob,
        "supplemental_source_repository": SUPPLEMENTAL_SOURCE_REPOSITORY,
        "supplemental_source_commit": SUPPLEMENTAL_SOURCE_COMMIT,
        "supplemental_source_path": SUPPLEMENTAL_SOURCE_PATH,
        "supplemental_source_blob_sha": SUPPLEMENTAL_SOURCE_BLOB_SHA,
        "selection_contract": SELECTION_CONTRACT,
    }
    return build_catalog(
        source,
        provenance=provenance,
        supplemental_records=SUPPLEMENTAL_RECORDS,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, default=CATALOG_PATH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = build_from_git(args.repo.resolve(), args.source_commit)
        expected = canonical_json_bytes(payload)
        if args.check:
            try:
                actual = args.output.read_bytes()
            except OSError as exc:
                raise CatalogGenerationError(
                    f"Generated catalog is unavailable: {args.output}."
                ) from exc
            if actual != expected:
                raise CatalogGenerationError(
                    "Tracked dock_identity_catalog.json is stale; regenerate it."
                )
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(expected)
    except CatalogGenerationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    records = payload["records"]
    aliases = sum(len(record["aliases"]) for record in records)
    print(
        f"PASS: records={len(records)} aliases={aliases} "
        f"source_commit={args.source_commit}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
