import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from module.map_detection.homography import Homography
from module.map_detection.view import View
from module.os.globe_camera import GlobeCamera
from module.os.globe_detection import GlobeDetection


class TestHomographyLoggingSemantics(unittest.TestCase):
    def test_opsi_detection_telemetry_uses_debug(self):
        homography = Homography(SimpleNamespace(Scheduler_Command='OpsiDaily'))

        with (
            patch('module.map_detection.homography.logger.debug') as debug,
            patch('module.map_detection.homography.logger.info') as info,
            patch('module.map_detection.homography.logger.attr_align') as attr_align,
        ):
            homography._log_detection('raw map state')
            homography._log_detection_attr('Центры клеток', '0.95 (good match)')

        self.assertEqual(2, debug.call_count)
        info.assert_not_called()
        attr_align.assert_not_called()

    def test_campaign_detection_telemetry_keeps_info_contract(self):
        homography = Homography(SimpleNamespace(Scheduler_Command='Commission'))

        with (
            patch('module.map_detection.homography.logger.debug') as debug,
            patch('module.map_detection.homography.logger.info') as info,
            patch('module.map_detection.homography.logger.attr_align') as attr_align,
        ):
            homography._log_detection('raw map state')
            homography._log_detection_attr('Центры клеток', '0.95 (good match)')

        debug.assert_not_called()
        info.assert_called_once_with('raw map state')
        attr_align.assert_called_once_with('Центры клеток', '0.95 (good match)')


class TestOpsiViewLoggingSemantics(unittest.TestCase):
    def test_opsi_prediction_summary_uses_debug_without_changing_grid_calls(self):
        grid = Mock()
        view = View.__new__(View)
        view.mode = 'os'
        view.grids = {(0, 0): grid}

        with (
            patch('module.map_detection.view.logger.debug') as debug,
            patch('module.map_detection.view.logger.info') as info,
            patch('module.map_detection.view.logger.attr_align') as attr_align,
        ):
            result = view.predict()

        self.assertIsNone(result)
        grid.predict.assert_called_once_with()
        debug.assert_called_once()
        self.assertIn('Распознано клеток: 1', debug.call_args.args[0])
        info.assert_not_called()
        attr_align.assert_not_called()

    def test_campaign_prediction_summary_keeps_info_attribute(self):
        grid = Mock()
        view = View.__new__(View)
        view.mode = 'main'
        view.grids = {(0, 0): grid}

        with (
            patch('module.map_detection.view.logger.debug') as debug,
            patch('module.map_detection.view.logger.attr_align') as attr_align,
        ):
            result = view.predict()

        self.assertIsNone(result)
        grid.predict.assert_called_once_with()
        debug.assert_not_called()
        attr_align.assert_called_once()
        self.assertEqual('predict', attr_align.call_args.args[0])
        self.assertEqual(1, attr_align.call_args.args[1])


class TestGlobeDetectionLoggingSemantics(unittest.TestCase):
    def test_raw_location_and_similarity_use_debug_but_warning_threshold_is_preserved(self):
        detector = GlobeDetection.__new__(GlobeDetection)
        detector.config = SimpleNamespace(
            OS_LOCAL_FIND_PEAKS_PARAMETERS={},
            OS_GLOBE_IMAGE_RESIZE=1,
            OS_GLOBE_IMAGE_PAD=0,
        )
        detector.globe = np.zeros((2, 2), dtype=np.uint8)
        detector.homo_center = np.array([0, 0])
        detector.load_globe_map = Mock(return_value=False)
        detector.find_peaks = Mock(return_value=np.zeros((2, 2), dtype=np.uint8))
        detector.perspective_transform = Mock(return_value=np.zeros((2, 2), dtype=np.uint8))

        with (
            patch('module.os.globe_detection.cv2.resize', side_effect=lambda image, *_args, **_kwargs: image),
            patch('module.os.globe_detection.cv2.matchTemplate', return_value=np.zeros((1, 1), dtype=np.float32)),
            patch('module.os.globe_detection.cv2.minMaxLoc', return_value=(0.0, 0.5, (0, 0), (1, 2))),
            patch('module.os.globe_detection.logger.debug') as debug,
            patch('module.os.globe_detection.logger.warning') as warning,
        ):
            result = detector.load(np.zeros((2, 2), dtype=np.uint8))

        self.assertIsNone(result)
        self.assertEqual((1.0, 2.0), detector.center_loca)
        self.assertEqual(2, debug.call_count)
        warning.assert_not_called()

    def test_low_similarity_keeps_warning(self):
        detector = GlobeDetection.__new__(GlobeDetection)
        detector.config = SimpleNamespace(
            OS_LOCAL_FIND_PEAKS_PARAMETERS={},
            OS_GLOBE_IMAGE_RESIZE=1,
            OS_GLOBE_IMAGE_PAD=0,
        )
        detector.globe = np.zeros((2, 2), dtype=np.uint8)
        detector.homo_center = np.array([0, 0])
        detector.load_globe_map = Mock(return_value=False)
        detector.find_peaks = Mock(return_value=np.zeros((2, 2), dtype=np.uint8))
        detector.perspective_transform = Mock(return_value=np.zeros((2, 2), dtype=np.uint8))

        with (
            patch('module.os.globe_detection.cv2.resize', side_effect=lambda image, *_args, **_kwargs: image),
            patch('module.os.globe_detection.cv2.matchTemplate', return_value=np.zeros((1, 1), dtype=np.float32)),
            patch('module.os.globe_detection.cv2.minMaxLoc', return_value=(0.0, 0.05, (0, 0), (1, 2))),
            patch('module.os.globe_detection.logger.debug') as debug,
            patch('module.os.globe_detection.logger.warning') as warning,
        ):
            result = detector.load(np.zeros((2, 2), dtype=np.uint8))

        self.assertIsNone(result)
        self.assertEqual((1.0, 2.0), detector.center_loca)
        self.assertEqual(2, debug.call_count)
        warning.assert_called_once_with(
            '[Операция «Сирена» — распознавание] Слишком низкое сходство при сопоставлении с картой глобуса'
        )


class _SingleZoneCollection:
    def __init__(self, zone):
        self.zone = zone

    def __bool__(self):
        return self.zone is not None

    def sort_by_camera_distance(self, _location):
        return [self.zone]

    def filter(self, _predicate):
        return self

    def delete(self, _other):
        return _SingleZoneCollection(None)

    def __iter__(self):
        if self.zone is None:
            return iter(())
        return iter((self.zone,))


class TestStrongholdLoggingSemantics(unittest.TestCase):
    def test_negative_candidates_use_debug_while_final_result_stays_info(self):
        zone = SimpleNamespace(zone_id=11, location=np.array([0, 0]))
        camera = GlobeCamera.__new__(GlobeCamera)
        camera.globe_camera = np.array([0, 0])
        camera.camera_to_zone = Mock(return_value=SimpleNamespace(location=np.array([0, 0])))
        camera.globe_in_sight = Mock(return_value=None)
        camera.globe2screen = Mock(return_value=np.array([[100, 100]]))
        camera._globe_predict_stronghold = Mock(return_value=False)

        with (
            patch('module.os.globe_camera.logger.debug') as debug,
            patch('module.os.globe_camera.logger.info') as info,
        ):
            result = camera._find_siren_stronghold(_SingleZoneCollection(zone))

        self.assertIsNone(result)
        self.assertEqual(2, debug.call_count)
        self.assertIn('не является крепостью Сирен', debug.call_args_list[-1].args[0])
        info.assert_called_once_with('[Операция «Сирена» — глобус] Поиск крепости Сирен завершён')


if __name__ == '__main__':
    unittest.main()
