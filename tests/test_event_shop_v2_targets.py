from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "module" / "webui" / "app_event_shop_v2.py"
CSS = ROOT / "assets" / "gui" / "css" / "event-shop-stability-alas.css"


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


def test_event_shop_v2_flattens_nested_group_surfaces():
    css = CSS.read_text(encoding="utf-8")

    assert "#pywebio-scope-group_EventShopPlan," in css
    assert "#pywebio-scope-group_EventShopTaskSettings {" in css
    assert "background: transparent !important;" in css
    assert "box-shadow: none !important;" in css
    assert ".event-shop-v2-hero" in css
    assert "background: transparent;" in css


def test_event_shop_task_fields_use_vertical_layout_instead_of_narrow_columns():
    css = CSS.read_text(encoding="utf-8")
    marker = '#pywebio-scope-event_shop_task_fields > [id^="pywebio-scope-arg_container_"] {'
    block = css.split(marker, 1)[1].split("}", 1)[0]

    assert "grid-template-columns: 1fr" in block
    assert "align-items: stretch" in block
