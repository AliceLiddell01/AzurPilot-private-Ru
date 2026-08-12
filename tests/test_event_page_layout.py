from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "module" / "webui" / "app_event_layout.py"
PLANNER = ROOT / "module" / "webui" / "app_event_planner.py"
SHOP_SAFETY = ROOT / "module" / "webui" / "app_event_shop_safety.py"
APP = ROOT / "module" / "webui" / "app.py"
TASKS = ROOT / "module" / "config" / "argument" / "task.yaml"


def test_event_layout_is_inserted_before_generic_task_renderer():
    source = APP.read_text(encoding="utf-8")
    profiles = source.index("    EventProfilesMixin,")
    safety = source.index("    EventShopSafetyMixin,")
    layout = source.index("    EventLayoutMixin,")
    generic = source.index("    TaskConfigMixin,")
    assert profiles < safety < layout < generic


def test_event_pages_clear_content_without_wrapping_unrelated_pages():
    source = LAYOUT.read_text(encoding="utf-8")
    assert '@use_scope("content", clear=True)\n    def _alas_set_event_group' in source
    assert 'if task not in EVENT_LAYOUT_TASKS:\n            return super().alas_set_group(task)' in source
    assert '@use_scope("content", clear=True)\n    def alas_set_group' not in source


def test_event_map_progressive_disclosure_contract():
    source = LAYOUT.read_text(encoding="utf-8")
    for group in ("Scheduler", "Campaign", "StopCondition", "Fleet", "Emotion"):
        assert f'    "{group}",' in source
    for group in ("Submarine", "HpControl", "EnemyPriority"):
        assert f'    "{group}",' in source
    assert 'title="Расширенные настройки карты"' in source
    assert "event-advanced-details" in source


def test_advanced_groups_do_not_precreate_generic_pywebio_scopes():
    source = LAYOUT.read_text(encoding="utf-8")
    assert '*[put_scope(f"group_{name}") for name in existing]' not in source
    assert 'scope_ids = [f"pywebio-scope-group_{name}" for name in rendered_names]' in source
    assert "body.appendChild(node)" in source
    assert "previous implementation pre-created the same scopes" in source


def test_event_general_replaces_raw_sentinels_with_clear_stop_controls():
    layout = LAYOUT.read_text(encoding="utf-8")
    assert 'put_scope("group_EventStop")' in layout
    assert 'put_text("Цель и автостоп ивента")' in layout
    assert "from module.config.time_sentinel import DEFAULT_TIME_TEXT" in layout
    assert "_DISABLED_EVENT_TIME = DEFAULT_TIME_TEXT" in layout
    assert 'return "Без ограничения"' in layout
    assert 'name="EventGeneral"' not in layout
    assert "_event_write_allowed" in layout


def test_event_general_uses_new_local_plan_and_keeps_bwiki_as_explicit_fallback():
    layout = LAYOUT.read_text(encoding="utf-8")
    planner = PLANNER.read_text(encoding="utf-8")
    assert 'put_scope("group_EventPlan")' in layout
    assert "self._render_event_plan_general(config)" in layout
    assert 'title="Резервный источник — BWiki (legacy)"' in layout
    assert "BWiki больше не используется как основной калькулятор" in layout
    assert "load_event_calculator(force_refresh=True)" in layout
    assert "self._import_legacy_bwiki_cache" in layout
    assert 'put_scope("group_EventCalculator")' not in layout
    assert "self._render_event_calculator(config)" not in layout
    assert 'title="Расширенные настройки — баланс задач"' in layout
    assert "Локальная модель не зависит от BWiki" in planner
    assert 'deep_get(config, "Dashboard.Pt.Value", 0)' in planner
    assert 'deep_get(config, "Dashboard.Pt.Record", "")' in planner
    assert 'progress.get("current_pt", 0)' in planner
    assert 'progress.get("pt_mode")' in planner
    assert 'forecast["recurring_pt"]' in planner
    assert 'forecast["farm_required_pt"]' in planner
    assert "current_time().date().isoformat()" in planner
    assert "today=now" in planner


def test_event_shop_plan_is_primary_and_runtime_controls_are_advanced():
    layout = LAYOUT.read_text(encoding="utf-8")
    planner = PLANNER.read_text(encoding="utf-8")
    safety = SHOP_SAFETY.read_text(encoding="utf-8")
    assert 'put_scope("group_EventShopPlan")' in layout
    assert "self._render_event_shop_plan(config)" in layout
    assert 'title="Расширенные настройки — автоматизация магазина"' in layout
    assert "Количество берётся из плана автоматически" in planner
    assert "selected_shop_items_partial" not in planner
    assert '"EventShop.EventShop.PresetFilter": "custom"' in safety
    assert '"EventShop.EventShop.CustomFilter": compiled.filter_text' in safety
    assert '"EventShop.EventShop.UnlockSSRShip": False' in safety
    assert '"EventShop.EventShop.BuyURShip": 0' in safety
    assert '"EventGeneral.EventGeneral.PtLimit": total' not in safety
    assert "Синхронизация магазина намеренно не меняет цель" in safety
    assert 'header=["Товар", "Цена", "Доступно", "Купить", "Итого", ""]' in planner
    assert 'header=["Товар", "Цена", "Доступно", "В плане", "Итого", "Фильтр", ""]' not in planner


def test_stage_two_does_not_remove_runtime_event_groups():
    source = TASKS.read_text(encoding="utf-8")
    expected = (
        "    EventGeneral:\n      - EventGeneral\n      - TaskBalancer",
        "    Event:\n      - Scheduler\n      - Campaign\n      - StopCondition\n      - Fleet\n      - Submarine\n      - Emotion\n      - HpControl\n      - EnemyPriority",
        "    EventShop:\n      - Scheduler\n      - EventShop",
    )
    for fragment in expected:
        assert fragment in source