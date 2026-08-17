from types import SimpleNamespace

import cv2
import numpy as np

from module.shop_event.catalog import (
    catalog_template_names,
    resolve_catalog_claim,
)
from module.shop_event.item import EventShopItemGrid, ITEM_SHAPE
from module.webui.event_shop_observation import reconcile_event_shop


def make_spec(*rows):
    return {
        "currencies": [{"id": 17, "runtime_token": "pt"}],
        "shop_items": [dict(row) for row in rows],
    }


def make_row(
    row_id,
    token,
    *,
    price=300,
    stock=5,
    amount=1,
    currency_id=17,
):
    return {
        "row_id": row_id,
        "event_shop_filter": token,
        "price": price,
        "stock": stock,
        "amount": amount,
        "currency_id": currency_id,
    }


def runtime(token, *, price=300, total=5, remaining=5, amount=1, cost="pt"):
    return SimpleNamespace(
        group=token,
        sub_genre=None,
        tier=None,
        price=price,
        total_count=total,
        count=remaining,
        amount=amount,
        cost=cost,
    )


def test_unique_source_identity_does_not_depend_on_noisy_amount_ocr():
    spec = make_spec(make_row(10, "token", amount=1))
    item = runtime("token", amount=71)

    claim = resolve_catalog_claim(spec, item)
    rows, findings = reconcile_event_shop(spec, [item])

    assert claim["status"] == "matched"
    assert claim["source"]["row_id"] == 10
    assert findings == []
    assert rows[0]["status"] == "matched"
    assert rows[0]["row_id"] == 10
    assert rows[0]["amount"] == 1
    assert rows[0]["ocr_amount"] == 71
    assert rows[0]["amount_evidence"] == "event_spec"


def test_amount_remains_disambiguator_when_base_source_identity_is_not_unique():
    spec = make_spec(
        make_row(10, "token", amount=1),
        make_row(11, "token", amount=10),
    )

    matched = resolve_catalog_claim(spec, runtime("token", amount=10))
    unresolved = resolve_catalog_claim(spec, runtime("token", amount=71))

    assert matched["status"] == "matched"
    assert matched["source"]["row_id"] == 11
    assert unresolved["status"] == "ambiguous"
    assert unresolved["source"] is None


def test_identical_catalog_rows_remain_fail_closed():
    spec = make_spec(
        make_row(10, "same"),
        make_row(11, "same"),
    )

    rows, findings = reconcile_event_shop(spec, [runtime("same")])

    assert rows[0]["status"] == "ambiguous"
    assert rows[0]["row_id"] is None
    assert {item["code"] for item in findings} == {"shop_match_ambiguous"}


def test_catalog_template_names_are_derived_from_source_rows():
    spec = make_spec(
        make_row(10, "AllowedT1"),
        make_row(11, "AllowedT2", price=500),
    )

    assert catalog_template_names(spec) == {"AllowedT1", "AllowedT2"}


def test_event_shop_matcher_ignores_named_templates_absent_from_catalog():
    grid = EventShopItemGrid(grids=None, templates={})
    rng = np.random.default_rng(0xA57)
    image = rng.integers(0, 256, size=(*ITEM_SHAPE, 3), dtype=np.uint8)
    grid.templates = {
        "StaleT1": image.copy(),
        "AllowedT1": image.copy(),
    }
    mean = cv2.mean(image)[:3]
    grid.colors = {"StaleT1": mean, "AllowedT1": mean}
    grid.templates_hit = {"StaleT1": 100, "AllowedT1": 0}
    grid.set_catalog_spec(make_spec(make_row(10, "AllowedT1")))

    assert grid.match_template(image) == "AllowedT1"


def test_source_amount_normalizes_unique_runtime_item_but_keeps_ocr_evidence():
    grid = EventShopItemGrid(grids=None, templates={})
    grid.set_catalog_spec(make_spec(make_row(10, "token", amount=1)))
    item = runtime("token", amount=51)
    item.catalog_row_id = None
    item.ocr_amount = item.amount

    grid._apply_catalog_evidence(item)

    assert item.catalog_row_id == 10
    assert item.amount == 1
    assert item.ocr_amount == 51


def test_event_shop_ocr_regions_exclude_currency_icon_and_bottom_item_border():
    grid = EventShopItemGrid(grids=None, templates={})

    assert grid.price_area[0] >= 0
    assert grid.price_area[2] <= ITEM_SHAPE[0] + 20
    assert grid.amount_area[3] < ITEM_SHAPE[1]
