"""Сопоставление строк магазина события с данными EventSpec.

Модуль хранит общий контракт между сканером времени выполнения и WebUI:
визуальные и OCR-наблюдения сначала ограничиваются доказанной идентичностью
каталога, а нестабильные числовые поля используются только там, где исходные
данные не дают однозначного значения.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def runtime_filter(item: Any) -> str:
    """Собрать токен товара времени выполнения из признаков фильтра магазина."""
    return "".join(
        str(getattr(item, field, "") or "")
        for field in ("group", "sub_genre", "tier")
    )


def int_attr(item: Any, name: str) -> int | None:
    """Безопасно прочитать целочисленное поле товара времени выполнения."""
    value = getattr(item, name, None)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def catalog_template_names(spec: Mapping[str, Any] | None) -> set[str]:
    """Вернуть именованные шаблонные идентичности, допустимые текущим EventSpec."""
    if not isinstance(spec, Mapping):
        return set()
    return {
        str(item.get("event_shop_filter") or "")
        for item in spec.get("shop_items", [])
        if isinstance(item, Mapping) and str(item.get("event_shop_filter") or "")
    }


def _catalog_rows(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in spec.get("shop_items", [])
        if isinstance(item, Mapping)
    ]


def _currency_by_token(spec: Mapping[str, Any]) -> dict[str, int]:
    return {
        str(item.get("runtime_token") or "").lower(): int(item.get("id", 0) or 0)
        for item in spec.get("currencies", [])
        if isinstance(item, Mapping) and item.get("runtime_token")
    }


def _consensus_int(candidates: list[Mapping[str, Any]], field: str) -> int | None:
    """Вернуть исходное значение, только если все кандидаты согласны между собой."""
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


def source_row_compatible(
    spec: Mapping[str, Any],
    runtime: Any,
    source: Mapping[str, Any],
) -> bool:
    """Проверить жёсткие факты наблюдения против одной исходной строки.

    ``amount`` намеренно не входит в обязательные факты: маленькая цифра на
    иконке нестабильна и после доказанной идентичности строки нормализуется из
    EventSpec.
    """
    total = int_attr(runtime, "total_count")
    remaining = int_attr(runtime, "count")
    price = int_attr(runtime, "price")
    if total is None or remaining is None or price is None:
        return False
    if total < 0 or remaining < 0 or remaining > total:
        return False

    try:
        source_stock = int(source.get("stock", 0) or 0)
        source_price = int(source.get("price", 0) or 0)
        source_currency = int(source.get("currency_id", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if total != source_stock or price != source_price:
        return False

    currency_token = str(getattr(runtime, "cost", "") or "").lower()
    currency_id = _currency_by_token(spec).get(currency_token)
    if currency_id is None or currency_id != source_currency:
        return False

    token = runtime_filter(runtime)
    source_filter = str(source.get("event_shop_filter") or "")
    if token and token.lower() != source_filter.lower():
        return False
    return True


def bind_catalog_source(
    runtime: Any,
    source: Mapping[str, Any],
    *,
    evidence: str,
) -> Any:
    """Привязать уже доказанную исходную строку к изменяемому товару сканера.

    Исходный результат OCR сохраняется отдельно, чтобы нормализация по EventSpec
    не уничтожала диагностические данные. Визуальная идентичность заменяется
    только после доказательства конкретной строки каталога.
    """
    current_price = int_attr(runtime, "price")
    current_amount = int_attr(runtime, "amount")
    if getattr(runtime, "ocr_price", None) is None and current_price is not None:
        setattr(runtime, "ocr_price", current_price)
    if getattr(runtime, "ocr_amount", None) is None and current_amount is not None:
        setattr(runtime, "ocr_amount", current_amount)

    try:
        row_id = int(source.get("row_id", 0) or 0)
        price = int(source.get("price", 0) or 0)
        amount = int(source.get("amount", 1) or 1)
    except (TypeError, ValueError, OverflowError):
        return runtime
    if row_id <= 0 or price <= 0 or amount <= 0:
        return runtime

    setattr(runtime, "catalog_row_id", row_id)
    setattr(runtime, "catalog_identity_evidence", str(evidence or "source"))
    setattr(runtime, "price", price)
    setattr(runtime, "amount", amount)

    token = str(source.get("event_shop_filter") or "")
    if token:
        setattr(runtime, "name", token)
        predict_genre = getattr(runtime, "predict_genre", None)
        if callable(predict_genre):
            predict_genre()
        else:
            setattr(runtime, "group", token)
            setattr(runtime, "sub_genre", None)
            setattr(runtime, "tier", None)
    return runtime


def inherit_catalog_identity(runtime: Any, target: Any) -> Any:
    """Перенести доказанную идентичность после уникальной визуальной перепроверки."""
    row_id = int_attr(target, "catalog_row_id")
    if row_id is None or row_id <= 0:
        return runtime

    current_price = int_attr(runtime, "price")
    current_amount = int_attr(runtime, "amount")
    if getattr(runtime, "ocr_price", None) is None and current_price is not None:
        setattr(runtime, "ocr_price", current_price)
    if getattr(runtime, "ocr_amount", None) is None and current_amount is not None:
        setattr(runtime, "ocr_amount", current_amount)

    for field in (
        "name",
        "price",
        "amount",
        "group",
        "sub_genre",
        "tier",
        "cost",
        "is_ship",
    ):
        if hasattr(target, field):
            setattr(runtime, field, getattr(target, field))
    setattr(runtime, "catalog_row_id", row_id)
    setattr(runtime, "catalog_identity_evidence", "reidentify_image")
    return runtime


def resolve_catalog_claim(
    spec: Mapping[str, Any], runtime: Any
) -> dict[str, Any]:
    """Сопоставить наблюдение сканера с каталогом без догадок.

    Визуальный фильтр используется, когда он доказан. Если распознаватель вернул
    временную числовую идентичность, кандидаты могут быть сужены жёсткими полями
    ``stock + currency + price``. Уже доказанный ``catalog_row_id`` имеет
    приоритет, но повторно проверяется по жёстким фактам. Согласованные исходные
    значения могут нормализовать шумные ``price`` и ``amount``; различающиеся
    поля остаются обязательными для разведения кандидатов.
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
        "identity_evidence": "",
    }
    if total is None or currency_id is None:
        claim["status"] = "incomplete"
        return claim

    catalog = _catalog_rows(spec)
    claimed_row_id = int_attr(runtime, "catalog_row_id")
    if claimed_row_id is not None and claimed_row_id > 0:
        source = next(
            (
                item
                for item in catalog
                if int(item.get("row_id", 0) or 0) == claimed_row_id
            ),
            None,
        )
        if source is not None and source_row_compatible(spec, runtime, source):
            claim["status"] = "matched"
            claim["source"] = source
            claim["candidates"] = [source]
            claim["filter"] = str(source.get("event_shop_filter") or token)
            claim["price"] = int(source.get("price", 0) or 0)
            claim["amount"] = int(source.get("amount", 1) or 1)
            claim["identity_evidence"] = "catalog_row_id"
            return claim
        claim["identity_evidence"] = "catalog_row_id_conflict"
        return claim

    candidates = [
        item
        for item in catalog
        if int(item.get("stock", 0) or 0) == total
        and int(item.get("currency_id", 0) or 0) == currency_id
        and (
            not token
            or str(item.get("event_shop_filter") or "").lower() == token.lower()
        )
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
        source = candidates[0]
        claim["status"] = "matched"
        claim["source"] = source
        claim["filter"] = str(source.get("event_shop_filter") or token)
        claim["price"] = int(source.get("price", claim["price"]) or claim["price"] or 0)
        claim["amount"] = int(source.get("amount", claim["amount"]) or claim["amount"] or 1)
        claim["identity_evidence"] = "source_key"
    elif len(candidates) > 1:
        claim["status"] = "ambiguous"
    return claim
