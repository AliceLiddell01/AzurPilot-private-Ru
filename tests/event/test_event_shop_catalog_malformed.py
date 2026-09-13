from types import SimpleNamespace

import pytest

from module.shop_event.catalog import resolve_catalog_claim


def _runtime():
    return SimpleNamespace(
        group="token",
        sub_genre=None,
        tier=None,
        price=300,
        total_count=5,
        count=5,
        amount=1,
        cost="pt",
    )


def _spec():
    return {
        "currencies": [{"id": 17, "runtime_token": "pt"}],
        "shop_items": [
            {
                "row_id": 10,
                "event_shop_filter": "token",
                "price": 300,
                "stock": 5,
                "amount": 1,
                "currency_id": 17,
            }
        ],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("row_id", "not-an-int"),
        ("stock", "not-an-int"),
        ("currency_id", "not-an-int"),
        ("price", "not-an-int"),
        ("amount", "not-an-int"),
    ),
)
def test_malformed_shop_row_is_rejected_without_runtime_exception(field, value):
    spec = _spec()
    spec["shop_items"][0][field] = value

    claim = resolve_catalog_claim(spec, _runtime())

    assert claim["status"] == "unmatched"
    assert claim["source"] is None
    assert claim["candidates"] == []


def test_malformed_currency_id_is_incomplete_instead_of_raising():
    spec = _spec()
    spec["currencies"][0]["id"] = "not-an-int"

    claim = resolve_catalog_claim(spec, _runtime())

    assert claim["status"] == "incomplete"
    assert claim["currency_id"] is None
    assert claim["source"] is None


def test_valid_catalog_row_still_matches_after_numeric_hardening():
    claim = resolve_catalog_claim(_spec(), _runtime())

    assert claim["status"] == "matched"
    assert claim["source"]["row_id"] == 10
    assert claim["price"] == 300
    assert claim["amount"] == 1
