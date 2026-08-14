from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "assets" / "gui" / "css" / "event-shop-stability-alas.css"
V2 = ROOT / "module" / "webui" / "app_event_shop_v2.py"


def test_event_shop_outer_row_is_not_an_extra_surface():
    css = CSS.read_text(encoding="utf-8")
    selector = (
        '#pywebio-scope-content.event-modern-page[data-event-task="EventShop"] '
        '#pywebio-scope-groups > .row {'
    )
    block = css.split(selector, 1)[1].split("}", 1)[0]

    assert "border: 0 !important" in block
    assert "background: transparent !important" in block
    assert "box-shadow: none !important" in block
    assert "padding: 0 !important" in block


def test_task_text_inputs_do_not_stretch_checkbox_switches():
    css = CSS.read_text(encoding="utf-8")

    assert 'input:not([type="checkbox"]):not([type="radio"])' in css
    assert ".custom-switch .custom-control-label" in css
    assert "width: 40px !important" in css
    assert "height: 20px !important" in css


def test_event_shop_notification_copy_matches_scheduler_semantics():
    source = V2.read_text(encoding="utf-8")

    assert 'title="Push-уведомление об ошибке"' not in source
    assert 'title="Уведомлять о завершении"' in source
    assert "Ошибки используют общий канал OnePush" in source
    assert "Если провайдер там не задан" in source
