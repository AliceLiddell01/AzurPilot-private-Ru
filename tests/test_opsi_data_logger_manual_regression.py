from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from module.config.deep import deep_get, deep_set
from module.config.opsi_data_logger import (
    DATA_LOGGER_CYCLE_KEY,
    DATA_LOGGER_RETRY_PENDING_KEY,
    DATA_LOGGER_STORAGE_PATH,
    DataLoggerShopResult,
    DataLoggerShopState,
    DataLoggerStorageState,
    data_logger_is_active,
    data_logger_set_retry,
)
from module.os.tasks.voucher import OpsiVoucher

NEXT_RESET = datetime(2026, 9, 1, 7, 0)
FIXED_UTC_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class FakeConfig:
    def __init__(self):
        self.data = {
            'OpsiExplore': {
                'OpsiExplore': {'SpecialRadar': True},
                'Storage': {'Storage': {}},
            }
        }
        self.delays = []

    def cross_get(self, keys, default=None):
        return deep_get(self.data, keys=keys, default=default)

    def cross_set(self, keys, value):
        deep_set(self.data, keys=keys, value=value)

    def task_delay(self, *, target=None, minute=None, server_update=False):
        self.delays.append(
            {
                'target': target,
                'minute': minute,
                'server_update': server_update,
            }
        )


class FakeShop:
    def __init__(self, result):
        self.result = result
        self.ensure_calls = 0
        self.run_calls = 0

    def ensure_data_logger(self):
        self.ensure_calls += 1
        return self.result

    def run(self):
        self.run_calls += 1


class VoucherHarness(OpsiVoucher):
    def __init__(self, config, shop, storage_state):
        self.config = config
        self.device = SimpleNamespace()
        self.shop = shop
        self.storage_state = storage_state
        self.storage_calls = 0

    def _create_voucher_shop(self):
        return self.shop

    def _os_voucher_enter(self):
        return None

    def _os_voucher_exit(self):
        return None

    def _data_logger_storage_lifecycle(self):
        self.storage_calls += 1
        return self.storage_state


@pytest.fixture(autouse=True)
def fixed_server_time(monkeypatch):
    monkeypatch.setattr(
        'module.config.opsi_data_logger.current_time',
        lambda _tz=None: FIXED_UTC_NOW,
    )
    monkeypatch.setattr(
        'module.config.opsi_data_logger.server_timezone',
        lambda: timedelta(hours=8),
    )
    monkeypatch.setattr(
        'module.config.opsi_data_logger.get_os_next_reset',
        lambda: NEXT_RESET,
    )
    monkeypatch.setattr(
        'module.os.tasks.voucher.get_os_next_reset',
        lambda: NEXT_RESET,
    )


def storage(config):
    return deep_get(config.data, keys=DATA_LOGGER_STORAGE_PATH, default={})


def test_confirmed_purchase_with_inconclusive_rescan_continues_to_storage():
    config = FakeConfig()
    shop = FakeShop(
        DataLoggerShopResult(
            state=DataLoggerShopState.UNKNOWN,
            reason='purchase_not_confirmed:full_scan_inconclusive',
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
    assert data_logger_is_active(config)
    assert storage(config)[DATA_LOGGER_CYCLE_KEY] == '2026-08'
    assert DATA_LOGGER_RETRY_PENDING_KEY not in storage(config)
    assert config.delays == [
        {
            'target': NEXT_RESET,
            'minute': None,
            'server_update': False,
        }
    ]


def test_existing_full_scan_retry_resumes_with_storage_probe():
    config = FakeConfig()
    data_logger_set_retry(
        config,
        reason='shop_unknown:full_scan_inconclusive',
    )
    shop = FakeShop(
        DataLoggerShopResult(
            state=DataLoggerShopState.UNKNOWN,
            reason='full_scan_inconclusive',
        )
    )
    task = VoucherHarness(
        config,
        shop,
        storage_state=DataLoggerStorageState.ACTIVATED,
    )

    task.os_voucher()

    assert shop.ensure_calls == 1
    assert shop.run_calls == 0
    assert task.storage_calls == 1
    assert data_logger_is_active(config)
    assert storage(config)[DATA_LOGGER_CYCLE_KEY] == '2026-08'
    assert DATA_LOGGER_RETRY_PENDING_KEY not in storage(config)


def test_unrelated_retry_unknown_does_not_probe_storage():
    config = FakeConfig()
    data_logger_set_retry(config, reason='shop_unknown:exception')
    shop = FakeShop(
        DataLoggerShopResult(
            state=DataLoggerShopState.UNKNOWN,
            reason='exception:RuntimeError',
        )
    )
    task = VoucherHarness(
        config,
        shop,
        storage_state=DataLoggerStorageState.ACTIVATED,
    )

    task.os_voucher()

    assert shop.run_calls == 0
    assert task.storage_calls == 0
    assert not data_logger_is_active(config)
    assert storage(config)[DATA_LOGGER_RETRY_PENDING_KEY] is True
