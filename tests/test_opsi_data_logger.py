from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from module.config.deep import deep_get, deep_set
from module.config.opsi_data_logger import (
    DATA_LOGGER_CYCLE_KEY,
    DATA_LOGGER_INTENT_PATH,
    DATA_LOGGER_RETRY_PENDING_KEY,
    DATA_LOGGER_STORAGE_PATH,
    DATA_LOGGER_VALID_UNTIL_KEY,
    DataLoggerShopResult,
    DataLoggerShopState,
    DataLoggerStorageState,
    data_logger_cycle_key,
    data_logger_is_active,
    data_logger_mark_active,
    data_logger_retry_pending,
    data_logger_set_retry,
)
from module.os.tasks.explore import OpsiExplore
from module.os.tasks.voucher import DATA_LOGGER_RETRY_MINUTES, OpsiVoucher

NEXT_RESET = datetime(2026, 9, 1, 7, 0, 0)
FIXED_UTC_NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


class FakeConfig:
    def __init__(self, *, intent: bool):
        self.data = {
            "OpsiExplore": {
                "OpsiExplore": {"SpecialRadar": intent},
                "Storage": {"Storage": {}},
            }
        }
        self.delays = []

    def cross_get(self, keys, default=None):
        return deep_get(self.data, keys=keys, default=default)

    def cross_set(self, keys, value):
        deep_set(self.data, keys=keys, value=value)

    def task_delay(
        self,
        *,
        target=None,
        minute=None,
        server_update=False,
    ):
        self.delays.append(
            {
                "target": target,
                "minute": minute,
                "server_update": server_update,
            }
        )


class FakeShop:
    def __init__(self, result):
        self.result = result
        self.ensure_calls = 0
        self.run_calls = 0
        self.shop_filter = "Book > Fragment"

    def ensure_data_logger(self):
        self.ensure_calls += 1
        return self.result

    def run(self):
        self.run_calls += 1


class VoucherHarness(OpsiVoucher):
    def __init__(self, config, shop, storage_state=DataLoggerStorageState.UNKNOWN):
        self.config = config
        self.device = SimpleNamespace()
        self.shop = shop
        self.storage_state = storage_state
        self.enter_calls = 0
        self.exit_calls = 0
        self.storage_calls = 0

    def _create_voucher_shop(self):
        return self.shop

    def _os_voucher_enter(self):
        self.enter_calls += 1

    def _os_voucher_exit(self):
        self.exit_calls += 1

    def _data_logger_storage_lifecycle(self):
        self.storage_calls += 1
        return self.storage_state


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(
        "module.config.opsi_data_logger.current_time",
        lambda _tz=None: FIXED_UTC_NOW,
    )
    monkeypatch.setattr(
        "module.config.opsi_data_logger.server_timezone",
        lambda: timedelta(hours=8),
    )
    monkeypatch.setattr(
        "module.config.opsi_data_logger.get_os_next_reset",
        lambda: NEXT_RESET,
    )
    monkeypatch.setattr(
        "module.os.tasks.voucher.get_os_next_reset",
        lambda: NEXT_RESET,
    )


def storage(config):
    return deep_get(config.data, keys=DATA_LOGGER_STORAGE_PATH, default={})


def monthly_delay():
    return {
        "target": NEXT_RESET,
        "minute": None,
        "server_update": False,
    }


def retry_delay():
    return {
        "target": None,
        "minute": DATA_LOGGER_RETRY_MINUTES,
        "server_update": True,
    }


def scheduler_config(*, active: bool):
    config = __import__(
        "module.config.config",
        fromlist=["AzurLaneConfig"],
    ).AzurLaneConfig.__new__(
        __import__(
            "module.config.config",
            fromlist=["AzurLaneConfig"],
        ).AzurLaneConfig
    )
    config.data = {
        "OpsiExplore": {
            "OpsiExplore": {
                "SpecialRadar": True,
                "ForceRun": False,
            },
            "Scheduler": {"NextRun": datetime(2026, 8, 1)},
            "Storage": {"Storage": {}},
        },
        "OpsiObscure": {
            "OpsiObscure": {"ForceRun": False},
            "Scheduler": {"NextRun": datetime(2026, 8, 1)},
        },
        "OpsiStronghold": {
            "OpsiStronghold": {"ForceRun": False},
            "Scheduler": {"NextRun": datetime(2026, 8, 1)},
        },
    }
    if active:
        deep_set(
            config.data,
            keys=f"{DATA_LOGGER_STORAGE_PATH}.{DATA_LOGGER_CYCLE_KEY}",
            value="2026-08",
        )
    config.modified = {}
    config.update = lambda: None
    return config


def test_cycle_key_uses_server_calendar_month():
    assert data_logger_cycle_key(server_now=datetime(2026, 8, 31, 23, 59)) == "2026-08"
    assert data_logger_cycle_key(server_now=datetime(2026, 9, 1, 0, 0)) == "2026-09"


def test_cycle_key_is_stable_across_local_dst_representations(monkeypatch):
    monkeypatch.setattr(
        "module.config.opsi_data_logger.server_timezone",
        lambda: timedelta(hours=-7),
    )
    instant = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    summer_local = instant.astimezone(timezone(timedelta(hours=3)))
    winter_local = instant.astimezone(timezone(timedelta(hours=2)))

    assert data_logger_cycle_key(server_now=summer_local) == "2026-10"
    assert data_logger_cycle_key(server_now=winter_local) == "2026-10"


def test_cycle_rolls_over_at_server_midnight_not_local_midnight(monkeypatch):
    monkeypatch.setattr(
        "module.config.opsi_data_logger.server_timezone",
        lambda: timedelta(hours=-7),
    )
    before = datetime(2026, 9, 1, 6, 59, tzinfo=timezone.utc)
    after = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)

    assert data_logger_cycle_key(server_now=before) == "2026-08"
    assert data_logger_cycle_key(server_now=after) == "2026-09"


def test_explicit_next_reset_compatibility_returns_preceding_cycle():
    assert data_logger_cycle_key(datetime(2026, 9, 1)) == "2026-08"
    assert data_logger_cycle_key(datetime(2026, 10, 1)) == "2026-09"


def test_mark_active_writes_cycle_and_removes_legacy_timestamp():
    config = FakeConfig(intent=True)
    deep_set(
        config.data,
        keys=f"{DATA_LOGGER_STORAGE_PATH}.{DATA_LOGGER_VALID_UNTIL_KEY}",
        value=NEXT_RESET.isoformat(sep=" "),
    )

    cycle = data_logger_mark_active(config)

    assert cycle == "2026-08"
    assert storage(config)[DATA_LOGGER_CYCLE_KEY] == "2026-08"
    assert DATA_LOGGER_VALID_UNTIL_KEY not in storage(config)


def test_legacy_exact_local_reset_value_is_read_only_for_migration():
    config = FakeConfig(intent=True)
    deep_set(
        config.data,
        keys=f"{DATA_LOGGER_STORAGE_PATH}.{DATA_LOGGER_VALID_UNTIL_KEY}",
        value=NEXT_RESET.isoformat(sep=" "),
    )

    assert data_logger_is_active(config)


def test_disabled_setting_runs_only_ordinary_shop():
    config = FakeConfig(intent=False)
    shop = FakeShop(DataLoggerShopResult(DataLoggerShopState.UNKNOWN))
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert shop.ensure_calls == 0
    assert shop.run_calls == 1
    assert task.storage_calls == 0
    assert config.delays == [monthly_delay()]


def test_valid_monthly_state_skips_shop_check_and_storage():
    config = FakeConfig(intent=True)
    data_logger_mark_active(config)
    shop = FakeShop(DataLoggerShopResult(DataLoggerShopState.UNKNOWN))
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert shop.ensure_calls == 0
    assert shop.run_calls == 1
    assert task.storage_calls == 0
    assert config.delays == [monthly_delay()]


def test_confirmed_activation_persists_server_cycle():
    config = FakeConfig(intent=True)
    shop = FakeShop(
        DataLoggerShopResult(
            DataLoggerShopState.SOLD_OUT,
            reason="confirmed_sold_out",
        )
    )
    task = VoucherHarness(
        config,
        shop,
        storage_state=DataLoggerStorageState.ACTIVATED,
    )

    task.os_voucher()

    assert shop.ensure_calls == 1
    assert shop.run_calls == 1
    assert task.storage_calls == 1
    assert data_logger_is_active(config)
    assert storage(config)[DATA_LOGGER_CYCLE_KEY] == "2026-08"
    assert DATA_LOGGER_RETRY_PENDING_KEY not in storage(config)
    assert config.delays == [monthly_delay()]


def test_sold_out_and_storage_absence_retries_without_false_success():
    config = FakeConfig(intent=True)
    shop = FakeShop(
        DataLoggerShopResult(
            DataLoggerShopState.SOLD_OUT,
            reason="confirmed_sold_out",
        )
    )
    task = VoucherHarness(
        config,
        shop,
        storage_state=DataLoggerStorageState.ABSENT,
    )

    task.os_voucher()

    assert task.storage_calls == 1
    assert not data_logger_is_active(config)
    assert storage(config)[DATA_LOGGER_RETRY_PENDING_KEY] is True
    assert storage(config)["OperationSirenDataLoggerRetryReason"] == "storage_absent"
    assert config.delays == [retry_delay()]


def test_unknown_shop_state_retries_at_six_hours_or_daily_reset():
    config = FakeConfig(intent=True)
    shop = FakeShop(
        DataLoggerShopResult(
            DataLoggerShopState.UNKNOWN,
            reason="full_scan_inconclusive",
        )
    )
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert not data_logger_is_active(config)
    assert config.delays == [retry_delay()]


def test_retry_only_run_does_not_repeat_ordinary_filter():
    config = FakeConfig(intent=True)
    data_logger_set_retry(config, "previous_unknown")
    shop = FakeShop(
        DataLoggerShopResult(
            DataLoggerShopState.UNKNOWN,
            reason="still_unknown",
        )
    )
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert shop.shop_filter == "Book > Fragment"
    assert shop.ensure_calls == 1
    assert shop.run_calls == 0
    assert task.storage_calls == 0
    assert config.delays == [retry_delay()]


def test_new_server_month_invalidates_old_active_and_retry_state():
    config = FakeConfig(intent=True)
    data_logger_mark_active(config, server_now=datetime(2026, 8, 31, 23, 0))
    data_logger_set_retry(
        config,
        "unknown",
        server_now=datetime(2026, 8, 31, 23, 0),
    )

    assert data_logger_is_active(config, server_now=datetime(2026, 8, 31, 23, 59))
    assert data_logger_retry_pending(
        config,
        server_now=datetime(2026, 8, 31, 23, 59),
    )
    assert not data_logger_is_active(config, server_now=datetime(2026, 9, 1, 0, 0))
    assert not data_logger_retry_pending(
        config,
        server_now=datetime(2026, 9, 1, 0, 0),
    )


def test_storage_lifecycle_returns_absent_not_already_activated():
    class StorageHarness(OpsiVoucher):
        def __init__(self):
            self.quit_calls = 0

        def _data_logger_storage_enter(self):
            return True

        def _data_logger_storage_scan(self):
            return []

        def _data_logger_storage_quit(self):
            self.quit_calls += 1
            return True

    task = StorageHarness()

    assert task._data_logger_storage_lifecycle() is DataLoggerStorageState.ABSENT
    assert task.quit_calls == 1


def test_purchase_state_machine_has_finite_timeout(monkeypatch):
    from module.shop.shop_voucher import DATA_LOGGER_PURCHASE_SECONDS, VoucherShop

    class FakeTimer:
        def __init__(self):
            self.calls = 0

        def start(self):
            return self

        def reached(self):
            self.calls += 1
            return self.calls > 3

    timer = FakeTimer()
    monkeypatch.setattr(
        "module.shop.shop_voucher.Timer.from_seconds",
        lambda seconds: timer if seconds == DATA_LOGGER_PURCHASE_SECONDS else None,
    )

    screenshots = []
    shop = VoucherShop.__new__(VoucherShop)
    shop.device = SimpleNamespace(
        screenshot=lambda: screenshots.append("shot"),
        click=lambda _item: None,
    )
    shop.shop_interval_clear = lambda: None
    shop.appear = lambda *_args, **_kwargs: False
    shop.appear_then_click = lambda *_args, **_kwargs: False
    shop.shop_buy_handle = lambda _item: False
    shop.handle_retirement = lambda: False
    shop.shop_obstruct_handle = lambda: False
    shop.info_bar_count = lambda: 0

    result = shop.shop_buy_execute(
        SimpleNamespace(),
        skip_first_screenshot=False,
        timeout_seconds=DATA_LOGGER_PURCHASE_SECONDS,
    )

    assert result is False
    assert screenshots == ["shot", "shot", "shot"]


def test_purchase_timeout_returns_unknown_without_unbounded_rescan():
    from module.shop.shop_voucher import DATA_LOGGER_PURCHASE_SECONDS, VoucherShop

    item = SimpleNamespace(price=5000)

    class Harness(VoucherShop):
        def __init__(self):
            self.inspect_calls = 0

        def inspect_data_logger(self):
            self.inspect_calls += 1
            return DataLoggerShopState.AVAILABLE, item, "available"

        def shop_buy_execute(self, selected, timeout_seconds=None):
            assert selected is item
            assert timeout_seconds == DATA_LOGGER_PURCHASE_SECONDS
            return False

    shop = Harness()
    result = shop.ensure_data_logger()

    assert result.state is DataLoggerShopState.UNKNOWN
    assert result.reason == "purchase_timeout"
    assert result.purchased is True
    assert shop.inspect_calls == 1


def test_available_purchase_requires_post_purchase_sold_out_confirmation():
    from module.shop.shop_voucher import DATA_LOGGER_PURCHASE_SECONDS, VoucherShop

    item = SimpleNamespace(price=5000)
    calls = []

    class Harness(VoucherShop):
        def __init__(self):
            self.device = SimpleNamespace(screenshot=lambda: calls.append("screenshot"))
            self._states = iter(
                [
                    (DataLoggerShopState.AVAILABLE, item, "available"),
                    (DataLoggerShopState.SOLD_OUT, None, "dimmed_target"),
                ]
            )

        def inspect_data_logger(self):
            return next(self._states)

        def shop_buy_execute(self, selected, timeout_seconds=None):
            assert selected is item
            assert timeout_seconds == DATA_LOGGER_PURCHASE_SECONDS
            calls.append("buy")
            return True

    result = Harness().ensure_data_logger()

    assert result.state is DataLoggerShopState.SOLD_OUT
    assert result.purchased is True
    assert calls == ["buy", "screenshot"]


def test_purchase_without_sold_out_confirmation_remains_unknown():
    from module.shop.shop_voucher import VoucherShop

    item = SimpleNamespace(price=5000)

    class Harness(VoucherShop):
        def __init__(self):
            self.device = SimpleNamespace(screenshot=lambda: None)
            self._states = iter(
                [
                    (DataLoggerShopState.AVAILABLE, item, "available"),
                    (DataLoggerShopState.UNKNOWN, None, "target_unreadable"),
                ]
            )

        def inspect_data_logger(self):
            return next(self._states)

        def shop_buy_execute(self, selected, timeout_seconds=None):
            assert selected is item
            return True

    result = Harness().ensure_data_logger()

    assert result.state is DataLoggerShopState.UNKNOWN
    assert result.reason == "purchase_not_confirmed:target_unreadable"


def test_shop_page_recognizes_available_item_without_filter_dependency():
    from module.config.opsi_data_logger import DATA_LOGGER_ITEM_NAME
    from module.shop.shop_voucher import VoucherShop

    shop = VoucherShop.__new__(VoucherShop)
    item = SimpleNamespace(name=DATA_LOGGER_ITEM_NAME, price=5000)

    state, selected, reason = shop._data_logger_page_inspection([item])

    assert state is DataLoggerShopState.AVAILABLE
    assert selected is item
    assert reason == "recognized_with_positive_price"


def test_shop_page_treats_recognized_dimmed_target_as_sold_out():
    import numpy as np

    from module.config.opsi_data_logger import DATA_LOGGER_ITEM_NAME
    from module.shop.shop_voucher import VoucherShop

    shop = VoucherShop.__new__(VoucherShop)
    item = SimpleNamespace(
        name=DATA_LOGGER_ITEM_NAME,
        price=5000,
        image=np.zeros((8, 8, 3), dtype=np.uint8),
    )

    state, selected, reason = shop._data_logger_page_inspection([item])

    assert state is DataLoggerShopState.SOLD_OUT
    assert selected is None
    assert reason == "recognized_dimmed_target"


def test_scheduler_bridge_preserves_visible_intent_and_delays_inactive_cycle():
    config = scheduler_config(active=False)

    config.opsi_task_delay(recon_scan=True)

    assert deep_get(config.data, DATA_LOGGER_INTENT_PATH) is True
    assert "OpsiExplore.Scheduler.NextRun" in config.modified


def test_scheduler_bridge_uses_active_monthly_state():
    config = scheduler_config(active=True)

    config.opsi_task_delay(recon_scan=True)

    assert deep_get(config.data, DATA_LOGGER_INTENT_PATH) is True
    assert "OpsiExplore.Scheduler.NextRun" not in config.modified


def test_explore_uses_monthly_state_without_mutating_visible_intent():
    source = inspect.getsource(OpsiExplore._os_explore)

    assert "data_logger_is_active(self.config)" in source
    assert "OpsiExplore_SpecialRadar =" not in source


def test_special_storage_path_uses_only_unlock_template():
    source = inspect.getsource(OpsiVoucher._data_logger_storage_items)
    full_source = inspect.getsource(OpsiVoucher)

    assert "TEMPLATE_STORAGE_LOGGER_UNLOCK.match_multi" in source
    assert "storage_logger_use_all" not in full_source
    assert "self.logger_use()" not in full_source


def test_storage_entry_and_scroll_have_local_bounds():
    enter_source = inspect.getsource(OpsiVoucher._data_logger_storage_enter)
    scroll_source = inspect.getsource(OpsiVoucher._data_logger_storage_scroll_bottom)

    assert "Timer.from_seconds(DATA_LOGGER_STORAGE_ENTER_SECONDS)" in enter_source
    assert "MISSION_CHECK" in enter_source
    assert "MISSION_QUIT" in enter_source
    assert "Timer.from_seconds(6)" in scroll_source
    assert "drag_count < 4" in scroll_source


def test_runtime_localization_uses_canonical_name(monkeypatch):
    from module.webui import lang

    monkeypatch.setattr(lang, "list_mod_dir", lambda: [])
    monkeypatch.setattr(lang, "filepath_i18n", lambda *_args: "ru-RU.json")
    monkeypatch.setattr(
        lang,
        "read_file",
        lambda _path: {
            "OpsiExplore": {
                "_info": {"help": "legacy"},
                "SpecialRadar": {"name": "legacy", "help": "legacy"},
            }
        },
    )

    lang.reload()

    assert lang.dic_lang["OpsiExplore.SpecialRadar.name"] == (
        "Покупать и активировать Operation Siren Data Logger после сброса"
    )
    assert "Другие координатные логгеры не используются" in lang.dic_lang[
        "OpsiExplore.SpecialRadar.help"
    ]
