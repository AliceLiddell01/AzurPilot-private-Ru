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
    emulator = types.SimpleNamespace(path=r'C:\MuMu\MuMuNxMain.exe')

    def __eq__(self, other):
        return other == FakeEmulator.MuMuPlayer12


class FakePlatformWindows:
    @property
    def emulator_instance(self):
        return self._instance


class EmulatorUnknown(Exception):
    pass


class MuMuInstanceIdentityError(RuntimeError):
    pass


def load_module():
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

    name = '_stage2_partial_start_harness'
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


class PlatformWindowsPartialStartTests(unittest.TestCase):
    def test_failed_manager_command_that_still_spawned_instance_is_stopped_before_retry(self):
        module = load_module()
        platform = module.RecoveryPlatformWindows.__new__(module.RecoveryPlatformWindows)
        platform._instance = FakeInstance()
        platform.MUMU_START_ATTEMPTS = 2

        module.is_mumu_instance_running = mock.Mock(side_effect=[False, True, False])
        platform._mumu_manager_command = mock.Mock(
            side_effect=[
                types.SimpleNamespace(returncode=9),
                types.SimpleNamespace(returncode=0),
            ]
        )
        platform.emulator_stop = mock.Mock(return_value=True)
        platform.emulator_start_watch = mock.Mock(return_value=True)

        self.assertTrue(platform.emulator_start())
        platform.emulator_stop.assert_called_once_with()
        self.assertEqual(2, platform._mumu_manager_command.call_count)
        platform.emulator_start_watch.assert_called_once_with()

    def test_partial_start_that_cannot_be_stopped_aborts_without_second_launch(self):
        module = load_module()
        platform = module.RecoveryPlatformWindows.__new__(module.RecoveryPlatformWindows)
        platform._instance = FakeInstance()
        platform.MUMU_START_ATTEMPTS = 3

        module.is_mumu_instance_running = mock.Mock(side_effect=[False, True])
        platform._mumu_manager_command = mock.Mock(
            return_value=types.SimpleNamespace(returncode=9)
        )
        platform.emulator_stop = mock.Mock(return_value=False)
        platform.emulator_start_watch = mock.Mock(return_value=True)

        self.assertFalse(platform.emulator_start())
        platform.emulator_stop.assert_called_once_with()
        platform._mumu_manager_command.assert_called_once_with(
            platform.emulator_instance,
            'launch_player',
        )
        platform.emulator_start_watch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
