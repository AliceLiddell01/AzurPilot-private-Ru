"""Точное сопоставление наблюдений EventShop с исходными данными EventSpec."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from module.shop_event.catalog import (
    bind_catalog_source,
    int_attr,
    resolve_catalog_claim,
    source_row_compatible,
)
from module.webui.event_observation_update import update_event_observation


def _same_runtime_claim(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Проверить, что два наблюдения сообщают один и тот же факт.

    Повторное окно просмотра может захватить ту же физическую строку магазина ещё
    раз. После уникального сопоставления с источником это не является
    неоднозначностью, если оба наблюдения полностью согласны по данным, влияющим
    на покупку и проверку остатка. Расхождение хотя бы одного поля сохраняет
    блокировку.
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


def _catalog_rows(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in spec.get("shop_items", [])
        if isinstance(item, Mapping)
    ]


def _source_positions(
    catalog: list[Mapping[str, Any]],
) -> dict[int, int] | None:
    positions: dict[int, int] = {}
    for index, source in enumerate(catalog):
        try:
            row_id = int(source.get("row_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if row_id <= 0 or row_id in positions:
            return None
        positions[row_id] = index
    return positions


def _remove_index_findings(
    findings: list[dict[str, str]], index: int
) -> None:
    path = f"shop_items.{index}"
    findings[:] = [item for item in findings if item.get("path") != path]


def _apply_source_to_row(
    *,
    row: dict[str, Any],
    runtime: Any,
    source: Mapping[str, Any],
    evidence: str,
) -> bool:
    try:
        row_id = int(source.get("row_id", 0) or 0)
        source_price = int(source.get("price", 0) or 0)
        source_amount = int(source.get("amount", 1) or 1)
    except (TypeError, ValueError, OverflowError):
        return False
    if row_id <= 0 or source_price <= 0 or source_amount <= 0:
        return False

    ocr_price = int_attr(runtime, "ocr_price")
    if ocr_price is None:
        ocr_price = int_attr(runtime, "price")
    ocr_amount = int_attr(runtime, "ocr_amount")
    if ocr_amount is None:
        ocr_amount = int_attr(runtime, "amount")

    bind_catalog_source(runtime, source, evidence=evidence)
    row["row_id"] = row_id
    row["status"] = "matched"
    row["filter"] = str(source.get("event_shop_filter") or row.get("filter") or "")
    row["price"] = source_price
    row["amount"] = source_amount
    row["identity_evidence"] = evidence
    if ocr_price is not None and ocr_price != source_price:
        row["ocr_price"] = ocr_price
        row["price_evidence"] = "event_spec"
    if ocr_amount is not None and ocr_amount != source_amount:
        row["ocr_amount"] = ocr_amount
        row["amount_evidence"] = "event_spec"
    return True


def _resolve_bounded_source_order(
    spec: Mapping[str, Any],
    runtime_items: list[Any],
    rows: list[dict[str, Any]],
    findings: list[dict[str, str]],
) -> None:
    """Разрешить только строго ограниченные разрывы по порядку исходного магазина.

    Порядок ``shop_items`` является порядком ``activity.config_data``. Разрыв
    разрешается лишь между двумя уже доказанными строками, когда число
    наблюдений точно совпадает с числом исходных строк между якорями и каждая
    позиционная пара согласна по жёстким полям. Любая неполнота оставляет весь
    разрыв без изменений.
    """
    catalog = _catalog_rows(spec)
    positions = _source_positions(catalog)
    if positions is None or not catalog:
        return

    claimed = {
        int(row["row_id"])
        for row in rows
        if row.get("status") == "matched" and row.get("row_id") is not None
    }
    anchors = [
        index
        for index, row in enumerate(rows)
        if row.get("status") == "matched" and row.get("row_id") is not None
    ]

    for left_index, right_index in zip(anchors, anchors[1:]):
        if right_index - left_index <= 1:
            continue
        gap_indices = list(range(left_index + 1, right_index))
        if any(rows[index].get("status") == "invalid_counter" for index in gap_indices):
            continue
        if any(rows[index].get("status") == "matched" for index in gap_indices):
            continue

        try:
            left_row_id = int(rows[left_index]["row_id"])
            right_row_id = int(rows[right_index]["row_id"])
            left_source_index = positions[left_row_id]
            right_source_index = positions[right_row_id]
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if left_source_index >= right_source_index:
            continue

        expected = catalog[left_source_index + 1 : right_source_index]
        if len(expected) != len(gap_indices):
            continue

        expected_ids: list[int] = []
        valid_expected = True
        for source in expected:
            try:
                source_row_id = int(source.get("row_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                valid_expected = False
                break
            if source_row_id <= 0 or source_row_id in claimed:
                valid_expected = False
                break
            expected_ids.append(source_row_id)
        if not valid_expected:
            continue

        if not all(
            source_row_compatible(spec, runtime_items[index], source)
            for index, source in zip(gap_indices, expected)
        ):
            continue

        applied: list[tuple[int, int]] = []
        for index, source, source_row_id in zip(gap_indices, expected, expected_ids):
            if not _apply_source_to_row(
                row=rows[index],
                runtime=runtime_items[index],
                source=source,
                evidence="source_order",
            ):
                applied = []
                break
            applied.append((index, source_row_id))

        if not applied:
            continue
        for index, source_row_id in applied:
            claimed.add(source_row_id)
            _remove_index_findings(findings, index)


def reconcile_event_shop(
    spec: Mapping[str, Any], runtime_items: Iterable[Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Сопоставить строки каталога; любую недоказанную неоднозначность сохранить."""

    runtime_items = list(runtime_items)
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
            "identity_evidence": str(claim.get("identity_evidence") or ""),
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
                    "message": "Для сопоставления не хватает наблюдений или исходных данных",
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
            if not _apply_source_to_row(
                row=row,
                runtime=runtime,
                source=source,
                evidence=str(claim.get("identity_evidence") or "source_key"),
            ):
                rows.append(row)
                continue
        elif claim_status == "ambiguous":
            row["status"] = "ambiguous"
            findings.append(
                {
                    "code": "shop_match_ambiguous",
                    "message": "Несколько строк каталога соответствуют наблюдению сканера",
                    "path": f"shop_items.{index}",
                }
            )
        else:
            findings.append(
                {
                    "code": "shop_match_unmatched",
                    "message": "Строка сканера не совпала с EventSpec по доказанным полям",
                    "path": f"shop_items.{index}",
                }
            )
        rows.append(row)

    _resolve_bounded_source_order(spec, runtime_items, rows, findings)

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
                "message": "Повторные наблюдения одной строки каталога расходятся",
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
