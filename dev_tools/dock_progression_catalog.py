#!/usr/bin/env python3
"""Сгенерировать компактный Dock progression catalog из точных upstream blobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from module.dock_inventory.catalog import (
    DockIdentityCatalogError,
    load_dock_identity_catalog,
)

SOURCE_REPOSITORY = "wess09/AzurPilot"
SOURCE_COMMIT = "42ffc9566870ce3074c12d4faabf19bfaaafaf71"
SOURCE_PATH = "assets/ship/ship_data.json"
SUPPLEMENTAL_SOURCE_REPOSITORY = "AzurLaneTools/AzurLaneLuaScripts"
SUPPLEMENTAL_SOURCE_COMMIT = "ef5a7ee5068e7a25b8abc0db67c2f185b87615cb"
SUPPLEMENTAL_TEMPLATE_PATH = "CN/sharecfgdata/ship_data_template.lua"
SUPPLEMENTAL_TEMPLATE_BLOB_SHA = "99d761430a727f32905c778281cdf6a80d846a9c"
BLUEPRINT_SOURCE_PATH = "EN/sharecfg/ship_data_blueprint.lua"
BLUEPRINT_SOURCE_BLOB_SHA = "fe78579405416a5e3ca4965cdda23afa73eb0ea6"
LEVEL_SOURCE_PATH = "EN/sharecfg/ship_level.lua"
LEVEL_SOURCE_BLOB_SHA = "e1bc64ad950fcb0eab3e7a6b829d70343973d861"
SUPPLEMENTAL_GROUPS = (970213,)
SELECTION_CONTRACT = (
    "exact Stage 4 canonical groups; all canonical ship templates plus same-group "
    "retrofit templates; four consecutive canonical states are ordinary limit breaks "
    "except blueprint families; Type II remains a separate canonical group; source-only "
    "or retrofit states are nonstandard"
)
CATALOG_PATH = (
    Path(__file__).parents[1] / "assets" / "ship" / "dock_progression_catalog.json"
)
IDENTITY_CATALOG_PATH = (
    Path(__file__).parents[1] / "assets" / "ship" / "dock_identity_catalog.json"
)


class ProgressionGenerationError(RuntimeError):
    pass


def _git_bytes(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ProgressionGenerationError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _git_text(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode("utf-8").strip()


def _read_pinned_blob(
    repo: Path,
    *,
    commit: str,
    expected_commit: str,
    path: str,
    expected_blob_sha: str | None = None,
) -> tuple[bytes, str, str]:
    resolved = _git_text(repo, "rev-parse", f"{commit}^{{commit}}")
    if resolved != expected_commit:
        raise ProgressionGenerationError(
            f"Источник разрешился в {resolved}, ожидался {expected_commit}."
        )
    blob_sha = _git_text(repo, "rev-parse", f"{resolved}:{path}")
    if expected_blob_sha is not None and blob_sha != expected_blob_sha:
        raise ProgressionGenerationError(
            f"Blob {path} разрешился в {blob_sha}, ожидался {expected_blob_sha}."
        )
    content = _git_bytes(repo, "show", f"{resolved}:{path}")
    return content, blob_sha, resolved


def _lua_brace_body(source: str, opening: int, *, label: str) -> str:
    depth = 0
    quoted = False
    escaped = False
    for index in range(opening, len(source)):
        char = source[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
            if depth < 0:
                break
    raise ProgressionGenerationError(
        f"Lua table {label} содержит несбалансированные braces."
    )


def _lua_table_body(source: str, table: str, record_id: int) -> str:
    pattern = re.compile(rf"pg\.base\.{re.escape(table)}\[{record_id}\]\s*=\s*\{{")
    matches = tuple(pattern.finditer(source))
    if len(matches) != 1:
        raise ProgressionGenerationError(
            f"{table}[{record_id}] должен встречаться ровно один раз."
        )
    return _lua_brace_body(source, matches[0].end() - 1, label=f"{table}[{record_id}]")


def _lua_all_values(source: str, table: str) -> tuple[int, ...]:
    pattern = re.compile(rf"pg\.{re.escape(table)}\.all\s*=\s*\{{")
    matches = tuple(pattern.finditer(source))
    if len(matches) != 1:
        raise ProgressionGenerationError(
            f"{table}.all должен встречаться ровно один раз."
        )
    body = _lua_brace_body(source, matches[0].end() - 1, label=f"{table}.all")
    if re.sub(r"[0-9,\s]", "", body):
        raise ProgressionGenerationError(
            f"{table}.all содержит неподдерживаемые tokens."
        )
    values = tuple(int(value) for value in re.findall(r"[0-9]+", body))
    if not values or len(values) != len(set(values)):
        raise ProgressionGenerationError(f"{table}.all пуст или содержит дубликаты.")
    return values


def _single_int_field(body: str, field: str, *, label: str) -> int:
    matches = re.findall(
        rf"^\s*{re.escape(field)}\s*=\s*([0-9]+)\s*,?\s*$", body, re.MULTILINE
    )
    if len(matches) != 1:
        raise ProgressionGenerationError(f"{label} требует ровно одно поле {field}.")
    return int(matches[0])


def extract_supplemental_templates(
    source_bytes: bytes,
) -> dict[int, list[dict[str, Any]]]:
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProgressionGenerationError(
            "Supplemental template Lua не является UTF-8."
        ) from exc
    result: dict[int, list[dict[str, Any]]] = {}
    for group in SUPPLEMENTAL_GROUPS:
        records = []
        for suffix in range(1, 5):
            template_id = group * 10 + suffix
            body = _lua_table_body(source, "ship_data_template", template_id)
            record = {
                "id": _single_int_field(body, "id", label=str(template_id)),
                "group_type": _single_int_field(
                    body, "group_type", label=str(template_id)
                ),
                "star": _single_int_field(body, "star", label=str(template_id)),
                "star_max": _single_int_field(body, "star_max", label=str(template_id)),
                "max_level": _single_int_field(
                    body, "max_level", label=str(template_id)
                ),
                "is_retrofit": False,
                "is_type2": False,
            }
            if record["id"] != template_id or record["group_type"] != group:
                raise ProgressionGenerationError(
                    f"Supplemental template {template_id} имеет несогласованные id/group_type."
                )
            records.append(record)
        result[group] = records
    return result


def extract_blueprint_groups(source_bytes: bytes) -> set[int]:
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProgressionGenerationError("Blueprint Lua не является UTF-8.") from exc
    groups = set(_lua_all_values(source, "ship_data_blueprint"))
    for group in groups:
        body = _lua_table_body(source, "ship_data_blueprint", group)
        if _single_int_field(body, "id", label=f"blueprint {group}") != group:
            raise ProgressionGenerationError(
                f"Blueprint group {group} имеет неверный id."
            )
    return groups


def extract_maximum_level(source_bytes: bytes) -> int:
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProgressionGenerationError("Ship level Lua не является UTF-8.") from exc
    levels = _lua_all_values(source, "ship_level")
    maximum = max(levels)
    if levels != tuple(range(1, maximum + 1)):
        raise ProgressionGenerationError(
            "ship_level.all должен быть непрерывным от 1 до maximum."
        )
    body = _lua_table_body(source, "ship_level", maximum)
    if (
        _single_int_field(body, "level", label=f"ship_level {maximum}") != maximum
        or _single_int_field(body, "level_limit", label=f"ship_level {maximum}") != 1
    ):
        raise ProgressionGenerationError(
            "Последний ship_level record не подтверждает source level limit."
        )
    return maximum


def _validated_record(ship_id: int, raw: dict[str, Any]) -> dict[str, Any]:
    result = dict(raw)
    for field in ("group_type", "star", "star_max", "max_level"):
        value = result.get(field)
        if type(value) is not int or value < 1:
            raise ProgressionGenerationError(
                f"Ship template {ship_id} содержит неверное поле {field}."
            )
    if result["star"] > result["star_max"]:
        raise ProgressionGenerationError(f"Ship template {ship_id}: star > star_max.")
    for field in ("is_retrofit", "is_type2"):
        if type(result.get(field, False)) is not bool:
            raise ProgressionGenerationError(
                f"Ship template {ship_id} содержит неверное поле {field}."
            )
        result.setdefault(field, False)
    result["id"] = ship_id
    return result


def build_catalog(
    source: object,
    *,
    canonical_ids: tuple[str, ...],
    identity_fingerprint: str,
    supplemental_templates: dict[int, list[dict[str, Any]]],
    blueprint_groups: set[int],
    maximum_observed_level: int,
    provenance: dict[str, str],
) -> dict[str, object]:
    if not isinstance(source, dict):
        raise ProgressionGenerationError(
            "Upstream ship_data top level должен быть object."
        )
    by_group: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for raw_id, raw_record in source.items():
        if (
            not isinstance(raw_id, str)
            or not raw_id.isdigit()
            or not isinstance(raw_record, dict)
        ):
            raise ProgressionGenerationError(
                f"Некорректный ship_data record: {raw_id!r}."
            )
        ship_id = int(raw_id)
        group = raw_record.get("group_type")
        if type(group) is not int or group < 1:
            continue
        if ship_id // 10 == group or raw_record.get("is_retrofit") is True:
            by_group[group].append(_validated_record(ship_id, raw_record))

    records = []
    for canonical_id in canonical_ids:
        if not re.fullmatch(r"azur_lane_ship_group:[1-9][0-9]*", canonical_id):
            raise ProgressionGenerationError(
                f"Некорректный canonical_id: {canonical_id!r}."
            )
        group = int(canonical_id.rsplit(":", 1)[1])
        rows = list(by_group.get(group, ()))
        if not rows:
            rows = [dict(row) for row in supplemental_templates.get(group, ())]
        canonical = sorted(
            (
                row
                for row in rows
                if row["id"] // 10 == group and not row["is_retrofit"]
            ),
            key=lambda row: row["id"],
        )
        retrofit = sorted(
            (row for row in rows if row["is_retrofit"]), key=lambda row: row["id"]
        )
        if not canonical:
            raise ProgressionGenerationError(
                f"Canonical group {group} не содержит progression templates."
            )
        totals = {row["star_max"] for row in canonical}
        if len(totals) != 1:
            raise ProgressionGenerationError(
                f"Canonical group {group} меняет star_max."
            )
        total = totals.pop()
        is_blueprint = group in blueprint_groups
        is_type2_values = {row["is_type2"] for row in canonical}
        if len(is_type2_values) != 1:
            raise ProgressionGenerationError(
                f"Canonical group {group} меняет is_type2."
            )
        is_type2 = is_type2_values.pop()
        stars = tuple(row["star"] for row in canonical)
        standard = (
            not is_blueprint
            and len(canonical) == 4
            and stars == tuple(range(stars[0], stars[0] + 4))
            and stars[-1] == total
        )
        if is_blueprint:
            family_type = "blueprint"
        elif is_type2:
            family_type = "type_ii"
        elif len(canonical) == 1:
            family_type = "single_state"
        elif retrofit:
            family_type = "ordinary_with_retrofit"
        else:
            family_type = "ordinary"
        states = []
        for index, row in enumerate(canonical):
            states.append(
                {
                    "semantic_id": (
                        f"limit_break:{index}"
                        if standard
                        else f"source_state:{row['id']}"
                    ),
                    "kind": "standard_limit_break" if standard else "nonstandard",
                    "filled": row["star"],
                    "total": row["star_max"],
                    "stage_index": index if standard else None,
                    "stage_count": len(canonical) if standard else None,
                    "is_max": row["star"] == row["star_max"],
                }
            )
        for row in retrofit:
            if row["group_type"] != group:
                raise ProgressionGenerationError(
                    f"Retrofit template {row['id']} относится к другой group."
                )
            states.append(
                {
                    "semantic_id": f"retrofit:{row['id']}",
                    "kind": "nonstandard",
                    "filled": row["star"],
                    "total": row["star_max"],
                    "stage_index": None,
                    "stage_count": None,
                    "is_max": row["star"] == row["star_max"],
                }
            )
        records.append(
            {
                "canonical_id": canonical_id,
                "family_type": family_type,
                "states": states,
            }
        )
    return {
        "schema_version": 1,
        "identity_scheme": "azur_lane_ship_group",
        "identity_fingerprint": identity_fingerprint,
        "maximum_observed_level": maximum_observed_level,
        "provenance": provenance,
        "records": records,
    }


def canonical_json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_from_git(
    repo: Path,
    source_commit: str,
    supplemental_repo: Path,
    supplemental_commit: str,
    identity_catalog_path: Path = IDENTITY_CATALOG_PATH,
) -> dict[str, object]:
    source_bytes, source_blob, resolved_source = _read_pinned_blob(
        repo,
        commit=source_commit,
        expected_commit=SOURCE_COMMIT,
        path=SOURCE_PATH,
    )
    template_bytes, template_blob, resolved_supplemental = _read_pinned_blob(
        supplemental_repo,
        commit=supplemental_commit,
        expected_commit=SUPPLEMENTAL_SOURCE_COMMIT,
        path=SUPPLEMENTAL_TEMPLATE_PATH,
        expected_blob_sha=SUPPLEMENTAL_TEMPLATE_BLOB_SHA,
    )
    blueprint_bytes, blueprint_blob, _ = _read_pinned_blob(
        supplemental_repo,
        commit=supplemental_commit,
        expected_commit=SUPPLEMENTAL_SOURCE_COMMIT,
        path=BLUEPRINT_SOURCE_PATH,
        expected_blob_sha=BLUEPRINT_SOURCE_BLOB_SHA,
    )
    level_bytes, level_blob, _ = _read_pinned_blob(
        supplemental_repo,
        commit=supplemental_commit,
        expected_commit=SUPPLEMENTAL_SOURCE_COMMIT,
        path=LEVEL_SOURCE_PATH,
        expected_blob_sha=LEVEL_SOURCE_BLOB_SHA,
    )
    try:
        source = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProgressionGenerationError(
            "Upstream ship_data не является UTF-8 JSON."
        ) from exc
    try:
        identity_catalog = load_dock_identity_catalog(identity_catalog_path)
    except DockIdentityCatalogError as exc:
        raise ProgressionGenerationError(
            f"Identity catalog недоступен или некорректен: {identity_catalog_path}."
        ) from exc
    provenance = {
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": resolved_source,
        "source_path": SOURCE_PATH,
        "source_blob_sha": source_blob,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "supplemental_source_repository": SUPPLEMENTAL_SOURCE_REPOSITORY,
        "supplemental_source_commit": resolved_supplemental,
        "supplemental_template_path": SUPPLEMENTAL_TEMPLATE_PATH,
        "supplemental_template_blob_sha": template_blob,
        "blueprint_source_path": BLUEPRINT_SOURCE_PATH,
        "blueprint_source_blob_sha": blueprint_blob,
        "level_source_path": LEVEL_SOURCE_PATH,
        "level_source_blob_sha": level_blob,
        "selection_contract": SELECTION_CONTRACT,
    }
    return build_catalog(
        source,
        canonical_ids=tuple(record.canonical_id for record in identity_catalog.records),
        identity_fingerprint=identity_catalog.fingerprint,
        supplemental_templates=extract_supplemental_templates(template_bytes),
        blueprint_groups=extract_blueprint_groups(blueprint_bytes),
        maximum_observed_level=extract_maximum_level(level_bytes),
        provenance=provenance,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--supplemental-repo", type=Path, required=True)
    parser.add_argument("--identity-catalog", type=Path, default=IDENTITY_CATALOG_PATH)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument(
        "--supplemental-source-commit", default=SUPPLEMENTAL_SOURCE_COMMIT
    )
    parser.add_argument("--output", type=Path, default=CATALOG_PATH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = build_from_git(
            args.repo.resolve(),
            args.source_commit,
            args.supplemental_repo.resolve(),
            args.supplemental_source_commit,
            args.identity_catalog.resolve(),
        )
        expected = canonical_json_bytes(payload)
        if args.check:
            try:
                actual = args.output.read_bytes()
            except OSError as exc:
                raise ProgressionGenerationError(
                    f"Generated progression catalog недоступен: {args.output}."
                ) from exc
            if actual != expected:
                raise ProgressionGenerationError(
                    "Tracked dock_progression_catalog.json устарел; перегенерируйте его."
                )
        else:
            try:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(expected)
            except OSError as exc:
                raise ProgressionGenerationError(
                    f"Не удалось записать progression catalog: {args.output}."
                ) from exc
    except ProgressionGenerationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "PASS: "
        f"records={len(payload['records'])} "
        f"maximum_observed_level={payload['maximum_observed_level']} "
        f"source_commit={payload['provenance']['source_commit']} "
        f"supplemental_source_commit={payload['provenance']['supplemental_source_commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())