"""Общие CSS-контракты EventShop для тестов presentation-layer."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVENT_SHOP_CSS = ROOT / "assets" / "gui" / "css" / "event-shop-stability-alas.css"
EVENT_SHOP_PLAN_SELECTOR = (
    '#pywebio-scope-content.event-modern-page[data-event-task="EventShop"] '
    '#pywebio-scope-group_EventShopPlan {'
)
EVENT_SHOP_SETTINGS_SELECTOR = (
    '#pywebio-scope-content.event-modern-page[data-event-task="EventShop"] '
    '#pywebio-scope-group_EventShopTaskSettings {'
)
EVENT_SHOP_HERO_SELECTOR = ".event-shop-v2-hero {"


def css_block(css: str, selector: str) -> str:
    """Вернуть тело точного CSS-блока или явно провалить контракт селектора."""

    assert selector in css
    return css.split(selector, 1)[1].split("}", 1)[0]
