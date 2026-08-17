from types import SimpleNamespace

import numpy as np

from module.event_datamine.compiler import EventCompiler
from module.shop_event.catalog import resolve_catalog_claim
from module.shop_event.clerk import EventShopClerk
from module.webui.event_shop_observation import reconcile_event_shop
from module.webui.event_shop_priority import _runtime_row_identity_proven


def make_source(row_id, token, *, price, stock, amount=1, currency_id=17):
    return {
        "row_id": row_id,
        "event_shop_filter": token,
        "price": price,
        "stock": stock,
        "amount": amount,
        "currency_id": currency_id,
    }


def make_spec(*rows):
    return {
        "currencies": [{"id": 17, "runtime_token": "pt"}],
        "shop_items": list(rows),
    }


def make_runtime(
    token,
    *,
    price,
    total,
    count=None,
    amount=1,
    image=None,
):
    if count is None:
        count = total
    return SimpleNamespace(
        name=token or "101",
        group=token or None,
        sub_genre=None,
        tier=None,
        price=price,
        total_count=total,
        count=count,
        amount=amount,
        cost="pt",
        image=(
            np.zeros((12, 12, 3), dtype=np.uint8)
            if image is None
            else image
        ),
        catalog_row_id=None,
    )


def test_compiler_preserves_activity_shop_order_instead_of_row_id_order():
    source = SimpleNamespace(snapshot=SimpleNamespace(server="EN"))
    compiler = EventCompiler(source)
    activity = {"config_data": [900, 100]}
    rows = {
        900: {
            "commodity_type": 2,
            "commodity_id": 501,
            "resource_type": 17,
            "resource_num": 30,
            "num_limit": 2,
            "num": 1,
        },
        100: {
            "commodity_type": 2,
            "commodity_id": 502,
            "resource_type": 17,
            "resource_num": 40,
            "num_limit": 3,
            "num": 1,
        },
    }
    normal_items = {
        501: {"name": "Первый товар", "icon": "first"},
        502: {"name": "Второй товар", "icon": "second"},
    }

    shop_items, currencies = compiler._shop(
        activity,
        rows,
        normal_items,
        {},
        {},
        {},
    )

    assert [item.row_id for item in shop_items] == [900, 100]
    assert currencies == {17}


def test_numeric_identity_can_match_unique_source_key_without_visual_token():
    spec = make_spec(
        make_source(900, "FirstT1", price=500, stock=30),
        make_source(100, "SecondT1", price=30, stock=30),
    )
    runtime = make_runtime("", price=500, total=30)

    claim = resolve_catalog_claim(spec, runtime)

    assert claim["status"] == "matched"
    assert claim["source"]["row_id"] == 900
    assert claim["filter"] == "FirstT1"


def test_bounded_source_order_resolves_only_exact_gap_between_anchors():
    spec = make_spec(
        make_source(900, "AnchorAT1", price=10, stock=1),
        make_source(100, "SameT1", price=20, stock=2),
        make_source(700, "SameT1", price=20, stock=2),
        make_source(200, "AnchorBT1", price=30, stock=3),
    )
    runtime = [
        make_runtime("AnchorAT1", price=10, total=1),
        make_runtime("SameT1", price=20, total=2),
        make_runtime("SameT1", price=20, total=2),
        make_runtime("AnchorBT1", price=30, total=3),
    ]

    rows, findings = reconcile_event_shop(spec, runtime)

    assert [row["row_id"] for row in rows] == [900, 100, 700, 200]
    assert [row["status"] for row in rows] == ["matched"] * 4
    assert rows[1]["identity_evidence"] == "source_order"
    assert rows[2]["identity_evidence"] == "source_order"
    assert runtime[1].catalog_row_id == 100
    assert runtime[2].catalog_row_id == 700
    assert findings == []


def test_source_order_gap_stays_fail_closed_when_runtime_count_does_not_match():
    spec = make_spec(
        make_source(900, "AnchorAT1", price=10, stock=1),
        make_source(100, "SameT1", price=20, stock=2),
        make_source(700, "SameT1", price=20, stock=2),
        make_source(200, "AnchorBT1", price=30, stock=3),
    )
    runtime = [
        make_runtime("AnchorAT1", price=10, total=1),
        make_runtime("SameT1", price=20, total=2),
        make_runtime("AnchorBT1", price=30, total=3),
    ]

    rows, findings = reconcile_event_shop(spec, runtime)

    assert rows[1]["row_id"] is None
    assert rows[1]["status"] == "ambiguous"
    assert runtime[1].catalog_row_id is None
    assert {item["code"] for item in findings} == {"shop_match_ambiguous"}


def test_source_order_gap_stays_fail_closed_on_hard_field_mismatch():
    spec = make_spec(
        make_source(900, "AnchorAT1", price=10, stock=1),
        make_source(100, "SameT1", price=20, stock=2),
        make_source(700, "SameT1", price=20, stock=2),
        make_source(200, "AnchorBT1", price=30, stock=3),
    )
    runtime = [
        make_runtime("AnchorAT1", price=10, total=1),
        make_runtime("SameT1", price=20, total=2),
        make_runtime("SameT1", price=20, total=99),
        make_runtime("AnchorBT1", price=30, total=3),
    ]

    rows, _ = reconcile_event_shop(spec, runtime)

    assert rows[1]["row_id"] is None
    assert rows[2]["row_id"] is None
    assert runtime[1].catalog_row_id is None
    assert runtime[2].catalog_row_id is None


def test_reidentification_uses_proven_row_id_and_image_when_local_row_id_missing():
    image = np.full((12, 12, 3), 80, dtype=np.uint8)
    target = make_runtime("SameT1", price=20, total=2, image=image)
    target.catalog_row_id = 100
    candidate = make_runtime("", price=20, total=2, amount=51, image=image.copy())

    assert EventShopClerk._purchase_item_matches(candidate, target)


def test_reidentification_rejects_different_visual_item_with_same_hard_fields():
    target = make_runtime(
        "SameT1",
        price=20,
        total=2,
        image=np.zeros((12, 12, 3), dtype=np.uint8),
    )
    target.catalog_row_id = 100
    candidate = make_runtime(
        "",
        price=20,
        total=2,
        image=np.full((12, 12, 3), 100, dtype=np.uint8),
    )

    assert not EventShopClerk._purchase_item_matches(candidate, target)


def test_reidentification_rejects_conflicting_proven_row_id():
    image = np.full((12, 12, 3), 80, dtype=np.uint8)
    target = make_runtime("SameT1", price=20, total=2, image=image)
    target.catalog_row_id = 100
    candidate = make_runtime("SameT1", price=20, total=2, image=image.copy())
    candidate.catalog_row_id = 700

    assert not EventShopClerk._purchase_item_matches(candidate, target)


def test_priority_accepts_runtime_only_for_its_proven_source_row():
    runtime = make_runtime("SameT1", price=20, total=2)
    runtime.catalog_row_id = 100

    assert _runtime_row_identity_proven("100", runtime)
    assert not _runtime_row_identity_proven("700", runtime)


def test_priority_rejects_missing_or_invalid_source_row_identity():
    runtime = make_runtime("SameT1", price=20, total=2)

    assert not _runtime_row_identity_proven("100", runtime)
    runtime.catalog_row_id = "invalid"
    assert not _runtime_row_identity_proven("100", runtime)
