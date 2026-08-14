from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from module.webui.app_event_planner import EventPlannerMixin


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "module" / "webui" / "app.py"
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


def test_event_shop_styles_are_loaded_before_gui_content():
    source = APP.read_text(encoding="utf-8")
    event_css = 'add_css(filepath_css("event-profiles-alas"))'
    stability_css = 'add_css(filepath_css("event-shop-stability-alas"))'
    gui_start = "gui = AlasGUI()"

    assert event_css in source
    assert stability_css in source
    assert source.index(event_css) < source.index(gui_start)
    assert source.index(stability_css) < source.index(gui_start)


def test_event_shop_scope_ids_have_critical_layout_without_post_render_js():
    css = CRITICAL_CSS.read_text(encoding="utf-8")

    assert "#pywebio-scope-event_shop_grid" in css
    assert '[id^="pywebio-scope-event_shop_card_"]' in css
    assert "grid-template-columns: repeat(auto-fit, minmax(220px, 1fr))" in css
    assert "display: flex" in css


def test_event_shop_icon_box_keeps_square_geometry_when_card_width_changes():
    css = CRITICAL_CSS.read_text(encoding="utf-8")
    marker = ".event-shop-card-visual > img {"
    block = css.split(marker, 1)[1].split("}", 1)[0]

    assert "height: auto" in block
    assert "aspect-ratio: 1 / 1" in block
    assert "height: 112px" not in block


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
