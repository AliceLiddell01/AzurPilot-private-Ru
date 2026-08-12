"""Provider-neutral manifest adapter for Event planning data.

A concrete source (for example AzurLaneLuaScripts) only has to produce this small
manifest shape. Network fetching and source-specific parsing intentionally stay
outside the WebUI and can be added without another page rewrite.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, Hashable, Mapping, Sequence

from module.webui.event_plan import empty_event_plan, normalize_event_plan


EVENT_MANIFEST_SCHEMA_VERSION = 1


def event_plan_from_manifest(
    manifest: Mapping[str, Any],
    *,
    source_kind: str,
    verified: bool,
    revision: str = "",
    updated_at: str = "",
) -> Dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise ValueError("manifest must be a mapping")

    event = manifest.get("event")
    if not isinstance(event, Mapping):
        raise ValueError("manifest.event is required")

    name = str(event.get("name") or "").strip()
    server = str(event.get("server") or "EN").upper()
    if not name:
        raise ValueError("manifest.event.name is required")

    plan = empty_event_plan(server)
    plan["event"].update(
        {
            "id": str(event.get("id") or "").strip(),
            "name": name,
            "server": server,
            "farm_end": str(event.get("farm_end") or "").strip(),
            "shop_end": str(event.get("shop_end") or "").strip(),
            "source": {
                "kind": str(source_kind or "manifest"),
                "verified": bool(verified),
                "updated_at": str(
                    updated_at
                    or datetime.now().replace(microsecond=0).isoformat(sep=" ")
                ),
                "revision": str(revision or ""),
            },
        }
    )
    plan["stages"] = list(manifest.get("stages") or [])
    plan["daily"] = list(manifest.get("daily") or [])
    plan["extra"] = list(manifest.get("extra") or [])
    plan["shop_items"] = list(manifest.get("shop_items") or [])
    return normalize_event_plan(plan)


def _unique_rows(
    rows: Sequence[Mapping[str, Any]],
    key: Callable[[Mapping[str, Any]], Hashable],
) -> dict[Hashable, Mapping[str, Any]]:
    """Index only identities that occur exactly once, keeping ambiguous rows fail-closed."""
    counts = Counter(key(item) for item in rows)
    return {key(item): item for item in rows if counts[key(item)] == 1}


def _compatible_event(old: Mapping[str, Any], fresh: Mapping[str, Any]) -> bool:
    """Allow first-time enrichment, but never carry local progress into another named event."""
    old_event = old.get("event") if isinstance(old, Mapping) else None
    fresh_event = fresh.get("event") if isinstance(fresh, Mapping) else None
    if not isinstance(old_event, Mapping) or not isinstance(fresh_event, Mapping):
        return False

    old_server = str(old_event.get("server") or "EN").upper()
    fresh_server = str(fresh_event.get("server") or "EN").upper()
    if old_server != fresh_server:
        return False

    old_id = str(old_event.get("id") or "").strip()
    fresh_id = str(fresh_event.get("id") or "").strip()
    if old_id and fresh_id:
        return old_id == fresh_id

    old_name = " ".join(str(old_event.get("name") or "").split()).casefold()
    fresh_name = " ".join(str(fresh_event.get("name") or "").split()).casefold()
    return not old_name or not fresh_name or old_name == fresh_name


def _preserve_recurring_state(
    old_rows: Sequence[Mapping[str, Any]],
    fresh_rows: list[dict[str, Any]],
) -> None:
    old_exact = _unique_rows(old_rows, lambda item: (item["name"], item["points"]))
    old_by_name = _unique_rows(old_rows, lambda item: item["name"])
    fresh_name_counts = Counter(item["name"] for item in fresh_rows)

    for item in fresh_rows:
        source = old_exact.get((item["name"], item["points"]))
        if source is None and fresh_name_counts[item["name"]] == 1:
            source = old_by_name.get(item["name"])
        if source is None:
            continue
        item["skip"] = bool(source.get("skip", False))
        item["completed_date"] = str(source.get("completed_date") or "")


def _preserve_shop_selection(
    old_rows: Sequence[Mapping[str, Any]],
    fresh_rows: list[dict[str, Any]],
) -> None:
    def source_id(item: Mapping[str, Any]) -> str:
        return str(item.get("id") or "").strip()

    def exact_key(item: Mapping[str, Any]) -> tuple[str, str, int]:
        return item["name"], item["filter"], item["price"]

    def relaxed_key(item: Mapping[str, Any]) -> tuple[str, str]:
        return item["name"], item["filter"]

    old_with_id = [item for item in old_rows if source_id(item)]
    old_by_id = _unique_rows(old_with_id, source_id)
    old_exact = _unique_rows(old_rows, exact_key)
    old_relaxed = _unique_rows(old_rows, relaxed_key)
    fresh_relaxed_counts = Counter(relaxed_key(item) for item in fresh_rows)

    for item in fresh_rows:
        item_id = source_id(item)
        source = old_by_id.get(item_id) if item_id else None
        if source is None:
            source = old_exact.get(exact_key(item))
        relaxed = relaxed_key(item)
        if source is None and fresh_relaxed_counts[relaxed] == 1:
            source = old_relaxed.get(relaxed)
        if source is None:
            continue
        item["selected"] = min(int(source["selected"]), item["stock"])


def merge_event_manifest(
    existing_plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    source_kind: str,
    verified: bool,
    revision: str = "",
    updated_at: str = "",
) -> Dict[str, Any]:
    """Refresh one event while preserving unambiguous user-owned local state."""
    old = normalize_event_plan(existing_plan)
    fresh = event_plan_from_manifest(
        manifest,
        source_kind=source_kind,
        verified=verified,
        revision=revision,
        updated_at=updated_at,
    )

    if not _compatible_event(old, fresh):
        return fresh

    fresh["progress"] = dict(old["progress"])
    _preserve_recurring_state(old["daily"], fresh["daily"])
    _preserve_recurring_state(old["extra"], fresh["extra"])
    _preserve_shop_selection(old["shop_items"], fresh["shop_items"])
    return fresh
