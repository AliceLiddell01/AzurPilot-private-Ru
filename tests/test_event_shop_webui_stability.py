from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from module.webui.app_event_planner import EventPlannerMixin


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "module" / "webui" / "app.py"
LAYOUT = ROOT / "module" / "webui" / "app_event_layout.py"
SHOP_V2 = ROOT / "module" / "webui" / "app_event_shop_v2.py"
CRITICAL_CSS = ROOT / "assets" / "gui" / "css" / "event-shop-stability-alas.css"


class _MemoryConfig:
    def __init__(self):
        self.data = {"EventShop": {"Scheduler": {"Enable": False}}}

    def read_file(self, _name):
        return self.data


class _Planner(EventPlannerMixin):
    def __init__(self):
        self.alas_name = "alas"
        self.alas_config = _MemoryConfig()
        self._event_plan_active_task = "EventShop"
        self.rendered = []
        self.full_refreshes = []

    def _render_event_shop_plan(self, config):
        self.rendered.append(config)

    def alas_set_group(self, task):
        self.full_refreshes.append(task)


class _LivePlanner(EventPlannerMixin):
    def __init__(self):
        self.alas_name = "alas"
        self.capacity = 10
        self._event_plan_active_task = "EventShop"
        self.plan = {
            "event": {"id": "event-test"},
            "shop_items": [
                {
                    "id": "item-a",
                    "name": "Тестовый товар",
                    "filter": "",
                    "price": 2000,
                    "stock": 10,
                    "selected": 0,
                }
            ],
        }
        self.messages = []
        self.patches = []
        self.refreshes = 0
        self.synced_targets = []

    def _event_plan_mutate(self, mutation, message):
        self.messages.append(message)
        result = mutation(self.plan)
        return result is None

    def _event_shop_quantity_capacity(self, plan, item):
        return self.capacity

    def _sync_event_shop_target_state(self, snapshot):
        self.synced_targets.append(dict(snapshot))
        return True

    def _patch_event_shop_plan_values(self, identity, snapshot):
        self.patches.append((identity, dict(snapshot)))

    def _refresh_event_plan_page(self):
        self.refreshes += 1


def test_event_shop_styles_are_loaded_before_gui_content():
    source = APP.read_text(encoding="utf-8")
    event_css = 'add_css(filepath_css("event-profiles-alas"))'
    stability_css = 'add_css(filepath_css("event-shop-stability-alas"))'
    gui_start = "gui = AlasGUI()"

    assert event_css in source
    assert stability_css in source
    assert source.index(event_css) < source.index(gui_start)
    assert source.index(stability_css) < source.index(gui_start)


def test_event_shop_scope_ids_have_critical_v2_layout_without_post_render_js():
    css = CRITICAL_CSS.read_text(encoding="utf-8")

    assert "#pywebio-scope-event_shop_v2_grid" in css
    assert '[id^="pywebio-scope-event_shop_card_"]' in css
    assert "grid-template-columns: repeat(auto-fit, minmax(205px, 1fr))" in css
    assert "display: flex" in css
    assert "#pywebio-scope-event_shop_grid" not in css


def test_event_shop_v2_icon_box_has_one_canonical_square_geometry():
    css = CRITICAL_CSS.read_text(encoding="utf-8")
    marker = ".event-shop-v2-image {"
    block = css.split(marker, 1)[1].split("}", 1)[0]

    assert "width: 88px" in block
    assert "height: 88px" in block
    assert "object-fit: contain" in block


def test_event_shop_live_values_have_animation_contract():
    css = CRITICAL_CSS.read_text(encoding="utf-8")

    assert ".event-shop-live-value" in css
    assert ".event-shop-value-updated" in css
    assert "@keyframes event-shop-value-update" in css


def test_event_shop_settings_are_prepared_before_heavy_catalog_render():
    source = LAYOUT.read_text(encoding="utf-8")
    method = source.split("def _render_event_shop_layout", 1)[1].split(
        '@use_scope("content", clear=True)', 1
    )[0]

    plan_slot = 'put_scope("group_EventShopPlan")'
    scheduler_slot = 'put_scope("group_Scheduler")'
    scheduler_render = 'self._render_named_group(task, "Scheduler", group_map, config)'
    catalog_render = 'with use_scope("group_EventShopPlan", clear=True):'

    assert method.index(plan_slot) < method.index(scheduler_slot)
    assert method.index(scheduler_slot) < method.index(scheduler_render)
    assert method.index(scheduler_render) < method.index(catalog_render)


def test_event_shop_v2_renderer_exposes_live_value_nodes():
    source = SHOP_V2.read_text(encoding="utf-8")

    assert 'id="event-shop-v2-plan-count"' in source
    assert 'id="event-shop-v2-plan-cost"' in source
    assert 'id="event-shop-selected-{live_key}"' in source
    assert 'id="event-shop-cost-{live_key}"' in source
    assert 'id="event-shop-plan-total"' not in source
    assert 'id="event-shop-plan-count"' not in source


def test_shop_item_dom_key_is_stable_and_identity_derived():
    left = EventPlannerMixin._shop_item_dom_key(
        ("item-a", "Тестовый товар", "", 2000, 10)
    )
    same = EventPlannerMixin._shop_item_dom_key(
        ("item-a", "Тестовый товар", "", 2000, 10)
    )
    other = EventPlannerMixin._shop_item_dom_key(
        ("item-a", "Тестовый товар", "", 2000, 11)
    )

    assert left == same
    assert left != other
    assert len(left) == 16
    assert all(character in "0123456789abcdef" for character in left)


def test_quantity_change_patches_live_values_without_plan_rerender():
    planner = _LivePlanner()
    identity = planner._shop_item_identity(planner.plan["shop_items"][0])

    planner._change_shop_quantity(identity, "increment")

    assert planner.plan["shop_items"][0]["selected"] == 1
    assert planner.messages == [""]
    assert planner.refreshes == 0
    assert planner.synced_targets == [
        {
            "event_id": "event-test",
            "row_id": "item-a",
            "previous_selected": 0,
            "selected": 1,
        }
    ]
    assert planner.patches == [
        (
            identity,
            {
                "selected": 1,
                "cost": 2000,
                "total": 2000,
                "selected_count": 1,
            },
        )
    ]


def test_quantity_max_and_increment_share_proven_capacity(monkeypatch):
    planner = _LivePlanner()
    planner.capacity = 2
    identity = planner._shop_item_identity(planner.plan["shop_items"][0])
    warnings = []
    monkeypatch.setattr(
        "module.webui.app_event_planner.toast",
        lambda message, **_kwargs: warnings.append(message),
    )

    planner._change_shop_quantity(identity, "maximum")
    assert planner.plan["shop_items"][0]["selected"] == 2
    assert planner.synced_targets[-1]["selected"] == 2

    planner._change_shop_quantity(identity, "increment")
    assert planner.plan["shop_items"][0]["selected"] == 2
    assert warnings == ["Доступная ёмкость цели по подтверждённому остатку: 2"]
    assert planner.patches == [
        (
            identity,
            {
                "selected": 2,
                "cost": 4000,
                "total": 4000,
                "selected_count": 1,
            },
        )
    ]


def test_event_shop_refresh_updates_only_plan_scope():
    planner = _Planner()
    scopes = []

    @contextmanager
    def fake_scope(name, clear=False):
        scopes.append((name, clear))
        yield

    with patch("module.webui.app_event_planner.use_scope", fake_scope):
        planner._refresh_event_plan_page()

    assert scopes == [("group_EventShopPlan", True)]
    assert planner.rendered == [planner.alas_config.data]
    assert planner.full_refreshes == []


def test_non_shop_event_refresh_keeps_existing_full_page_behavior():
    planner = _Planner()
    planner._event_plan_active_task = "EventGeneral"

    planner._refresh_event_plan_page()

    assert planner.rendered == []
    assert planner.full_refreshes == ["EventGeneral"]
