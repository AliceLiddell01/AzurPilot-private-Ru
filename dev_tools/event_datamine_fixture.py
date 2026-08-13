"""Извлечение минимального source-derived fixture текущего EN события."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from module.event_datamine.discovery import (
    discover_major_events,
    resolve_current_candidate,
)
from module.event_datamine.map_compiler import _values
from module.event_datamine.source import ShareCfgLoader, SourceSnapshot

TABLES = (
    "activity_template",
    "memory_group",
    "activity_medal_group",
    "activity_event_pt",
    "activity_shop_template",
    "chapter_template",
    "chapter_template_loop",
    "map_event_list",
    "map_event_template",
    "expedition_data_template",
    "task_data_template",
    "item_data_statistics",
    "item_virtual_data_statistics",
    "player_resource",
    "equip_data_statistics",
    "ship_data_statistics",
)


def _mapping(value: Any) -> Mapping[Any, Any]:
    return value if isinstance(value, Mapping) else {}


def _ints(value: Any) -> set[int]:
    result: set[int] = set()
    if isinstance(value, Mapping):
        for item in value.values():
            result.update(_ints(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.update(_ints(item))
    elif isinstance(value, int):
        result.add(value)
    return result


def _reward_identity(value: Any) -> tuple[int, int] | None:
    parts = _values(value)
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _canonical(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def extract_current_fixture(
    source: ShareCfgLoader, *, now: datetime
) -> tuple[dict[str, dict[int, Any]], dict[str, Any]]:
    candidate = resolve_current_candidate(
        discover_major_events(source), server=source.snapshot.server, now=now
    )
    if candidate is None:
        raise ValueError("В snapshot нет active/redemption major event")

    loaded: dict[str, dict[int, Any]] = {
        table: source.load_table(table) for table in TABLES
    }
    activities = loaded["activity_template"]
    related = {
        row_id: activities[row_id]
        for row_id in candidate.related_activity_ids
        if row_id in activities
    }
    selected: dict[str, dict[int, Any]] = {
        table: {} for table in TABLES
    }
    selected["activity_template"] = related
    selected["memory_group"] = {
        row_id: row
        for row_id, row in loaded["memory_group"].items()
        if int(_mapping(row).get("link_event", 0) or 0) == candidate.activity_id
    }
    selected["activity_medal_group"] = {
        row_id: row
        for row_id, row in loaded["activity_medal_group"].items()
        if any(
            len(_values(link)) > 1
            and int(_values(link)[1] or 0) == candidate.activity_id
            for link in _values(_mapping(row).get("activity_link"))
        )
    }

    map_ids = set(candidate.map_ids)
    for table in ("chapter_template", "chapter_template_loop", "map_event_list"):
        selected[table] = {
            row_id: row
            for row_id, row in loaded[table].items()
            if row_id in map_ids
        }

    event_ids: set[int] = set()
    for row in selected["map_event_list"].values():
        event_ids.update(_ints(_mapping(row).get("event_list")))
        event_ids.update(_ints(_mapping(row).get("event_list_loop")))
    selected["map_event_template"] = {
        row_id: row
        for row_id, row in loaded["map_event_template"].items()
        if row_id in event_ids
    }

    expedition_ids: set[int] = set()
    for table in ("chapter_template", "chapter_template_loop"):
        for row in selected[table].values():
            expedition_ids.update(_ints(_mapping(row).get("ai_expedition_list")))
    selected["expedition_data_template"] = {
        row_id: row
        for row_id, row in loaded["expedition_data_template"].items()
        if row_id in expedition_ids
    }

    milestone = next(
        (
            row
            for row in related.values()
            if int(_mapping(row).get("type", 0) or 0) == 74
        ),
        None,
    )
    milestone_id = int(
        _mapping(milestone).get(
            "config_id", _mapping(milestone).get("id", 0)
        )
        or 0
    )
    if milestone_id in loaded["activity_event_pt"]:
        selected["activity_event_pt"][milestone_id] = loaded[
            "activity_event_pt"
        ][milestone_id]

    shop_id = int(
        _mapping(_mapping(milestone).get("config_client")).get(
            "shopLinkActID", 0
        )
        or 0
    )
    shop_activity = activities.get(shop_id)
    if not isinstance(shop_activity, Mapping):
        shop_activity = next(
            (
                row
                for row in related.values()
                if int(_mapping(row).get("type", 0) or 0) == 14
            ),
            {},
        )
    shop_rows = {
        int(value) for value in _values(_mapping(shop_activity).get("config_data"))
    }
    selected["activity_shop_template"] = {
        row_id: row
        for row_id, row in loaded["activity_shop_template"].items()
        if row_id in shop_rows
    }

    task_ids: set[int] = set()
    all_task_ids = set(loaded["task_data_template"])
    for row in related.values():
        if int(_mapping(row).get("type", 0) or 0) == 13:
            task_ids.update(
                int(value) for value in _values(_mapping(row).get("config_data"))
            )
        task_ids.update(
            _ints(_mapping(_mapping(row).get("config_client")).get("taskConfig"))
            & all_task_ids
        )
    selected["task_data_template"] = {
        row_id: row
        for row_id, row in loaded["task_data_template"].items()
        if row_id in task_ids
    }

    identities: set[tuple[int, int]] = set()
    currency_ids: set[int] = set()
    for row in selected["activity_shop_template"].values():
        currency_ids.add(int(_mapping(row).get("resource_type", 0) or 0))
        identities.add(
            (
                int(_mapping(row).get("commodity_type", 0) or 0),
                int(_mapping(row).get("commodity_id", 0) or 0),
            )
        )
    for row in selected["activity_event_pt"].values():
        currency_ids.add(int(_mapping(row).get("pt", 0) or 0))
        for reward in _values(_mapping(row).get("drop_client")):
            identity = _reward_identity(reward)
            if identity:
                identities.add(identity)
    selected["player_resource"] = {
        row_id: row
        for row_id, row in loaded["player_resource"].items()
        if row_id in currency_ids
    }
    identities.update(
        (2, int(_mapping(row).get("itemid", 0) or 0))
        for row in selected["player_resource"].values()
        if int(_mapping(row).get("itemid", 0) or 0)
    )

    table_by_type = {
        2: ("item_data_statistics", "item_virtual_data_statistics"),
        3: ("equip_data_statistics",),
        4: ("ship_data_statistics",),
    }
    for item_type, item_id in identities:
        for table in table_by_type.get(item_type, ()):
            if item_id in loaded[table]:
                selected[table][item_id] = loaded[table][item_id]

    manifest = {
        "fixture_schema_version": 1,
        "kind": "derived_sharecfg_subset",
        "event_id": candidate.id,
        "source": {
            "provider": source.snapshot.provider,
            "repository": source.snapshot.repository,
            "revision": source.snapshot.revision,
            "server": source.snapshot.server,
        },
        "records": {table: len(selected[table]) for table in TABLES},
    }
    return selected, manifest


def write_fixture(
    output: Path, tables: Mapping[str, Mapping[int, Any]], manifest: dict[str, Any]
) -> None:
    table_root = output / str(manifest["source"]["server"]) / "sharecfgjson"
    table_root.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for table in TABLES:
        payload = _bytes(tables.get(table, {}))
        path = table_root / f"{table}.json"
        path.write_bytes(payload)
        hashes[path.relative_to(output).as_posix()] = hashlib.sha256(payload).hexdigest()
    manifest = dict(manifest)
    manifest["sha256"] = hashes
    (output / "manifest.json").write_bytes(_bytes(manifest))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--server", default="EN")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = SourceSnapshot(
        root=args.source_root,
        server=args.server,
        repository=args.repository,
        revision=args.revision,
    )
    tables, manifest = extract_current_fixture(
        ShareCfgLoader(snapshot), now=datetime.fromisoformat(args.now)
    )
    write_fixture(args.output, tables, manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
