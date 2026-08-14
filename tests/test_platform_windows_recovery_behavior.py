from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'module/device/platform/platform_windows_recovery.py'


class FakeLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeEmulator:
    MuMuPlayer12 = 'MuMuPlayer12'

    @staticmethod
    def single_to_console(path):
        return path.replace('MuMuNxMain.exe', 'MuMuManager.exe')


class FakeInstance:
    name = 'MuMuPlayerGlobal-15.0-1'
    MuMuPlayer12_id = 1
    emulator = types.SimpleNamespace(path=r'C:\Program Files\Netease\MuMuPlayer\nx_main\MuMuNxMain.exe')

    def __eq__(self, other):
        return other == FakeEmulator.MuMuPlayer12


class FakePlatformWindows:
    @property
    def emulator_instance(self):
        return self._instance

    def emulator_stop(self):
        return True

    def emulator_start(self):
        return True


class EmulatorUnknown(Exception):
    pass


class MuMuInstanceIdentityError(RuntimeError):
    pass


def load_recovery_module():
    emulator_windows = types.ModuleType('module.device.platform.emulator_windows')
    emulator_windows.Emulator = FakeEmulator
    emulator_windows.EmulatorInstance = FakeInstance

    process_control = types.ModuleType('module.device.platform.mumu_process_control')
    process_control.MuMuInstanceIdentityError = MuMuInstanceIdentityError
    process_control.force_stop_mumu_instance = mock.Mock(return_value=True)
    process_control.is_mumu_instance_running = mock.Mock(return_value=False)
    process_control.wait_mumu_instance_stopped = mock.Mock(return_value=True)

    platform_windows = types.ModuleType('module.device.platform.platform_windows')
    platform_windows.EmulatorUnknown = EmulatorUnknown
    platform_windows.PlatformWindows = FakePlatformWindows

    logger_module = types.ModuleType('module.logger')
    logger_module.logger = FakeLogger()

    name = '_stage2_platform_windows_recovery_harness'
    spec = importlib.util.spec_from_file_location(name, SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Не удалось загрузить {SOURCE}')
    module = importlib.util.module_from_spec(spec)

    with mock.patch.dict(
        sys.modules,
        {
            'module.device.platform.emulator_windows': emulator_windows,
            'module.device.platform.mumu_process_control': process_control,
            'module.device.platform.platform_windows': platform_windows,
            'module.logger': logger_module,
        },
    ):
        spec.loader.exec_module(module)
    return module


class PlatformWindowsRecoveryBehaviorTests(unittest.TestCase):
    def make_platform(self):
        module = load_recovery_module()
        platform = module.RecoveryPlatformWindows.__new__(module.RecoveryPlatformWindows)
        platform._instance = FakeInstance()
        return module, platform

    def test_shutdown_return_zero_but_instance_alive_is_failure(self):
        module, platform = self.make_platform()
        platform._mumu_manager_command = mock.Mock(
            return_value=types.SimpleNamespace(returncode=0)
        )
        module.wait_mumu_instance_stopped = mock.Mock(return_value=False)

        self.assertFalse(platform.emulator_stop())
        module.wait_mumu_instance_stopped.assert_called_once_with(
            platform.emulator_instance,
            timeout=platform.MUMU_STOP_VERIFY_TIMEOUT,
            interval=platform.MUMU_STOP_VERIFY_INTERVAL,
        )

    def test_shutdown_nonzero_but_instance_dead_is_success(self):
        module, platform = self.make_platform()
        platform._mumu_manager_command = mock.Mock(
            return_value=types.SimpleNamespace(returncode=7)
        )
        module.wait_mumu_instance_stopped = mock.Mock(return_value=True)

        self.assertTrue(platform.emulator_stop())

    def test_shutdown_timeout_but_instance_dead_is_success(self):
        module, platform = self.make_platform()
        platform._mumu_manager_command = mock.Mock(return_value=None)
        module.wait_mumu_instance_stopped = mock.Mock(return_value=True)

        self.assertTrue(platform.emulator_stop())

    def test_launch_manager_failure_never_reaches_start_watch(self):
        module, platform = self.make_platform()
        platform.MUMU_START_ATTEMPTS = 1
        module.is_mumu_instance_running = mock.Mock(return_value=False)
        platform._mumu_manager_command = mock.Mock(
            return_value=types.SimpleNamespace(returncode=9)
        )
        platform.emulator_start_watch = mock.Mock(return_value=True)

        self.assertFalse(platform.emulator_start())
        platform.emulator_start_watch.assert_not_called()

    def test_start_watch_failure_is_overall_failure(self):
        module, platform = self.make_platform()
        platform.MUMU_START_ATTEMPTS = 1
        module.is_mumu_instance_running = mock.Mock(return_value=False)
        platform._mumu_manager_command = mock.Mock(
            return_value=types.SimpleNamespace(returncode=0)
        )
        platform.emulator_start_watch = mock.Mock(return_value=False)
        platform.emulator_stop = mock.Mock(return_value=True)

        self.assertFalse(platform.emulator_start())
        platform.emulator_start_watch.assert_called_once_with()
        platform.emulator_stop.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
