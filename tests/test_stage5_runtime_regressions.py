from __future__ import annotations

import inspect
from types import SimpleNamespace

import numpy as np

from campaign import _adapt_generated_campaign_ui
from module.event_datamine.campaign_selector import generated_campaign_ui_layout
from module.event_datamine.runtime_policy import load_generated_runtime_policy
from module.shop_event.clerk import EventShopClerk
from module.webui.app_event_acceptance import EventAcceptanceMixin


class ScannerFact:
    def __init__(
        self,
        name,
        *,
        price,
        count,
        total_count,
        image,
        cost="pt",
    ):
        self.name = name
        self.price = price
        self.count = count
        self.total_count = total_count
        self.image = image
        self.cost = cost


def _image(value):
    return np.full((63, 63, 3), value, dtype=np.uint8)


def test_event_general_keeps_long_sections_inside_main_column():
    source = inspect.getsource(EventAcceptanceMixin._render_event_general_v2)

    assert 'with use_scope("group_EventMainColumn")' in source
    assert "for name in main_scopes" in source
    assert "put_scope(name)" in source
    assert "full_width_scopes" not in source


def test_event_general_currency_values_use_icon_markup():
    presenter = EventAcceptanceMixin()
    card = {
        "name": "A1",
        "title": "Idol and Detective",
        "sources": [
            {
                "kind": "repeatable_map_clear",
                "points": 30,
            }
        ],
    }

    rendered = presenter._render_source_card(
        card,
        '<img class="event-currency-inline-icon" src="/currency.png" alt="currency">',
    )

    assert 'event-currency-inline-icon' in rendered
    assert ">30<" in rendered
    assert "30 PT" not in rendered


def test_current_generated_package_has_verified_modern_campaign_ui_policy():
    policy = load_generated_runtime_policy(("en_51101",))

    assert policy is not None
    assert policy["campaign_ui"]["layout"] == "20241219"
    assert generated_campaign_ui_layout(
        "campaign.generated_event.en_51101.a1"
    ) == "20241219"


def test_generated_campaign_ui_policy_enables_modern_part_tabs_without_replacing_class():
    class Config:
        MAP_CHAPTER_SWITCH_20241219 = False
        MAP_CHAPTER_SWITCH_20241219_SP = False
        MAP_CHAPTER_SWITCH_20241219_SPEX = False
        MAP_CHAPTER_SWITCH_20260326 = False

    class Campaign:
        MAP = SimpleNamespace(name="A1")

        def ensure_campaign_ui(self, name, mode="normal", skip_first_screenshot=True):
            return name, mode, skip_first_screenshot

    module = SimpleNamespace(Config=Config, Campaign=Campaign, MAP=Campaign.MAP)
    original = module.Campaign

    _adapt_generated_campaign_ui(module, "20241219")

    assert module.Campaign is original
    assert module.Config.MAP_CHAPTER_SWITCH_20241219 is True
    assert module.Config.MAP_CHAPTER_SWITCH_20260326 is False
    assert module.Campaign().ensure_campaign_ui("t1")[0] == "a1"


def test_scanner_partial_overlap_drops_coin_and_oil_even_if_neighbor_identity_changes():
    old_row = [
        ScannerFact("PlateTorpedoT3", price=30, count=30, total_count=30, image=_image(20)),
        ScannerFact("PlateAntiairT3", price=30, count=30, total_count=30, image=_image(40)),
        ScannerFact("PlateGeneralT3", price=30, count=30, total_count=30, image=_image(60)),
        ScannerFact("Coin", price=500, count=5, total_count=5, image=_image(80)),
        ScannerFact("Oil", price=450, count=3, total_count=5, image=_image(100)),
    ]
    new_row = [
        ScannerFact("PlateTorpedoT3", price=30, count=30, total_count=30, image=_image(20)),
        ScannerFact("PlatePlaneT3", price=30, count=30, total_count=30, image=_image(140)),
        ScannerFact("PlatePlaneT3", price=30, count=30, total_count=30, image=_image(160)),
        ScannerFact("Coin", price=500, count=5, total_count=5, image=_image(80)),
        ScannerFact("Oil", price=450, count=3, total_count=5, image=_image(100)),
    ]

    remainder = EventShopClerk._scanner_overlap_remainder(old_row, new_row)

    assert [item.name for item in remainder] == ["PlatePlaneT3", "PlatePlaneT3"]


def test_scanner_numeric_identity_requests_bounded_stabilizing_rescan():
    items = [
        SimpleNamespace(name="Chip"),
        SimpleNamespace(name="107"),
    ]

    assert EventShopClerk._has_unresolved_template_items(items) is True
    assert EventShopClerk._has_unresolved_template_items(
        [SimpleNamespace(name="Chip")]
    ) is False
