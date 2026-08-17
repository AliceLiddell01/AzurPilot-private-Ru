"""Source-aware сопоставление строк EventShop с EventSpec.

Модуль хранит общий data-driven контракт между runtime-сканером и WebUI:
визуальный/OCR claim сначала сопоставляется по устойчивым полям каталога, а
количество предметов внутри одной покупки используется только когда без него
источник остаётся неоднозначным.
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


def resolve_catalog_claim(
    spec: Mapping[str, Any], runtime: Any
) -> dict[str, Any]:
    """Сопоставить runtime claim с каталогом без догадок о неоднозначных строках.

    Основной ключ состоит из runtime filter, цены, общего запаса и валюты. OCR
    ``amount`` не участвует в основном ключе: для уникальной source-строки это
    уже известный факт EventSpec. Если основной ключ соответствует нескольким
    строкам, ``amount`` используется как дополнительное доказательство. Если и
    после этого остаётся более одной строки, результат остаётся неоднозначным.
    """
    token = runtime_filter(runtime)
    price = int_attr(runtime, "price")
    total = int_attr(runtime, "total_count")
    remaining = int_attr(runtime, "count")
    amount = int_attr(runtime, "amount")
    currency_token = str(getattr(runtime, "cost", "") or "")
    currency_id = _currency_by_token(spec).get(currency_token.lower())

    claim: dict[str, Any] = {
        "status": "unmatched",
        "filter": token,
        "price": price,
        "total": total,
        "remaining": remaining,
        "currency_token": currency_token,
        "currency_id": currency_id,
        "ocr_amount": amount,
        "source": None,
        "candidates": [],
    }
    if not token or price is None or total is None or currency_id is None:
        claim["status"] = "incomplete"
        return claim

    candidates = [
        item
        for item in spec.get("shop_items", [])
        if isinstance(item, Mapping)
        and str(item.get("event_shop_filter") or "").lower() == token.lower()
        and int(item.get("price", 0) or 0) == price
        and int(item.get("stock", 0) or 0) == total
        and int(item.get("currency_id", 0) or 0) == currency_id
    ]

    if len(candidates) == 1:
        claim["status"] = "matched"
        claim["source"] = candidates[0]
        claim["candidates"] = candidates
        return claim

    if len(candidates) > 1 and amount is not None:
        amount_candidates = [
            item
            for item in candidates
            if int(item.get("amount", 1) or 1) == amount
        ]
        if len(amount_candidates) == 1:
            claim["status"] = "matched"
            claim["source"] = amount_candidates[0]
            claim["candidates"] = amount_candidates
            return claim
        if amount_candidates:
            candidates = amount_candidates

    claim["candidates"] = candidates
    if len(candidates) > 1:
        claim["status"] = "ambiguous"
    return claim
