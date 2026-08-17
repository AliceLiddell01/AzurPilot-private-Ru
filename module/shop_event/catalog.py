"""Source-aware сопоставление строк EventShop с EventSpec.

Модуль хранит общий data-driven контракт между runtime-сканером и WebUI:
визуальный/OCR claim сначала ограничивается устойчивой identity каталога, а
неустойчивые числовые поля используются только там, где source не даёт
однозначного значения или не позволяет сохранить fail-closed неоднозначность.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def runtime_filter(item: Any) -> str:
    """Собрать runtime-токен товара из признаков фильтра магазина."""
    return "".join(
        str(getattr(item, field, "") or "")
        for field in ("group", "sub_genre", "tier")
    )


def int_attr(item: Any, name: str) -> int | None:
    """Безопасно прочитать целочисленное runtime-поле."""
    value = getattr(item, name, None)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def catalog_template_names(spec: Mapping[str, Any] | None) -> set[str]:
    """Вернуть именованные template identity, допустимые текущим EventSpec."""
    if not isinstance(spec, Mapping):
        return set()
    return {
        str(item.get("event_shop_filter") or "")
        for item in spec.get("shop_items", [])
        if isinstance(item, Mapping) and str(item.get("event_shop_filter") or "")
    }


def _currency_by_token(spec: Mapping[str, Any]) -> dict[str, int]:
    return {
        str(item.get("runtime_token") or "").lower(): int(item.get("id", 0) or 0)
        for item in spec.get("currencies", [])
        if isinstance(item, Mapping) and item.get("runtime_token")
    }


def _consensus_int(candidates: list[Mapping[str, Any]], field: str) -> int | None:
    """Вернуть source-значение, только если все кандидаты согласны между собой."""
    values: set[int] = set()
    for item in candidates:
        default = 1 if field == "amount" else 0
        try:
            values.add(int(item.get(field, default) or default))
        except (TypeError, ValueError, OverflowError):
            return None
    if len(values) != 1:
        return None
    return next(iter(values))


def resolve_catalog_claim(
    spec: Mapping[str, Any], runtime: Any
) -> dict[str, Any]:
    """Сопоставить runtime claim с каталогом без догадок о неоднозначных строках.

    Сначала используются visual filter, общий запас и валюта. Если все source-
    кандидаты согласны по цене или amount, такое значение безопасно нормализуется
    из EventSpec и OCR сохраняется только как диагностическое evidence. Поле,
    которое различает кандидатов, остаётся обязательным disambiguator. Если после
    всех доказательств остаётся несколько строк, результат остаётся неоднозначным.
    """
    token = runtime_filter(runtime)
    price = int_attr(runtime, "price")
    total = int_attr(runtime, "total_count")
    remaining = int_attr(runtime, "count")
    amount = int_attr(runtime, "amount")
    ocr_price = int_attr(runtime, "ocr_price")
    ocr_amount = int_attr(runtime, "ocr_amount")
    if ocr_price is None:
        ocr_price = price
    if ocr_amount is None:
        ocr_amount = amount
    currency_token = str(getattr(runtime, "cost", "") or "")
    currency_id = _currency_by_token(spec).get(currency_token.lower())

    claim: dict[str, Any] = {
        "status": "unmatched",
        "filter": token,
        "price": price,
        "ocr_price": ocr_price,
        "total": total,
        "remaining": remaining,
        "amount": amount,
        "ocr_amount": ocr_amount,
        "currency_token": currency_token,
        "currency_id": currency_id,
        "source": None,
        "candidates": [],
    }
    if not token or total is None or currency_id is None:
        claim["status"] = "incomplete"
        return claim

    candidates = [
        item
        for item in spec.get("shop_items", [])
        if isinstance(item, Mapping)
        and str(item.get("event_shop_filter") or "").lower() == token.lower()
        and int(item.get("stock", 0) or 0) == total
        and int(item.get("currency_id", 0) or 0) == currency_id
    ]
    if not candidates:
        return claim

    source_price = _consensus_int(candidates, "price")
    if source_price is not None:
        claim["price"] = source_price
    else:
        if price is None:
            claim["status"] = "incomplete"
            claim["candidates"] = candidates
            return claim
        candidates = [
            item
            for item in candidates
            if int(item.get("price", 0) or 0) == price
        ]
        if not candidates:
            return claim

    source_amount = _consensus_int(candidates, "amount")
    if source_amount is not None:
        claim["amount"] = source_amount
    elif len(candidates) > 1:
        if amount is None:
            claim["status"] = "incomplete"
            claim["candidates"] = candidates
            return claim
        amount_candidates = [
            item
            for item in candidates
            if int(item.get("amount", 1) or 1) == amount
        ]
        if amount_candidates:
            candidates = amount_candidates
        else:
            claim["candidates"] = candidates
            claim["status"] = "ambiguous"
            return claim

    claim["candidates"] = candidates
    if len(candidates) == 1:
        claim["status"] = "matched"
        claim["source"] = candidates[0]
    elif len(candidates) > 1:
        claim["status"] = "ambiguous"
    return claim
