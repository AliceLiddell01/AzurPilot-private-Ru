from module.shop_event.item import EventShopItemGrid


class _NumericRuntimeItem:
    def __init__(self):
        self.name = "1"
        self.group = None
        self.sub_genre = None
        self.tier = None
        self.price = 300
        self.total_count = 5
        self.count = 5
        self.amount = 1
        self.cost = "pt"
        self.ocr_price = None
        self.ocr_amount = None
        self.catalog_row_id = None
        self.catalog_identity_evidence = ""
        self.predict_calls = 0

    def predict_genre(self):
        self.predict_calls += 1
        self.group = self.name.lower()
        self.sub_genre = None
        self.tier = None


def test_unique_numeric_identity_binds_canonical_filter_traits():
    spec = {
        "currencies": [{"id": 17, "runtime_token": "pt"}],
        "shop_items": [
            {
                "row_id": 10,
                "event_shop_filter": "equip",
                "price": 300,
                "stock": 5,
                "amount": 1,
                "currency_id": 17,
            }
        ],
    }
    item = _NumericRuntimeItem()
    grid = EventShopItemGrid(grids=None, templates={})
    grid.set_catalog_spec(spec)

    grid._apply_catalog_evidence(item)

    assert item.catalog_row_id == 10
    assert item.catalog_identity_evidence == "source_key"
    assert item.name == "equip"
    assert item.group == "equip"
    assert item.predict_calls == 1
    assert item.ocr_price == 300
    assert item.ocr_amount == 1
