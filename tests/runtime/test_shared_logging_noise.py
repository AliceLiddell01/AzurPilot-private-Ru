import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import module.device.control as control_module
from module.device.control import Control
from module.logger import logger
from module.ui.scroll import Scroll
from module.ui.switch import Switch


class _DummyButton:
    def __init__(self, name="BUTTON", button=(0, 0, 20, 20)):
        self.name = name
        self.button = button

    def __str__(self):
        return self.name


class _ControlProbe(Control):
    def __init__(self, method="ADB"):
        self.config = SimpleNamespace(Emulator_ControlMethod=method)
        self.control_checks = []
        self.click_calls = []
        self.swipe_calls = []

    @property
    def click_methods(self):
        return {"ADB": self._record_click}

    def _record_click(self, x, y):
        self.click_calls.append((x, y))

    def handle_control_check(self, button):
        self.control_checks.append(button)

    def swipe_adb(self, p1, p2, duration):
        self.swipe_calls.append((p1, p2, duration))


class _SequenceTimer:
    def __init__(self, reached_values=()):
        self.reached_values = list(reached_values)
        self.reset_count = 0
        self.clear_count = 0

    def reset(self):
        self.reset_count += 1
        return self

    def clear(self):
        self.clear_count += 1
        return self

    def reached(self):
        if self.reached_values:
            return self.reached_values.pop(0)
        return False


class _DeviceProbe:
    def __init__(self):
        self.clicks = []
        self.screenshots = 0
        self.swipes = []

    def click(self, button):
        self.clicks.append(button)

    def screenshot(self):
        self.screenshots += 1

    def swipe(self, p1, p2, **kwargs):
        self.swipes.append((p1, p2, kwargs))


class _MainProbe:
    def __init__(self, appear_results=()):
        self.device = _DeviceProbe()
        self._appear_results = iter(appear_results)

    def appear(self, button, offset=0, similarity=0.85):
        return next(self._appear_results, False)


class TestDeviceControlLogging(unittest.TestCase):
    def setUp(self):
        logger.reset_diagnostic_context()

    def tearDown(self):
        logger.reset_diagnostic_context()

    def test_click_keeps_dispatch_but_moves_raw_coordinates_to_debug(self):
        control = _ControlProbe()
        button = _DummyButton()
        with (
            patch.object(control_module, "random_rectangle_point", return_value=(7, 9)),
            patch.object(logger, "info") as info,
        ):
            control.click(button)

        self.assertEqual([(7, 9)], control.click_calls)
        self.assertEqual([button], control.control_checks)
        info.assert_not_called()
        context = logger.get_diagnostic_context()
        self.assertTrue(any("Нажатие" in message and "(   7,    9)" in message for message in context))

    def test_swipe_keeps_adb_duration_and_rejects_short_swipe_without_dispatch(self):
        control = _ControlProbe()
        with patch.object(logger, "info") as info:
            control.swipe((10, 10), (110, 10), duration=0.2)
            self.assertEqual([([10, 10], [110, 10], 0.5)], control.swipe_calls)

            control.swipe_calls.clear()
            control.swipe((10, 10), (15, 10), duration=0.2)
            self.assertEqual([], control.swipe_calls)
        info.assert_not_called()

    def test_unsupported_drag_fallback_warning_is_preserved(self):
        control = _ControlProbe(method="ADB")
        with (
            patch.object(control_module, "random_rectangle_point", return_value=(50, 60)),
            patch.object(logger, "warning") as warning,
        ):
            control.drag((10, 10), (100, 100), point_random=(0, 0, 0, 0), swipe_duration=0.25)

        warning.assert_called_once()
        self.assertIn("не поддерживает перетаскивание", warning.call_args.args[0])
        self.assertEqual([([10, 10], [100, 100], 0.5)], control.swipe_calls)
        self.assertEqual([(50, 60)], control.click_calls)


class TestScrollLogging(unittest.TestCase):
    def test_cal_position_math_is_unchanged_and_telemetry_is_debug(self):
        scroll = Scroll((0, 0, 10, 100), color=(255, 255, 255), name="TEST_SCROLL")
        mask = np.zeros(100, dtype=np.bool_)
        mask[20:40] = True

        def match_color(_main):
            scroll.length = int(mask.sum())
            return mask

        with (
            patch.object(scroll, "match_color", side_effect=match_color),
            patch.object(logger, "debug") as debug,
            patch.object(logger, "info") as info,
        ):
            position = scroll.cal_position(object())

        self.assertAlmostEqual(0.24375, position)
        debug.assert_called_once()
        self.assertIn("[TEST_SCROLL]", debug.call_args.args[0])
        info.assert_not_called()

    def test_set_immediate_target_does_not_add_swipes_or_screenshots(self):
        scroll = Scroll((0, 0, 10, 100), color=(255, 255, 255), name="TEST_SCROLL")
        main = _MainProbe()
        with patch.object(scroll, "cal_position", return_value=0.5):
            dragged = scroll.set(0.5, main=main)

        self.assertEqual(0, dragged)
        self.assertEqual(0, main.device.screenshots)
        self.assertEqual([], main.device.swipes)

    def test_disappeared_scrollbar_warning_is_preserved_without_swipe(self):
        scroll = Scroll((0, 0, 10, 100), color=(255, 255, 255), name="TEST_SCROLL")
        scroll.drag_timeout = _SequenceTimer([True])
        main = _MainProbe()

        def missing_position(_main):
            scroll.length = 0
            return 0.0

        with (
            patch.object(scroll, "cal_position", side_effect=missing_position),
            patch.object(logger, "warning") as warning,
        ):
            dragged = scroll.set(0.5, main=main)

        self.assertEqual(0, dragged)
        warning.assert_called_once_with('[UI] Полоса прокрутки исчезла; считаем, что позиция установлена')
        self.assertEqual([], main.device.swipes)


class TestSwitchLogging(unittest.TestCase):
    def setUp(self):
        logger.reset_suppression()

    def tearDown(self):
        logger.reset_suppression()

    @staticmethod
    def _switch():
        switch = Switch("TEST_SWITCH")
        switch.add_state("on", check_button="ON")
        switch.add_state("off", check_button="OFF")
        switch.set_unknown_timer = _SequenceTimer()
        switch.set_click_timer = _SequenceTimer([True])
        return switch

    def test_set_immediate_target_keeps_click_and_screenshot_counts_zero(self):
        switch = self._switch()
        main = _MainProbe([True])
        with patch.object(logger, "log") as log:
            changed = switch.set("on", main=main)

        self.assertFalse(changed)
        self.assertEqual([], main.device.clicks)
        self.assertEqual(0, main.device.screenshots)
        self.assertEqual(1, log.call_count)
        self.assertEqual(logging.DEBUG, log.call_args.args[0])

    def test_set_known_state_transition_keeps_one_click_and_one_new_screenshot(self):
        switch = self._switch()
        main = _MainProbe([False, True, True])
        with patch.object(logger, "log") as log:
            changed = switch.set("on", main=main)

        self.assertTrue(changed)
        self.assertEqual(["OFF"], main.device.clicks)
        self.assertEqual(1, main.device.screenshots)
        self.assertEqual(2, log.call_count)
        self.assertTrue(all(call.args[0] == logging.DEBUG for call in log.call_args_list))

    def test_wait_repeated_unknown_is_suppressed_and_timeout_warning_survives(self):
        switch = self._switch()
        switch.wait_timeout = _SequenceTimer([False, False, True])
        main = _MainProbe([False, False, False, False, False, False])

        with (
            patch.object(logger, "log") as log,
            patch.object(logger, "warning") as warning,
        ):
            result = switch.wait(main=main)

        self.assertFalse(result)
        self.assertEqual(2, main.device.screenshots)
        warning.assert_called_once_with('TEST_SWITCH: превышено время ожидания активации')
        self.assertEqual(2, log.call_count)
        self.assertEqual(logging.DEBUG, log.call_args_list[0].args[0])
        self.assertEqual(logging.DEBUG, log.call_args_list[1].args[0])
        self.assertIn("повторено 2 раз", log.call_args_list[1].args[1])

    def test_wait_unknown_to_known_preserves_state_change_without_warning(self):
        switch = self._switch()
        switch.wait_timeout = _SequenceTimer([False])
        main = _MainProbe([False, False, True])

        with (
            patch.object(logger, "log") as log,
            patch.object(logger, "warning") as warning,
        ):
            result = switch.wait(main=main)

        self.assertTrue(result)
        self.assertEqual(1, main.device.screenshots)
        warning.assert_not_called()
        self.assertEqual(2, log.call_count)
        messages = [call.args[1] for call in log.call_args_list]
        self.assertIn("состояние unknown", messages[0])
        self.assertIn("состояние on", messages[1])


if __name__ == "__main__":
    unittest.main()
