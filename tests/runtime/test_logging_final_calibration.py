import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from module.device.app_control import AppControl
from module.exception import MapDetectionError
from module.map.camera import Camera, _MAP_OUTSIDE_WARNING_KEY
from module.ocr.ocr import DigitCounter, Ocr


class TestFinalLoggingCalibration(unittest.TestCase):
    @staticmethod
    def _camera(*, in_map):
        camera = Camera.__new__(Camera)
        camera.view = SimpleNamespace(load=Mock(), center_offset=np.array([0.5, 0.5]))
        camera.device = SimpleNamespace(image=object(), app_is_running=Mock(return_value=True))
        camera.config = SimpleNamespace(task=SimpleNamespace(command="Event"))
        camera.is_in_map = Mock(return_value=in_map)
        camera.is_in_strategy_submarine_move = Mock(return_value=False)
        camera.is_in_strategy_mob_move = Mock(return_value=False)
        camera.is_in_strategy_air_strike = Mock(return_value=False)
        camera.info_bar_count = Mock(return_value=0)
        camera.appear = Mock(return_value=False)
        camera.handle_story_skip = Mock(return_value=False)
        camera.is_in_stage = Mock(return_value=False)
        camera._auto_search_menu_offset = (0, 0, 0, 0)
        return camera

    def test_map_swipe_keeps_result_but_moves_telemetry_to_debug(self):
        camera = self._camera(in_map=True)
        camera._map_swipe = Mock(return_value=True)

        with (
            patch("module.map.camera.logger.debug") as debug,
            patch("module.map.camera.logger.info") as info,
        ):
            result = camera.map_swipe((0, 0))

        self.assertTrue(result)
        debug.assert_called_once_with('[Карта — камера] Сдвиг карты: (0, 0)')
        info.assert_not_called()
        camera._map_swipe.assert_called_once()

    def test_outside_map_warning_uses_existing_bounded_suppressor(self):
        camera = self._camera(in_map=False)
        message = '[Карта — камера] Проверяемое изображение не относится к карте'

        with (
            patch("module.map.camera.logger.log_suppressed", return_value=True) as suppressed,
            patch("module.map.camera.logger.finish_suppressed") as finish,
        ):
            with self.assertRaisesRegex(MapDetectionError, "in_map"):
                camera._update_view()

        suppressed.assert_called_once_with(
            logging.WARNING,
            message,
            key=_MAP_OUTSIDE_WARNING_KEY,
            payload=message,
        )
        finish.assert_not_called()

    def test_valid_map_closes_outside_map_suppression_series(self):
        camera = self._camera(in_map=True)

        with patch("module.map.camera.logger.finish_suppressed") as finish:
            self.assertTrue(camera._update_view())

        finish.assert_called_once_with(_MAP_OUTSIDE_WARNING_KEY)
        camera.view.load.assert_called_once_with(camera.device.image)

    def test_digit_counter_invalid_result_uses_existing_bounded_suppressor(self):
        counter = DigitCounter((0, 0, 1, 1), name="TEST_COUNTER")
        key = ("ocr-counter-invalid", "DigitCounter", "TEST_COUNTER")

        with (
            patch.object(Ocr, "ocr", return_value="10"),
            patch("module.ocr.ocr.logger.log_suppressed", return_value=True) as suppressed,
            patch("module.ocr.ocr.logger.finish_suppressed") as finish,
        ):
            result = counter.ocr(object())

        self.assertEqual((0, 0, 0), result)
        suppressed.assert_called_once_with(
            logging.WARNING,
            "[OCR] Неожиданный результат счётчика: 10",
            key=key,
            payload="10",
        )
        finish.assert_not_called()

    def test_digit_counter_valid_result_closes_suppression_without_changing_result(self):
        counter = DigitCounter((0, 0, 1, 1), name="TEST_COUNTER")
        key = ("ocr-counter-invalid", "DigitCounter", "TEST_COUNTER")

        with (
            patch.object(Ocr, "ocr", return_value="3/10"),
            patch("module.ocr.ocr.logger.log_suppressed") as suppressed,
            patch("module.ocr.ocr.logger.finish_suppressed") as finish,
        ):
            result = counter.ocr(object())

        self.assertEqual((3, 7, 10), result)
        finish.assert_called_once_with(key)
        suppressed.assert_not_called()

    def test_app_running_poll_moves_package_to_debug_without_changing_result(self):
        control = AppControl.__new__(AppControl)
        control.app_current = Mock(return_value="com.YoStarEN.AzurLane")
        control.package = "com.YoStarEN.AzurLane"

        with (
            patch("module.device.app_control.logger.debug") as debug,
            patch("module.device.app_control.logger.info") as info,
        ):
            result = control.app_is_running()

        self.assertTrue(result)
        debug.assert_called_once_with('[Пакет приложения] com.YoStarEN.AzurLane')
        info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
