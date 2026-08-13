from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from threading import Barrier, Thread
from time import sleep
from unittest.mock import patch

import pytest

from module.webui import app_event_layout, app_event_planner, app_event_shop_safety
from module.webui.app_event_layout import EventLayoutMixin
from module.webui.app_event_planner import EventPlannerMixin
from module.webui.app_event_shop_safety import EventShopSafetyMixin
from module.webui.event_config import mutate_event_config, update_event_config
from module.webui.event_plan import empty_event_plan
from module.webui.event_profiles import add_event_profile, get_event_profile_metadata
from module.webui.event_rose_tower_fixture import rose_tower_fixture_plan


class MemoryConfig:
    def __init__(self, data, *, fail_write: bool = False):
        self.data = deepcopy(data)
        self.fail_write = fail_write
        self.load_count = 0

    def read_file(self, _name):
        return deepcopy(self.data)

    def write_file(self, _name, data):
        if self.fail_write:
            raise OSError("simulated write failure")
        self.data = deepcopy(data)

    def load(self):
        self.load_count += 1


class CorruptOnceConfig(MemoryConfig):
    def __init__(self, data):
        super().__init__(data)
        self.corrupt_next_write = True

    def write_file(self, name, data):
        super().write_file(name, data)
        if self.corrupt_next_write:
            self.corrupt_next_write = False
            self.data["EventShop"]["Scheduler"]["Enable"] = False


class SafetyProbe(EventShopSafetyMixin, EventPlannerMixin):
    alas_name = "ap"

    def __init__(self, config):
        self.alas_config = config


def _runtime_config():
    return {
        "EventGeneral": {"EventGeneral": {"PtLimit": 777}},
        "EventShop": {
            "Scheduler": {"Enable": False},
            "EventShop": {
                "UnlockSSRShip": True,
                "BuyURShip": 2,
                "PresetFilter": "custom",
                "CustomFilter": "legacy",
            },
        },
    }


def _shop_plan(token: str = "Cube"):
    plan = empty_event_plan()
    plan["shop_items"] = [
        {"name": "Cube", "price": 100, "stock": 5, "selected": 3, "filter": token}
    ]
    return plan


def test_event_shop_write_failure_is_reported_instead_of_false_success():
    data = _runtime_config()
    data["EventShop"]["Scheduler"]["Enable"] = True
    config = MemoryConfig(data, fail_write=True)
    probe = SafetyProbe(config)

    with patch.object(app_event_shop_safety, "toast"), patch.object(
        app_event_shop_safety.logger, "exception"
    ):
        assert probe._set_event_shop_scheduler(False) is False

    assert config.load_count == 0
    assert config.data["EventShop"]["Scheduler"]["Enable"] is True


def test_valid_shop_sync_preserves_pt_limit_and_scheduler_state():
    config = MemoryConfig(_runtime_config())
    probe = SafetyProbe(config)

    with patch.object(app_event_shop_safety, "toast"):
        assert probe._sync_shop_plan_fail_closed(_shop_plan(), announce=True) is True

    assert config.data["EventGeneral"]["EventGeneral"]["PtLimit"] == 777
    assert config.data["EventShop"]["Scheduler"]["Enable"] is False
    assert config.data["EventShop"]["EventShop"] == {
        "UnlockSSRShip": False,
        "BuyURShip": 0,
        "PresetFilter": "custom",
        "CustomFilter": "cube:3",
    }


def test_config_verification_failure_restores_original_and_names_bad_key():
    original = _runtime_config()
    config = CorruptOnceConfig(original)

    with pytest.raises(
        OSError,
        match=r"EventShop\.Scheduler\.Enable",
    ):
        update_event_config(
            config,
            "ap",
            {"EventShop.Scheduler.Enable": True},
        )

    assert config.data == original


def test_ambiguous_shop_selector_disables_scheduler_without_reusing_old_filter():
    data = _runtime_config()
    data["EventShop"]["Scheduler"]["Enable"] = True
    config = MemoryConfig(data)
    probe = SafetyProbe(config)

    with patch.object(app_event_shop_safety, "toast"):
        assert probe._sync_shop_plan_fail_closed(
            _shop_plan("PlateT3"), announce=True
        ) is False

    assert config.data["EventShop"]["Scheduler"]["Enable"] is False
    assert config.data["EventShop"]["EventShop"]["CustomFilter"] == "legacy"
    assert config.data["EventGeneral"]["EventGeneral"]["PtLimit"] == 777


def test_plan_clear_from_another_event_page_still_pauses_shop_scheduler():
    data = _runtime_config()
    data["EventShop"]["Scheduler"]["Enable"] = True
    config = MemoryConfig(data)
    probe = SafetyProbe(config)
    probe._event_plan_active_task = "EventGeneral"

    with patch.object(
        EventPlannerMixin, "_event_plan_write", return_value=True
    ), patch.object(app_event_shop_safety, "toast"):
        assert probe._event_plan_write(empty_event_plan(), "Очищено") is True

    assert config.data["EventShop"]["Scheduler"]["Enable"] is False


class LayoutProbe(EventLayoutMixin):
    alas_name = "ap"

    def __init__(self, plan, *, fail_runtime_write: bool = False):
        self.plan = deepcopy(plan)
        self.runtime_updates = []
        self.fail_runtime_write = fail_runtime_write

    def _event_write_allowed(self):
        return True

    def _event_plan(self):
        return deepcopy(self.plan)

    def _event_plan_write(self, plan, _message):
        self.plan = deepcopy(plan)
        return True

    def _event_config_update(self, updates):
        if self.fail_runtime_write:
            raise OSError("simulated runtime write failure")
        self.runtime_updates.append(dict(updates))

    def _refresh_event_plan_page(self):
        pass


def _settings_pins(plan, *, farm_end=None, shop_end=None, target=321):
    event = plan["event"]
    return {
        app_event_layout._NAME: event["name"],
        app_event_layout._FARM_END: event["farm_end"] if farm_end is None else farm_end,
        app_event_layout._SHOP_END: event["shop_end"] if shop_end is None else shop_end,
        app_event_layout._PT_MODE: "auto",
        app_event_layout._CURRENT_PT: 0,
        app_event_layout._TARGET_PT: target,
    }


def test_shop_date_edit_does_not_verify_or_apply_imported_farm_end():
    fixture = rose_tower_fixture_plan()
    probe = LayoutProbe(fixture)
    pins = _settings_pins(fixture, shop_end="2025-06-19 23:59:59")

    with patch.object(app_event_layout, "pin", pins), patch.object(
        app_event_layout, "close_popup"
    ), patch.object(app_event_layout, "toast"):
        probe._save_settings_popup()

    assert probe.plan["event"]["source"]["kind"] == "manual"
    assert probe.plan["event"]["source"]["verified"] is False
    assert probe.runtime_updates == [{"EventGeneral.EventGeneral.PtLimit": 321}]


def test_manual_farm_date_edit_applies_runtime_time_limit():
    fixture = rose_tower_fixture_plan()
    probe = LayoutProbe(fixture)
    pins = _settings_pins(fixture, farm_end="2025-06-12 23:59:59")

    with patch.object(app_event_layout, "pin", pins), patch.object(
        app_event_layout, "close_popup"
    ), patch.object(app_event_layout, "toast"):
        probe._save_settings_popup()

    assert probe.plan["event"]["source"]["verified"] is True
    assert probe.runtime_updates == [
        {
            "EventGeneral.EventGeneral.PtLimit": 321,
            "EventGeneral.EventGeneral.TimeLimit": "2025-06-12 23:59:59",
        }
    ]


def test_settings_runtime_write_failure_rolls_back_local_plan():
    fixture = rose_tower_fixture_plan()
    probe = LayoutProbe(fixture, fail_runtime_write=True)
    pins = _settings_pins(fixture, farm_end="2025-06-12 23:59:59")

    with patch.object(app_event_layout, "pin", pins), patch.object(
        app_event_layout, "close_popup"
    ) as close_popup, patch.object(app_event_layout, "toast"), patch.object(
        app_event_layout.logger, "exception"
    ):
        probe._save_settings_popup()

    assert probe.plan == fixture
    close_popup.assert_not_called()


class MenuParent:
    def init_menu(self, collapse_menu=True, name=None):
        return collapse_menu, name


class MenuProbe(EventLayoutMixin, MenuParent):
    def __init__(self):
        self.unmarked = 0

    def _unmark_event_page(self):
        self.unmarked += 1


def test_event_dom_marker_is_removed_when_overview_becomes_active():
    probe = MenuProbe()

    assert probe.init_menu(name="Event") is None
    assert probe.unmarked == 0
    assert probe.init_menu(name="Overview") is None
    assert probe.unmarked == 1


def test_timezone_aware_dashboard_record_is_accepted_without_mixed_datetime_error():
    config = {
        "Dashboard": {
            "Pt": {
                "Value": 4567,
                "Record": "2026-08-13T10:00:00+07:00",
            }
        }
    }
    plan = empty_event_plan()

    with patch.object(
        app_event_planner,
        "current_time",
        return_value=datetime(2026, 8, 13, 11, 0, 0),
    ):
        current_pt, source = EventPlannerMixin._current_pt_for_plan(config, plan)

    assert current_pt == 4567
    assert source.startswith("Автоматически из OCR")


class PlanMutationProbe(EventPlannerMixin):
    def __init__(self):
        self.store = empty_event_plan()

    def _event_plan(self):
        return deepcopy(self.store)

    def _event_plan_write(self, plan, _message):
        sleep(0.01)
        self.store = deepcopy(plan)
        return True


def test_event_plan_mutations_are_serialized_across_sessions():
    probe = PlanMutationProbe()
    start = Barrier(3)

    def worker(name):
        start.wait()

        def mutation(plan):
            plan["stages"].append({"name": name, "points": 100})
            sleep(0.01)

        assert probe._event_plan_mutate(mutation, "") is True

    threads = [Thread(target=worker, args=(name,)) for name in ("A", "B")]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert {item["name"] for item in probe.store["stages"]} == {"A", "B"}


def test_event_profile_mutations_share_one_read_modify_write_lock():
    config = MemoryConfig(
        {
            "EventGeneral": {"Storage": {"Storage": {}}},
            "Event2": {"Scheduler": {"Enable": False}},
            "Event3": {"Scheduler": {"Enable": False}},
        }
    )
    start = Barrier(3)

    def worker(name):
        start.wait()
        mutate_event_config(config, "ap", lambda data: add_event_profile(data, name))

    threads = [Thread(target=worker, args=(name,)) for name in ("Первый", "Второй")]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert set(get_event_profile_metadata(config.data)) == {"Event2", "Event3"}
