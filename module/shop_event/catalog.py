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


def _source_int(
    source: Mapping[str, Any],
    field: str,
    *,
    default: int | None = None,
) -> int | None:
    """Безопасно прочитать целочисленный факт одной строки EventSpec."""

    value = source.get(field, default)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_catalog_row(source: Mapping[str, Any]) -> bool:
    """Пропускать в сопоставление только полностью типизированную source-row."""

    row_id = _source_int(source, "row_id")
    stock = _source_int(source, "stock")
    currency_id = _source_int(source, "currency_id")
    price = _source_int(source, "price")
    amount = _source_int(source, "amount", default=1)
    return bool(
        row_id is not None
        and row_id > 0
        and stock is not None
        and stock >= 0
        and currency_id is not None
        and currency_id > 0
        and price is not None
        and price > 0
        and amount is not None
        and amount > 0
    )


def catalog_rows(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Вернуть только source-строки, безопасные для runtime-сопоставления."""

    rows = spec.get("shop_items", [])
    if not isinstance(rows, list):
        return []
    return [
        item
        for item in rows
        if isinstance(item, Mapping) and _valid_catalog_row(item)
    ]


def catalog_template_names(spec: Mapping[str, Any] | None) -> set[str]:
    """Вернуть именованные шаблонные идентичности, допустимые текущим EventSpec."""
    if not isinstance(spec, Mapping):
        return set()
    return {
        str(item.get("event_shop_filter") or "")
        for item in catalog_rows(spec)
        if str(item.get("event_shop_filter") or "")
    }


def _currency_by_token(spec: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    currencies = spec.get("currencies", [])
    if not isinstance(currencies, list):
        return result
    for item in currencies:
        if not isinstance(item, Mapping):
            continue
        token = str(item.get("runtime_token") or "").strip().lower()
        currency_id = _source_int(item, "id")
        if token and currency_id is not None and currency_id > 0:
            result[token] = currency_id
    return result


def _consensus_int(candidates: list[Mapping[str, Any]], field: str) -> int | None:
    """Вернуть исходное значение, только если все кандидаты согласны между собой."""
    values: set[int] = set()
    for item in candidates:
        default = 1 if field == "amount" else 0
        value = _source_int(item, field, default=default)
        if value is None:
            return None
        values.add(value)
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

    source_stock = _source_int(source, "stock")
    source_price = _source_int(source, "price")
    source_currency = _source_int(source, "currency_id")
    if (
        source_stock is None
        or source_price is None
        or source_currency is None
    ):
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

    row_id = _source_int(source, "row_id")
    price = _source_int(source, "price")
    amount = _source_int(source, "amount", default=1)
    if (
        row_id is None
        or row_id <= 0
        or price is None
        or price <= 0
        or amount is None
        or amount <= 0
    ):
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

    catalog = catalog_rows(spec)
    claimed_row_id = int_attr(runtime, "catalog_row_id")
    if claimed_row_id is not None and claimed_row_id > 0:
        source = next(
            (
                item
                for item in catalog
                if _source_int(item, "row_id") == claimed_row_id
            ),
            None,
        )
        if source is not None and source_row_compatible(spec, runtime, source):
            source_price = _source_int(source, "price")
            source_amount = _source_int(source, "amount", default=1)
            if source_price is None or source_amount is None:
                claim["identity_evidence"] = "catalog_row_id_conflict"
                return claim
            claim["status"] = "matched"
            claim["source"] = source
            claim["candidates"] = [source]
            claim["filter"] = str(source.get("event_shop_filter") or token)
            claim["price"] = source_price
            claim["amount"] = source_amount
            claim["identity_evidence"] = "catalog_row_id"
            return claim
        claim["identity_evidence"] = "catalog_row_id_conflict"
        return claim

    candidates = [
        item
        for item in catalog
        if _source_int(item, "stock") == total
        and _source_int(item, "currency_id") == currency_id
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
            if _source_int(item, "price") == price
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
            if _source_int(item, "amount", default=1) == amount
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
        source_price = _source_int(source, "price")
        source_amount = _source_int(source, "amount", default=1)
        if source_price is None or source_amount is None:
            return claim
        claim["status"] = "matched"
        claim["source"] = source
        claim["filter"] = str(source.get("event_shop_filter") or token)
        claim["price"] = source_price
        claim["amount"] = source_amount
        claim["identity_evidence"] = "source_key"
    elif len(candidates) > 1:
        claim["status"] = "ambiguous"
    return claim
