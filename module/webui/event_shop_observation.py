"""Точное source-aware сопоставление scanner rows EventShop с EventSpec."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from module.shop_event.catalog import resolve_catalog_claim
from module.webui.event_observation_update import update_event_observation


def _same_runtime_claim(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Проверить, что два scanner-наблюдения сообщают один и тот же факт.

    Повторный viewport может захватить ту же физическую строку магазина ещё раз.
    После уникального source-match это не является неоднозначностью, если оба
    наблюдения полностью согласны по данным, влияющим на покупку и проверку
    остатка. Расхождение хотя бы одного поля остаётся fail-closed.
    """

    return all(
        left.get(field) == right.get(field)
        for field in (
            "filter",
            "price",
            "total",
            "remaining",
            "purchased",
            "currency_token",
            "amount",
        )
    )


def reconcile_event_shop(
    spec: Mapping[str, Any], runtime_items: Iterable[Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Сопоставлять source-backed catalog identity; неоднозначность сохранять явно."""

    rows: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []
    for index, runtime in enumerate(runtime_items):
        claim = resolve_catalog_claim(spec, runtime)
        total = claim.get("total")
        remaining = claim.get("remaining")
        ocr_amount = claim.get("ocr_amount")
        row: dict[str, Any] = {
            "runtime_index": index,
            "row_id": None,
            "status": "unmatched",
            "filter": claim.get("filter"),
            "price": claim.get("price"),
            "total": total,
            "remaining": remaining,
            "purchased": None,
            "currency_token": claim.get("currency_token"),
            "amount": ocr_amount,
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

        claim_status = str(claim.get("status") or "")
        if claim_status == "incomplete":
            findings.append(
                {
                    "code": "shop_match_input_incomplete",
                    "message": "Для source match не хватает scanner/source evidence",
                    "path": f"shop_items.{index}",
                }
            )
            rows.append(row)
            continue

        if claim_status == "matched":
            source = claim.get("source")
            if not isinstance(source, Mapping):
                rows.append(row)
                continue
            try:
                row_id = int(source.get("row_id", 0) or 0)
                source_amount = int(source.get("amount", 1) or 1)
            except (TypeError, ValueError, OverflowError):
                rows.append(row)
                continue
            if row_id <= 0 or source_amount <= 0:
                rows.append(row)
                continue
            row["row_id"] = row_id
            row["status"] = "matched"
            row["amount"] = source_amount
            if ocr_amount != source_amount:
                row["ocr_amount"] = ocr_amount
                row["amount_evidence"] = "event_spec"
        elif claim_status == "ambiguous":
            row["status"] = "ambiguous"
            findings.append(
                {
                    "code": "shop_match_ambiguous",
                    "message": "Несколько catalog rows соответствуют scanner evidence",
                    "path": f"shop_items.{index}",
                }
            )
        else:
            findings.append(
                {
                    "code": "shop_match_unmatched",
                    "message": "Scanner row не совпал с EventSpec по source-backed key",
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

        canonical_index = indices[0]
        canonical = rows[canonical_index]
        if all(_same_runtime_claim(canonical, rows[index]) for index in indices[1:]):
            for index in indices[1:]:
                rows[index]["duplicate_of_runtime_index"] = canonical_index
                rows[index]["duplicate_of_row_id"] = row_id
                rows[index]["row_id"] = None
                rows[index]["status"] = "duplicate"
            continue

        for index in indices:
            rows[index]["row_id"] = None
            rows[index]["status"] = "ambiguous"
        findings.append(
            {
                "code": "shop_runtime_duplicate_conflict",
                "message": "Повторные scanner-наблюдения одного catalog row расходятся",
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
    complete_runtime_items = getattr(runtime_items, "observation_items", runtime_items)
    rows, findings = reconcile_event_shop(spec, complete_runtime_items)
    kwargs = {} if root is None else {"root": root}
    provenance = spec.get("provenance")
    revision = str(
        (provenance.get("revision") if isinstance(provenance, Mapping) else "")
        or ""
    )
    event_id = str(spec.get("id") or "")
    server = str(spec.get("server") or "EN")
    timestamp = (
        (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    )

    def apply(observation: dict[str, Any]) -> bool:
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
        return True

    return update_event_observation(
        instance=instance,
        event_id=event_id,
        server=server,
        source_revision=revision,
        updater=apply,
        **kwargs,
    )


def invalidate_event_shop_observation(
    *,
    instance: str,
    event_id: str,
    server: str,
    source_revision: str = "",
    root=None,
) -> None:
    kwargs = {} if root is None else {"root": root}

    def apply(observation: dict[str, Any]) -> bool:
        observation["observed_at"] = ""
        observation["shop_observed_at"] = ""
        observation["findings"].append(
            {
                "code": "shop_observation_invalidated_after_purchase",
                "message": "Покупка изменила магазин; требуется новое полное сканирование",
                "path": "shop_items",
            }
        )
        return True

    update_event_observation(
        instance=instance,
        event_id=event_id,
        server=server,
        source_revision=source_revision,
        updater=apply,
        **kwargs,
    )
