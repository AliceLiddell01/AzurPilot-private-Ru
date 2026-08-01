from __future__ import annotations

import inspect
from datetime import datetime
from types import SimpleNamespace

import pytest

from module.config.deep import deep_get, deep_set
from module.config.opsi_data_logger import (
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
    data_logger_set_retry,
)
from module.os.tasks.explore import OpsiExplore
from module.os.tasks.voucher import (
    DATA_LOGGER_RETRY_MINUTES,
    OpsiVoucher,
)

NEXT_RESET = datetime(2026, 9, 1, 0, 0, 0)


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

    def task_delay(self, *, target=None, minute=None):
        self.delays.append({"target": target, "minute": minute})


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
def fixed_reset(monkeypatch):
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


def test_disabled_setting_runs_only_ordinary_shop():
    config = FakeConfig(intent=False)
    shop = FakeShop(DataLoggerShopResult(DataLoggerShopState.UNKNOWN))
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert shop.ensure_calls == 0
    assert shop.run_calls == 1
    assert task.storage_calls == 0
    assert config.delays == [{"target": NEXT_RESET, "minute": None}]


def test_valid_monthly_state_skips_shop_check_and_storage():
    config = FakeConfig(intent=True)
    data_logger_mark_active(config, next_reset=NEXT_RESET)
    shop = FakeShop(DataLoggerShopResult(DataLoggerShopState.UNKNOWN))
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert shop.ensure_calls == 0
    assert shop.run_calls == 1
    assert task.storage_calls == 0
    assert config.delays == [{"target": NEXT_RESET, "minute": None}]


@pytest.mark.parametrize(
    "storage_state",
    [
        DataLoggerStorageState.ACTIVATED,
        DataLoggerStorageState.ALREADY_ACTIVATED,
    ],
)
def test_sold_out_storage_success_persists_month(storage_state):
    config = FakeConfig(intent=True)
    shop = FakeShop(
        DataLoggerShopResult(
            DataLoggerShopState.SOLD_OUT,
            reason="confirmed_sold_out",
        )
    )
    task = VoucherHarness(config, shop, storage_state=storage_state)

    task.os_voucher()

    assert shop.ensure_calls == 1
    assert shop.run_calls == 1
    assert task.storage_calls == 1
    assert data_logger_is_active(config, next_reset=NEXT_RESET)
    assert storage(config)[DATA_LOGGER_VALID_UNTIL_KEY] == data_logger_cycle_key(NEXT_RESET)
    assert DATA_LOGGER_RETRY_PENDING_KEY not in storage(config)
    assert config.delays == [{"target": NEXT_RESET, "minute": None}]


@pytest.mark.parametrize(
    ("shop_result", "reason_prefix"),
    [
        (
            DataLoggerShopResult(
                DataLoggerShopState.UNKNOWN,
                reason="full_scan_inconclusive",
            ),
            "shop_unknown",
        ),
        (
            DataLoggerShopResult(
                DataLoggerShopState.AVAILABLE,
                reason="insufficient_currency",
            ),
            "shop_available",
        ),
    ],
)
def test_unknown_or_insufficient_currency_retries_without_false_success(
    shop_result,
    reason_prefix,
):
    config = FakeConfig(intent=True)
    shop = FakeShop(shop_result)
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert shop.ensure_calls == 1
    assert shop.run_calls == 1
    assert task.storage_calls == 0
    assert not data_logger_is_active(config, next_reset=NEXT_RESET)
    assert storage(config)[DATA_LOGGER_RETRY_PENDING_KEY] is True
    assert reason_prefix in storage(config)["OperationSirenDataLoggerRetryReason"]
    assert config.delays == [{"target": None, "minute": DATA_LOGGER_RETRY_MINUTES}]


def test_available_purchase_confirmation_activates_and_persists_month():
    config = FakeConfig(intent=True)
    shop = FakeShop(
        DataLoggerShopResult(
            DataLoggerShopState.SOLD_OUT,
            reason="confirmed_after_purchase",
            purchased=True,
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
    assert data_logger_is_active(config, next_reset=NEXT_RESET)
    assert config.delays == [{"target": NEXT_RESET, "minute": None}]


def test_storage_entry_timeout_retries_without_monthly_success():
    config = FakeConfig(intent=True)
    shop = FakeShop(DataLoggerShopResult(DataLoggerShopState.SOLD_OUT))
    task = VoucherHarness(
        config,
        shop,
        storage_state=DataLoggerStorageState.ENTER_TIMEOUT,
    )

    task.os_voucher()

    assert task.storage_calls == 1
    assert not data_logger_is_active(config, next_reset=NEXT_RESET)
    assert config.delays == [{"target": None, "minute": DATA_LOGGER_RETRY_MINUTES}]


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


def test_new_reset_invalidates_old_monthly_state():
    config = FakeConfig(intent=True)
    data_logger_mark_active(config, next_reset=NEXT_RESET)

    assert data_logger_is_active(config, next_reset=NEXT_RESET)
    assert not data_logger_is_active(
        config,
        next_reset=datetime(2026, 10, 1, 0, 0, 0),
    )


def test_special_storage_path_uses_only_unlock_template():
    source = inspect.getsource(OpsiVoucher._data_logger_storage_items)
    full_source = inspect.getsource(OpsiVoucher)

    assert "TEMPLATE_STORAGE_LOGGER_UNLOCK.match_multi" in source
    assert "storage_logger_use_all" not in full_source
    assert "self.logger_use()" not in full_source


def test_explore_uses_monthly_state_without_mutating_visible_intent():
    source = inspect.getsource(OpsiExplore._os_explore)

    assert "data_logger_is_active(self.config)" in source
    assert "OpsiExplore_SpecialRadar =" not in source


def test_scheduler_bridge_restores_visible_intent(monkeypatch):
    from module.config.config import AzurLaneConfig

    config = AzurLaneConfig.__new__(AzurLaneConfig)
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
    config.modified = {}
    monkeypatch.setattr(config, "update", lambda: None)

    config.opsi_task_delay(recon_scan=True)

    assert deep_get(config.data, DATA_LOGGER_INTENT_PATH) is True
    assert "OpsiExplore.Scheduler.NextRun" in config.modified


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


def test_shop_page_recognizes_confirmed_dimmed_target(monkeypatch):
    import numpy as np

    from module.config.opsi_data_logger import DATA_LOGGER_ITEM_NAME
    from module.shop.shop_voucher import VoucherShop

    image = np.arange(27, dtype=np.uint8).reshape(3, 3, 3)
    grid = SimpleNamespace(
        templates={DATA_LOGGER_ITEM_NAME: image},
        grids=SimpleNamespace(buttons=[object()]),
        item_class=lambda _screen, _button: SimpleNamespace(image=image),
    )
    shop = VoucherShop.__new__(VoucherShop)
    shop.device = SimpleNamespace(image=image)
    monkeypatch.setattr(shop, "shop_items", lambda: grid)
    monkeypatch.setattr(
        "module.shop.shop_voucher.cv2.matchTemplate",
        lambda *_args, **_kwargs: np.array([[1.0]], dtype=np.float32),
    )

    state, selected, reason = shop._data_logger_page_inspection([])

    assert state is DataLoggerShopState.SOLD_OUT
    assert selected is None
    assert reason.startswith("dimmed_target:")


def test_shop_page_keeps_ocr_miss_unknown_when_target_is_not_dimmed(monkeypatch):
    import numpy as np

    from module.config.opsi_data_logger import DATA_LOGGER_ITEM_NAME
    from module.shop.shop_voucher import VoucherShop

    image = np.full((3, 3, 3), 220, dtype=np.uint8)
    image[0, 0] = (180, 200, 240)
    grid = SimpleNamespace(
        templates={DATA_LOGGER_ITEM_NAME: image},
        grids=SimpleNamespace(buttons=[object()]),
        item_class=lambda _screen, _button: SimpleNamespace(image=image),
    )
    shop = VoucherShop.__new__(VoucherShop)
    shop.device = SimpleNamespace(image=image)
    monkeypatch.setattr(shop, "shop_items", lambda: grid)
    monkeypatch.setattr(
        "module.shop.shop_voucher.cv2.matchTemplate",
        lambda *_args, **_kwargs: np.array([[1.0]], dtype=np.float32),
    )

    state, selected, reason = shop._data_logger_page_inspection([])

    assert state is DataLoggerShopState.UNKNOWN
    assert selected is None
    assert reason.startswith("target_unreadable:")


def test_available_purchase_requires_post_purchase_sold_out_confirmation():
    from module.shop.shop_voucher import VoucherShop

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

        def shop_currency(self):
            self._currency = 5000
            return self._currency

        def shop_buy_execute(self, selected):
            assert selected is item
            calls.append("buy")

    result = Harness().ensure_data_logger()

    assert result.state is DataLoggerShopState.SOLD_OUT
    assert result.purchased is True
    assert calls == ["buy", "screenshot"]


def test_purchase_click_without_sold_out_confirmation_is_unknown():
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

        def shop_currency(self):
            self._currency = 5000
            return self._currency

        def shop_buy_execute(self, selected):
            assert selected is item

    result = Harness().ensure_data_logger()

    assert result.state is DataLoggerShopState.UNKNOWN
    assert result.purchased is True
    assert result.reason == "purchase_not_confirmed:target_unreadable"


def test_scheduler_bridge_uses_active_monthly_state(monkeypatch):
    from module.config.config import AzurLaneConfig

    config = AzurLaneConfig.__new__(AzurLaneConfig)
    config.data = {
        "OpsiExplore": {
            "OpsiExplore": {
                "SpecialRadar": True,
                "ForceRun": False,
            },
            "Scheduler": {"NextRun": datetime(2026, 8, 1)},
            "Storage": {
                "Storage": {
                    DATA_LOGGER_VALID_UNTIL_KEY: data_logger_cycle_key(NEXT_RESET),
                }
            },
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
    config.modified = {}
    monkeypatch.setattr(config, "update", lambda: None)

    config.opsi_task_delay(recon_scan=True)

    assert deep_get(config.data, DATA_LOGGER_INTENT_PATH) is True
    assert "OpsiExplore.Scheduler.NextRun" not in config.modified


def test_runtime_localization_uses_canonical_name(monkeypatch):
    from module.webui import lang

    monkeypatch.setattr(lang, "list_mod_dir", lambda: [])
    monkeypatch.setattr(lang, "filepath_i18n", lambda *_args: "ru-RU.json")
    monkeypatch.setattr(
        lang,
        "read_file",
        lambda _path: {
            "OpsiDaily": {
                "_info": {
                    "help": "Use Campaign Information Recorder.",
                }
            },
            "OpsiExplore": {
                "SpecialRadar": {
                    "name": "Campaign Information Recorder purchased",
                    "help": "legacy",
                }
            },
        },
    )

    lang.reload()

    assert "Campaign Information Recorder" not in lang.dic_lang[
        "OpsiDaily._info.help"
    ]
    assert lang.dic_lang["OpsiExplore.SpecialRadar.name"] == (
        "Покупать и активировать Operation Siren Data Logger после сброса"
    )
    assert "Другие координатные логгеры не используются" in lang.dic_lang[
        "OpsiExplore.SpecialRadar.help"
    ]


def test_storage_entry_is_bounded_and_guards_overview_misclick():
    source = inspect.getsource(OpsiVoucher._data_logger_storage_enter)

    assert "Timer.from_seconds(DATA_LOGGER_STORAGE_ENTER_SECONDS)" in source
    assert "for _ in self.loop()" not in source
    assert "MISSION_CHECK" in source
    assert "MISSION_QUIT" in source
    assert "offset=(20, 20)" in source


def test_storage_requires_confirmed_allied_port_local_map():
    source = inspect.getsource(OpsiVoucher._data_logger_ensure_port_map)

    assert "self.zone_init()" in source
    assert "self.zone.is_azur_port" in source
    assert "self.zone_nearest_azur_port(self.zone)" in source
    assert "self.globe_goto(target)" in source
    assert "stable_frames >= 3" in source


def test_storage_scroll_is_locally_bounded():
    source = inspect.getsource(OpsiVoucher._data_logger_storage_scroll_bottom)

    assert "Timer.from_seconds(6)" in source
    assert "drag_count < 4" in source
    assert "SCROLL_STORAGE.set_bottom" not in source


def test_retry_marker_expires_with_monthly_cycle():
    config = FakeConfig(intent=True)
    data_logger_set_retry(config, "unknown")

    from module.config.opsi_data_logger import data_logger_retry_pending

    assert data_logger_retry_pending(config, next_reset=NEXT_RESET)
    assert not data_logger_retry_pending(
        config,
        next_reset=datetime(2026, 10, 1, 0, 0, 0),
    )
