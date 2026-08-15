from __future__ import annotations

import types
import unittest
from unittest.mock import Mock, patch

from module.recovery.emulator_recovery import recover_emulator_transport


class EmulatorRecoveryTransportTests(unittest.TestCase):
    def make_platform(self, *, stop=True, force=True, start=True):
        platform = types.SimpleNamespace(
            emulator_instance=types.SimpleNamespace(name='MuMuPlayerGlobal-15.0-1'),
            emulator_stop=Mock(return_value=stop),
            emulator_force_stop_instance=Mock(return_value=force),
            emulator_start=Mock(return_value=start),
        )
        return platform

    def test_graceful_success_skips_hard_kill(self):
        platform = self.make_platform(stop=True, start=True)
        fresh = object()
        factory = Mock(return_value=fresh)

        outcome = recover_emulator_transport(
            object(),
            allow_hard_kill=True,
            platform=platform,
            device_factory=factory,
        )

        self.assertTrue(outcome.success)
        self.assertEqual('graceful', outcome.mode)
        self.assertEqual('transport-ready', outcome.stage)
        self.assertIs(fresh, outcome.device)
        platform.emulator_stop.assert_called_once_with()
        platform.emulator_force_stop_instance.assert_not_called()
        platform.emulator_start.assert_called_once_with()
        factory.assert_called_once()

    def test_default_platform_uses_isolated_recovery_factory_not_current_device_platform(self):
        platform = self.make_platform(stop=True, start=True)
        config = object()

        class CurrentDevice:
            release_during_wait = Mock()

            @property
            def platform(self):
                raise AssertionError('обычный current_device.platform не должен использоваться Stage 2')

        current_device = CurrentDevice()
        fresh = object()

        with patch('module.device.platform.get_recovery_platform', return_value=platform) as get_platform:
            outcome = recover_emulator_transport(
                config,
                current_device=current_device,
                allow_hard_kill=True,
                device_factory=Mock(return_value=fresh),
            )

        self.assertTrue(outcome.success)
        get_platform.assert_called_once_with(config)
        current_device.release_during_wait.assert_called_once_with()

    def test_current_device_resources_are_released_before_emulator_stop(self):
        order = []
        platform = self.make_platform(stop=True, start=True)
        platform.emulator_stop.side_effect = lambda: order.append('stop') or True
        platform.emulator_start.side_effect = lambda: order.append('start') or True
        current_device = types.SimpleNamespace(
            release_during_wait=Mock(side_effect=lambda: order.append('release')),
        )
        factory = Mock(side_effect=lambda **_: order.append('fresh-device') or object())

        outcome = recover_emulator_transport(
            object(),
            current_device=current_device,
            allow_hard_kill=True,
            platform=platform,
            device_factory=factory,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(['release', 'stop', 'start', 'fresh-device'], order)
        current_device.release_during_wait.assert_called_once_with()

    def test_still_alive_after_graceful_uses_hard_kill_before_start(self):
        order = []
        platform = self.make_platform(stop=False, force=True, start=True)
        platform.emulator_stop.side_effect = lambda: order.append('graceful') or False
        platform.emulator_force_stop_instance.side_effect = lambda: order.append('hard-kill') or True
        platform.emulator_start.side_effect = lambda: order.append('start') or True
        factory = Mock(side_effect=lambda **_: order.append('fresh-device') or object())

        outcome = recover_emulator_transport(
            object(),
            allow_hard_kill=True,
            platform=platform,
            device_factory=factory,
        )

        self.assertTrue(outcome.success)
        self.assertEqual('hard-kill', outcome.mode)
        self.assertEqual(['graceful', 'hard-kill', 'start', 'fresh-device'], order)

    def test_hard_kill_failure_blocks_cold_start(self):
        platform = self.make_platform(stop=False, force=False, start=True)
        factory = Mock(return_value=object())

        outcome = recover_emulator_transport(
            object(),
            allow_hard_kill=True,
            platform=platform,
            device_factory=factory,
        )

        self.assertFalse(outcome.success)
        self.assertEqual('hard-kill', outcome.stage)
        self.assertEqual('graceful', outcome.mode)
        platform.emulator_start.assert_not_called()
        factory.assert_not_called()

    def test_hard_kill_is_not_used_without_policy_opt_in(self):
        platform = self.make_platform(stop=False, force=True, start=True)

        outcome = recover_emulator_transport(
            object(),
            allow_hard_kill=False,
            platform=platform,
            device_factory=Mock(return_value=object()),
        )

        self.assertFalse(outcome.success)
        self.assertEqual('graceful-stop', outcome.stage)
        platform.emulator_force_stop_instance.assert_not_called()
        platform.emulator_start.assert_not_called()

    def test_start_false_is_failure_and_fresh_device_is_not_created(self):
        platform = self.make_platform(stop=True, start=False)
        factory = Mock(return_value=object())

        outcome = recover_emulator_transport(
            object(),
            allow_hard_kill=True,
            platform=platform,
            device_factory=factory,
        )

        self.assertFalse(outcome.success)
        self.assertEqual('cold-start', outcome.stage)
        factory.assert_not_called()

    def test_fresh_device_failure_is_not_false_success(self):
        platform = self.make_platform(stop=True, start=True)
        factory = Mock(side_effect=RuntimeError('fresh device failed'))

        outcome = recover_emulator_transport(
            object(),
            allow_hard_kill=True,
            platform=platform,
            device_factory=factory,
        )

        self.assertFalse(outcome.success)
        self.assertEqual('fresh-device', outcome.stage)


if __name__ == '__main__':
    unittest.main()
