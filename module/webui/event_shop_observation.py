"""Exact, deterministic reconciliation of EventShop scanner rows with EventSpec."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from module.webui.event_observation import (
    empty_event_observation,
    load_event_observation,
    save_event_observation,
)


def _runtime_filter(item: Any) -> str:
    return "".join(
        str(getattr(item, field, "") or "") for field in ("group", "sub_genre", "tier")
    )


def _int_attr(item: Any, name: str) -> int | None:
    value = getattr(item, name, None)
    try:
        return int(value)
    except TypeError, ValueError, OverflowError:
        return None


def reconcile_event_shop(
    spec: Mapping[str, Any], runtime_items: Iterable[Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Match only a unique exact catalog key; ambiguity remains explicit."""
    currency_by_token = {
        str(item.get("runtime_token") or "").lower(): int(item.get("id", 0) or 0)
        for item in spec.get("currencies", [])
        if isinstance(item, Mapping) and item.get("runtime_token")
    }
    catalog: dict[tuple[str, int, int, int, int], list[Mapping[str, Any]]] = (
        defaultdict(list)
    )
    for item in spec.get("shop_items", []):
        if not isinstance(item, Mapping):
            continue
        token = str(item.get("event_shop_filter") or "").lower()
        if not token:
            continue
        key = (
            token,
            int(item.get("price", 0) or 0),
            int(item.get("stock", 0) or 0),
            int(item.get("currency_id", 0) or 0),
            int(item.get("amount", 1) or 1),
        )
        catalog[key].append(item)

    rows: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []
    for index, runtime in enumerate(runtime_items):
        token = _runtime_filter(runtime)
        price = _int_attr(runtime, "price")
        total = _int_attr(runtime, "total_count")
        remaining = _int_attr(runtime, "count")
        amount = _int_attr(runtime, "amount")
        currency_token = str(getattr(runtime, "cost", "") or "")
        currency_id = currency_by_token.get(currency_token.lower())
        row: dict[str, Any] = {
            "runtime_index": index,
            "row_id": None,
            "status": "unmatched",
            "filter": token,
            "price": price,
            "total": total,
            "remaining": remaining,
            "purchased": None,
            "currency_token": currency_token,
            "amount": amount,
        }
        invalid_counter = (
            total is None
            or remaining is None
            or total < 0
            or remaining < 0
            or remaining > total
        )
        if invalid_counter:
            row["status"] = "invalid_counter"
            findings.append(
                {
                    "code": "shop_counter_invalid",
                    "message": "Счётчик магазина не прошёл проверку",
                    "path": f"shop_items.{index}",
                }
            )
            rows.append(row)
            continue
        row["purchased"] = total - remaining
        if not token or price is None or amount is None or currency_id is None:
            findings.append(
                {
                    "code": "shop_match_input_incomplete",
                    "message": "Для exact match не хватает scanner/source evidence",
                    "path": f"shop_items.{index}",
                }
            )
            rows.append(row)
            continue
        matches = catalog.get((token.lower(), price, total, currency_id, amount), [])
        if len(matches) == 1:
            row["row_id"] = int(matches[0].get("row_id", 0) or 0)
            row["status"] = "matched"
        elif len(matches) > 1:
            row["status"] = "ambiguous"
            findings.append(
                {
                    "code": "shop_match_ambiguous",
                    "message": "Несколько catalog rows имеют одинаковый exact key",
                    "path": f"shop_items.{index}",
                }
            )
        else:
            findings.append(
                {
                    "code": "shop_match_unmatched",
                    "message": "Scanner row не совпал с EventSpec по exact key",
                    "path": f"shop_items.{index}",
                }
            )
        rows.append(row)
    claimed: dict[int, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        if row["status"] == "matched" and row["row_id"] is not None:
            claimed[int(row["row_id"])].append(index)
    for row_id, indices in claimed.items():
        if len(indices) <= 1:
            continue
        for index in indices:
            rows[index]["row_id"] = None
            rows[index]["status"] = "ambiguous"
        findings.append(
            {
                "code": "shop_runtime_duplicate",
                "message": "Несколько scanner rows претендуют на один catalog row",
                "path": f"shop_items.row:{row_id}",
            }
        )
    return rows, findings


def persist_event_shop_observation(
    *,
    instance: str,
    spec: Mapping[str, Any],
    runtime_items: Iterable[Any],
    observed_at: datetime | None = None,
    root=None,
) -> dict[str, Any]:
    rows, findings = reconcile_event_shop(spec, runtime_items)
    kwargs = {} if root is None else {"root": root}
    provenance = spec.get("provenance")
    revision = str(
        (provenance.get("revision") if isinstance(provenance, Mapping) else "")
        or ""
    )
    observation = load_event_observation(
        instance,
        str(spec.get("id") or ""),
        str(spec.get("server") or "EN"),
        revision,
        **kwargs,
    )
    if not observation.get("event_id"):
        observation = empty_event_observation(
            str(spec.get("id") or ""),
            str(spec.get("server") or "EN"),
            instance,
            revision,
        )
    timestamp = (
        (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    )
    observation.update(
        {
            "observed_at": timestamp,
            "source": "event_shop_scanner",
            "shop_source": "event_shop_scanner",
            "shop_observed_at": timestamp,
            "shop_items": rows,
            "findings": [
                item
                for item in observation.get("findings", [])
                if item.get("path") != "shop_items"
                and not str(item.get("path") or "").startswith("shop_items.")
            ]
            + findings,
        }
    )
    save_event_observation(instance, observation, **kwargs)
    return observation


def invalidate_event_shop_observation(
    *,
    instance: str,
    event_id: str,
    server: str,
    source_revision: str = "",
    root=None,
) -> None:
    kwargs = {} if root is None else {"root": root}
    observation = load_event_observation(
        instance, event_id, server, source_revision, **kwargs
    )
    observation["observed_at"] = ""
    observation["shop_observed_at"] = ""
    observation["findings"].append(
        {
            "code": "shop_observation_invalidated_after_purchase",
            "message": "Покупка изменила магазин; требуется новое полное сканирование",
            "path": "shop_items",
        }
    )
    save_event_observation(instance, observation, **kwargs)
