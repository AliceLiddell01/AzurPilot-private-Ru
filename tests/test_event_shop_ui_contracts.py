from pathlib import Path

from tests.event_css_contract_helpers import (
    EVENT_SHOP_CSS,
    EVENT_SHOP_PLAN_SELECTOR,
    EVENT_SHOP_SETTINGS_SELECTOR,
    css_block,
)


ROOT = Path(__file__).resolve().parents[1]
CSS = EVENT_SHOP_CSS
MATERIAL_CSS = ROOT / "assets" / "gui" / "css" / "advanced-material-alas.css"
V2 = ROOT / "module" / "webui" / "app_event_shop_v2.py"


def test_event_shop_surface_contract_keeps_only_component_surfaces():
    css = CSS.read_text(encoding="utf-8")
    material = MATERIAL_CSS.read_text(encoding="utf-8")

    # Material-тема оформляет каждого прямого потомка #groups, включая wrapper put_row().
    assert "#pywebio-scope-groups>*:not(#pywebio-scope-navigator)" in material

    wrapper_selector = (
        '#pywebio-scope-content.event-modern-page[data-event-task="EventShop"] '
        '#pywebio-scope-groups > * {'
    )
    wrapper = css_block(css, wrapper_selector)
    for token in (
        "border: 0 !important",
        "border-radius: 0 !important",
        "background: transparent !important",
        "box-shadow: none !important",
        "-webkit-backdrop-filter: none !important",
        "backdrop-filter: none !important",
        "padding: 0 !important",
        "animation: none !important",
    ):
        assert token in wrapper

    plan = css_block(css, EVENT_SHOP_PLAN_SELECTOR)
    assert "background: transparent !important" in plan
    assert "backdrop-filter: none !important" in plan

    settings = css_block(css, EVENT_SHOP_SETTINGS_SELECTOR)
    assert "background: var(--event-surface) !important" in settings
    assert "border: 1px solid var(--event-border) !important" in settings

    cards = css_block(
        css,
        '#pywebio-scope-event_shop_v2_grid > [id^="pywebio-scope-event_shop_card_"] {',
    )
    assert "background: var(--event-surface) !important" in cards
    assert "backdrop-filter: blur(12px) saturate(150%) !important" in cards


def test_task_text_inputs_do_not_stretch_checkbox_switches():
    css = CSS.read_text(encoding="utf-8")

    assert 'input:not([type="checkbox"]):not([type="radio"])' in css
    assert ".custom-switch .custom-control-label" in css
    assert "width: 40px !important" in css
    assert "height: 20px !important" in css

    base_thumb = css_block(
        css,
        "#pywebio-scope-event_shop_task_fields .custom-switch "
        ".custom-control-label::after {",
    )
    checked_thumb = css_block(
        css,
        "#pywebio-scope-event_shop_task_fields .custom-switch "
        ".custom-control-input:checked ~ .custom-control-label::after {",
    )
    assert "transform: translateX(0) !important" in base_thumb
    assert "transform: translateX(20px) !important" in checked_thumb


def test_event_shop_notification_copy_matches_scheduler_semantics():
    source = V2.read_text(encoding="utf-8")

    assert 'title="Push-уведомление об ошибке"' not in source
    assert 'title="Уведомлять о завершении"' in source
    assert "Ошибки используют общий канал OnePush" in source
    assert "Если провайдер там не задан" in source
