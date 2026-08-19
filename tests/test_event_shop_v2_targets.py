from pathlib import Path

from tests.event_css_contract_helpers import (
    EVENT_SHOP_CSS,
    EVENT_SHOP_HERO_SELECTOR,
    EVENT_SHOP_PLAN_SELECTOR,
    EVENT_SHOP_SETTINGS_SELECTOR,
    css_block,
)


ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "module" / "webui" / "app_event_shop_v2.py"
CSS = EVENT_SHOP_CSS


def test_event_shop_v2_restores_quantity_target_controls():
    source = V2.read_text(encoding="utf-8")

    assert '"−"' in source
    assert '"+"' in source
    assert '"MAX"' in source
    assert '"Сброс"' in source
    assert '"decrement"' in source
    assert '"increment"' in source
    assert '"maximum"' in source
    assert '"clear"' in source
    assert 'id="event-shop-selected-{live_key}"' in source
    assert 'id="event-shop-target-left-{live_key}"' in source


def test_event_shop_v2_keeps_quantity_and_priority_as_separate_controls():
    source = V2.read_text(encoding="utf-8")

    assert "Цель покупки" in source
    assert 'label="Приоритет"' in source
    assert "Для автоматической покупки должны быть заданы и цель больше 0, и приоритет" in source


def test_event_shop_v2_renders_currency_icon_in_balance_and_prices():
    source = V2.read_text(encoding="utf-8")

    assert 'class="event-shop-v2-balance"' in source
    assert 'class="event-shop-v2-price"' in source
    assert 'currency_asset = event_asset_url(currency.get("asset"))' in source


def test_event_shop_v2_keeps_availability_next_to_terminal_status():
    source = V2.read_text(encoding="utf-8")

    assert 'priority_state.get("completed")' in source
    assert "Полностью куплено" in source
    assert 'event-shop-v2-stock">Доступно: {available}' in source
    assert "state_html = status_html + availability_html" in source


def test_event_shop_v2_flattens_layout_but_keeps_component_surfaces():
    css = CSS.read_text(encoding="utf-8")

    plan = css_block(css, EVENT_SHOP_PLAN_SELECTOR)
    assert "background: transparent !important;" in plan
    assert "box-shadow: none !important;" in plan
    assert "backdrop-filter: none !important;" in plan

    settings = css_block(css, EVENT_SHOP_SETTINGS_SELECTOR)
    assert "background: var(--event-surface) !important;" in settings
    assert "border: 1px solid var(--event-border) !important;" in settings

    hero = css_block(css, EVENT_SHOP_HERO_SELECTOR)
    assert "background: transparent;" in hero


def test_event_shop_task_fields_use_vertical_layout_instead_of_narrow_columns():
    css = CSS.read_text(encoding="utf-8")
    marker = '#pywebio-scope-event_shop_task_fields > [id^="pywebio-scope-arg_container_"] {'
    block = css_block(css, marker)

    assert "grid-template-columns: 1fr" in block
    assert "align-items: stretch" in block
