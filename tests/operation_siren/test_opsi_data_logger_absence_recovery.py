from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from module.config.deep import deep_get, deep_set
from module.config.opsi_data_logger import (
    DATA_LOGGER_CYCLE_KEY,
    DATA_LOGGER_RETRY_PENDING_KEY,
    DATA_LOGGER_STORAGE_PATH,
    DataLoggerLifecycleEvidence,
    DataLoggerShopResult,
    DataLoggerShopState,
    DataLoggerStorageState,
    data_logger_is_active,
    data_logger_mark_evidence,
    data_logger_retry_count,
    data_logger_set_retry,
)
from module.os.tasks.voucher import DATA_LOGGER_RETRY_MINUTES, OpsiVoucher

FIXED_UTC_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
NEXT_RESET = datetime(2026, 9, 1, 7, 0)


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
    def __init__(self, config, shop):
        self.config = config
        self.device = SimpleNamespace()
        self.shop = shop
        self.storage_calls = 0

    def _create_voucher_shop(self):
        return self.shop

    def _os_voucher_enter(self):
        return None

    def _os_voucher_exit(self):
        return None

    def _data_logger_storage_lifecycle(self):
        self.storage_calls += 1
        return DataLoggerStorageState.ABSENT


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


def retry_delay():
    return {
        'target': None,
        'minute': DATA_LOGGER_RETRY_MINUTES,
        'server_update': True,
    }


def full_scan_unknown():
    return DataLoggerShopResult(
        state=DataLoggerShopState.UNKNOWN,
        reason='full_scan_inconclusive',
    )


def test_fresh_full_shop_absence_and_storage_absence_remain_unverified():
    config = FakeConfig()
    shop = FakeShop(full_scan_unknown())
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert shop.ensure_calls == 1
    assert shop.run_calls == 1
    assert task.storage_calls == 1
    assert not data_logger_is_active(config)
    assert DATA_LOGGER_CYCLE_KEY not in storage(config)
    assert storage(config)[DATA_LOGGER_RETRY_PENDING_KEY] is True
    assert data_logger_retry_count(config) == 1
    assert config.delays == [retry_delay()]


def test_retry_full_shop_absence_without_evidence_remains_unverified():
    config = FakeConfig()
    data_logger_set_retry(
        config,
        reason='shop_unknown:full_scan_inconclusive',
    )
    shop = FakeShop(full_scan_unknown())
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert shop.ensure_calls == 1
    assert shop.run_calls == 0
    assert task.storage_calls == 1
    assert not data_logger_is_active(config)
    assert data_logger_retry_count(config) == 2
    assert config.delays == [retry_delay()]


def test_persisted_exact_item_evidence_allows_absence_recovery():
    config = FakeConfig()
    data_logger_mark_evidence(
        config,
        DataLoggerLifecycleEvidence.STORAGE_OBSERVED,
    )
    data_logger_set_retry(
        config,
        reason='storage_unknown',
    )
    shop = FakeShop(full_scan_unknown())
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert shop.ensure_calls == 1
    assert shop.run_calls == 0
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


def test_purchase_timeout_and_storage_absence_do_not_confirm_activation():
    config = FakeConfig()
    shop = FakeShop(
        DataLoggerShopResult(
            state=DataLoggerShopState.UNKNOWN,
            reason='purchase_timeout',
            purchased=True,
        )
    )
    task = VoucherHarness(config, shop)

    task.os_voucher()

    assert task.storage_calls == 1
    assert not data_logger_is_active(config)
    assert data_logger_retry_count(config) == 1
    assert config.delays == [retry_delay()]


def test_single_page_target_miss_probes_but_does_not_confirm_full_absence():
    result = DataLoggerShopResult(
        state=DataLoggerShopState.UNKNOWN,
        reason='target_not_observed',
    )

    assert OpsiVoucher._data_logger_should_probe_storage(result, False)
    assert not OpsiVoucher._data_logger_full_absence_confirms_activation(
        result,
        DataLoggerStorageState.ABSENT,
        DataLoggerLifecycleEvidence.STORAGE_OBSERVED,
    )


def test_unrelated_shop_failure_does_not_probe_or_confirm_absence():
    result = DataLoggerShopResult(
        state=DataLoggerShopState.UNKNOWN,
        reason='exception:RuntimeError',
    )

    assert not OpsiVoucher._data_logger_should_probe_storage(result, True)
    assert not OpsiVoucher._data_logger_full_absence_confirms_activation(
        result,
        DataLoggerStorageState.ABSENT,
        DataLoggerLifecycleEvidence.USE_CLICKED,
    )
