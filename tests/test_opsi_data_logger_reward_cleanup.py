from __future__ import annotations

import inspect

from module.config.opsi_data_logger import DataLoggerStorageState
from module.os.tasks.voucher import OpsiVoucher


def test_activation_waits_for_reward_grace_before_disappearance_fallback():
    source = inspect.getsource(OpsiVoucher._data_logger_storage_activate_item)

    grace_check = source.index("reward_grace.reached()")
    fallback_message = source.index(
        "reward grace, and stable item disappearance"
    )
    fallback_return = source.index(
        "return DataLoggerStorageState.ACTIVATED",
        fallback_message,
    )

    assert "DATA_LOGGER_REWARD_GRACE_SECONDS" in source
    assert grace_check < fallback_message < fallback_return


def test_storage_quit_drains_events_and_rate_limits_back_button():
    source = inspect.getsource(OpsiVoucher._data_logger_storage_quit)

    assert "self.handle_map_event()" in source
    assert "self.appear_then_click(" in source
    assert "interval=2" in source
    assert "self.device.click(BACK_ARROW)" not in source


def test_confirmed_activation_survives_storage_cleanup_exception():
    class StorageHarness(OpsiVoucher):
        def _data_logger_storage_enter(self):
            return True

        def _data_logger_storage_scan(self):
            return [object()]

        def _data_logger_storage_activate_item(self):
            return DataLoggerStorageState.ACTIVATED

        def _data_logger_storage_quit(self):
            raise RuntimeError("reward popup blocked Storage exit")

    task = StorageHarness.__new__(StorageHarness)

    assert (
        task._data_logger_storage_lifecycle()
        is DataLoggerStorageState.ACTIVATED
    )
