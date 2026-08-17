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


def _shift(image, dx=0.0, dy=0.0):
    matrix = np.float32([[1.0, 0.0, dx], [0.0, 1.0, dy]])
    return cv2.warpAffine(
        image,
        matrix,
        (image.shape[1], image.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT_101,
    )


def _synthetic_icon(kind):
    """Создать детерминированную UI-подобную текстуру без event-specific asset."""
    height, width = ITEM_SHAPE
    y, x = np.mgrid[0:height, 0:width]
    image = np.empty((height, width, 3), dtype=np.uint8)
    image[:, :, 0] = np.clip(40 + x * 2, 0, 255)
    image[:, :, 1] = np.clip(70 + y * 2, 0, 255)
    image[:, :, 2] = 100
    if kind == 1:
        cv2.circle(image, (31, 31), 18, (220, 180, 80), -1, cv2.LINE_AA)
        cv2.rectangle(image, (20, 18), (42, 45), (80, 220, 180), 2, cv2.LINE_AA)
        cv2.line(image, (16, 47), (47, 16), (250, 250, 250), 2, cv2.LINE_AA)
    else:
        cv2.rectangle(image, (14, 14), (49, 49), (180, 80, 220), -1, cv2.LINE_AA)
        cv2.circle(image, (31, 31), 12, (60, 220, 220), 3, cv2.LINE_AA)
        cv2.line(image, (12, 31), (51, 31), (250, 250, 250), 2, cv2.LINE_AA)
    return image


def _synthetic_template_pair():
    return _synthetic_icon(1), _synthetic_icon(2)


def _set_templates(grid, templates):
    grid.templates = {name: image.copy() for name, image in templates.items()}
    grid.colors = {name: cv2.mean(image)[:3] for name, image in grid.templates.items()}
    grid.templates_hit = {name: 0 for name in grid.templates}


def test_unique_source_identity_does_not_depend_on_noisy_amount_ocr():
    spec = make_spec(make_row(10, "token", amount=1))
    item = runtime("token", amount=71)

    claim = resolve_catalog_claim(spec, item)
    rows, findings = reconcile_event_shop(spec, [item])

    assert claim["status"] == "matched"
    assert claim["source"]["row_id"] == 10
    assert claim["amount"] == 1
    assert claim["ocr_amount"] == 71
    assert findings == []
    assert rows[0]["status"] == "matched"
    assert rows[0]["row_id"] == 10
    assert rows[0]["amount"] == 1
    assert rows[0]["ocr_amount"] == 71
    assert rows[0]["amount_evidence"] == "event_spec"


def test_amount_remains_disambiguator_when_source_candidates_disagree():
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


def test_unique_source_identity_normalizes_noisy_price_and_keeps_ocr_evidence():
    spec = make_spec(make_row(10, "token", price=300))
    item = runtime("token", price=1300)

    claim = resolve_catalog_claim(spec, item)

    assert claim["status"] == "matched"
    assert claim["source"]["row_id"] == 10
    assert claim["price"] == 300
    assert claim["ocr_price"] == 1300


def test_price_consensus_normalizes_ambiguous_group_without_guessing_row():
    spec = make_spec(
        make_row(10, "same", price=300),
        make_row(11, "same", price=300),
    )
    item = runtime("same", price=1300)

    claim = resolve_catalog_claim(spec, item)
    grid = EventShopItemGrid(grids=None, templates={})
    grid.set_catalog_spec(spec)
    grid._apply_catalog_evidence(item)

    assert claim["status"] == "ambiguous"
    assert claim["source"] is None
    assert claim["price"] == 300
    assert item.price == 300
    assert item.ocr_price == 1300
    assert item.catalog_row_id is None


def test_price_remains_disambiguator_when_source_candidates_disagree():
    spec = make_spec(
        make_row(10, "token", price=300),
        make_row(11, "token", price=500),
    )

    matched = resolve_catalog_claim(spec, runtime("token", price=500))
    unresolved = resolve_catalog_claim(spec, runtime("token", price=1300))

    assert matched["status"] == "matched"
    assert matched["source"]["row_id"] == 11
    assert unresolved["status"] == "unmatched"
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
    image, _ = _synthetic_template_pair()
    _set_templates(grid, {"StaleT1": image, "AllowedT1": image})
    grid.templates_hit["StaleT1"] = 100
    grid.set_catalog_spec(make_spec(make_row(10, "AllowedT1")))

    assert grid.match_template(image) == "AllowedT1"


def test_event_shop_matcher_tolerates_bounded_translation_for_catalog_identity():
    grid = EventShopItemGrid(grids=None, templates={})
    expected, competitor = _synthetic_template_pair()
    _set_templates(grid, {"AllowedT1": expected, "AllowedT2": competitor})
    grid.set_catalog_spec(
        make_spec(
            make_row(10, "AllowedT1"),
            make_row(11, "AllowedT2", price=500),
        )
    )

    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, 1), (0.5, -0.5)):
        assert grid.match_template(_shift(expected, dx=dx, dy=dy)) == "AllowedT1"


def test_catalog_identity_has_priority_over_ephemeral_numeric_template():
    grid = EventShopItemGrid(grids=None, templates={})
    expected, competitor = _synthetic_template_pair()
    query = _shift(expected, dx=2)
    _set_templates(
        grid,
        {
            "AllowedT1": expected,
            "AllowedT2": competitor,
            "101": query,
        },
    )
    grid.set_catalog_spec(
        make_spec(
            make_row(10, "AllowedT1"),
            make_row(11, "AllowedT2", price=500),
        )
    )

    assert grid.match_template(query) == "AllowedT1"


def test_close_named_candidates_remain_fail_closed_instead_of_arbitrary_choice():
    grid = EventShopItemGrid(grids=None, templates={})
    image, _ = _synthetic_template_pair()
    _set_templates(grid, {"AllowedT1": image, "AllowedT2": image})
    grid.set_catalog_spec(
        make_spec(
            make_row(10, "AllowedT1"),
            make_row(11, "AllowedT2", price=500),
        )
    )

    result = grid.match_template(image)

    assert result.isdigit()
    assert result not in {"AllowedT1", "AllowedT2"}


def test_source_price_and_amount_normalize_runtime_item_but_keep_ocr_evidence():
    grid = EventShopItemGrid(grids=None, templates={})
    grid.set_catalog_spec(make_spec(make_row(10, "token", price=300, amount=1)))
    item = runtime("token", price=1300, amount=51)

    grid._apply_catalog_evidence(item)

    assert item.catalog_row_id == 10
    assert item.price == 300
    assert item.ocr_price == 1300
    assert item.amount == 1
    assert item.ocr_amount == 51


def test_event_shop_ocr_regions_exclude_currency_icon_and_bottom_item_border():
    grid = EventShopItemGrid(grids=None, templates={})

    assert grid.price_area[0] >= 0
    assert grid.price_area[2] <= ITEM_SHAPE[0] + 20
    assert grid.amount_area[3] < ITEM_SHAPE[1]
