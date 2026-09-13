from types import SimpleNamespace

import cv2
import numpy as np

import module.base.utils as base_utils
import module.os.globe_operation as globe_operation
from module.os.globe_operation import GlobeOperation, _zone_pinned_match_profile


def test_zone_pinned_match_profiles_preserve_existing_thresholds():
    assert _zone_pinned_match_profile((1280, 720)) == (0.75, ())
    assert _zone_pinned_match_profile((1920, 1080)) == (0.70, ())
    assert _zone_pinned_match_profile((2560, 1440)) == (0.72, ())
    assert _zone_pinned_match_profile((3840, 2160)) == (0.65, (1.04,))
    assert _zone_pinned_match_profile((1600, 900)) == (0.75, ())


def test_global_non_native_threshold_for_unrelated_detectors_is_unchanged(monkeypatch):
    monkeypatch.setattr(base_utils, 'TEMPLATE_MATCH_NON_NATIVE_720P', True)
    monkeypatch.setattr(base_utils, 'TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION', (3840, 2160))

    assert base_utils.TEMPLATE_MATCH_NON_NATIVE_720P_THRESHOLD == 0.75
    assert base_utils.lower_template_match_similarity(0.85) == 0.75
    assert base_utils.lower_template_match_similarity(0.65) == 0.65


def test_native_and_unknown_resolution_do_not_use_scaled_fallback(monkeypatch):
    fake_zone = SimpleNamespace(name='ZONE_DANGEROUS')
    monkeypatch.setattr(globe_operation, 'ZONE_TYPES', [fake_zone])
    monkeypatch.setattr(globe_operation, 'ASSETS_PINNED_ZONE', [])

    operation = object.__new__(GlobeOperation)
    operation.appear = lambda *args, **kwargs: False
    operation.device = SimpleNamespace(image=np.zeros((720, 1280, 3), dtype=np.uint8))

    def unexpected_fallback(*args, **kwargs):
        raise AssertionError('Масштабированный fallback не должен использоваться для этого разрешения')

    operation._match_zone_pinned_scaled = unexpected_fallback

    for resolution in ((1280, 720), (1600, 900)):
        monkeypatch.setattr(base_utils, 'TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION', resolution)
        assert operation.get_zone_pinned() is None


def test_4k_fallback_runs_after_exact_match_with_existing_threshold(monkeypatch):
    fake_zone = SimpleNamespace(name='ZONE_DANGEROUS')
    loaded_offsets = []
    follower = SimpleNamespace(load_offset=lambda zone: loaded_offsets.append(zone))
    monkeypatch.setattr(globe_operation, 'ZONE_TYPES', [fake_zone])
    monkeypatch.setattr(globe_operation, 'ASSETS_PINNED_ZONE', [follower])
    monkeypatch.setattr(base_utils, 'TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION', (3840, 2160))

    operation = object.__new__(GlobeOperation)
    operation.device = SimpleNamespace(image=np.zeros((720, 1280, 3), dtype=np.uint8))
    exact_calls = []
    fallback_calls = []

    def exact_match(zone, offset, similarity):
        exact_calls.append((zone, offset, similarity))
        return False

    def scaled_match(zone, image, similarity, scale, offset):
        fallback_calls.append((zone, image, similarity, scale, offset))
        return True

    operation.appear = exact_match
    operation._match_zone_pinned_scaled = scaled_match

    assert operation.get_zone_pinned() is fake_zone
    assert exact_calls == [(fake_zone, (20, 20), 0.65)]
    assert len(fallback_calls) == 1
    assert fallback_calls[0][0] is fake_zone
    assert fallback_calls[0][1] is operation.device.image
    assert fallback_calls[0][2:] == (0.65, 1.04, (20, 20))
    assert loaded_offsets == [fake_zone]


def test_scaled_matcher_recovers_small_template_without_color_conversion(monkeypatch):
    rng = np.random.default_rng(20260814)
    template = rng.integers(0, 256, size=(12, 30, 3), dtype=np.uint8)
    scaled = cv2.resize(template, None, fx=1.04, fy=1.04, interpolation=cv2.INTER_CUBIC)

    class FakeZone:
        area = (20, 20, 50, 32)
        _button = (20, 20, 50, 32)
        image = template
        is_gif = False
        _button_offset = None

        @staticmethod
        def ensure_template():
            return None

    image = np.zeros((80, 100, 3), dtype=np.uint8)
    y, x = 18, 22
    height, width = scaled.shape[:2]
    image[y:y + height, x:x + width] = scaled

    monkeypatch.setattr(base_utils, 'TEMPLATE_MATCH_NON_NATIVE_720P', True)
    zone = FakeZone()

    assert GlobeOperation._match_zone_pinned_scaled(
        zone,
        image,
        similarity=0.65,
        scale=1.04,
        offset=(20, 20),
    ) is True
    assert zone._button_offset is not None
