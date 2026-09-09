from pathlib import Path
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from PIL import Image

from module.base.button import Button
from module.base.template import Template
from module.base.utils import rgb2gray, rgb2luma, template_match
from module.exception import (
    OpsiError,
    OpsiMapDetectionError,
    OpsiMapDetectionTemplateMatchError,
    OpsiStorageError,
    OpsiStorageTemplateMatchError,
    ScriptError,
    TemplateMatchError,
)
from module.map_detection.os_grid import OSGridPredictor
from module.observability.incident import build_incident_metadata
from module.os_handler.storage import StorageHandler


def _rgb_pattern():
    image = np.zeros((12, 14, 3), dtype=np.uint8)
    image[1:5, 1:6] = (255, 0, 0)
    image[6:10, 7:12] = (0, 255, 0)
    image[2:5, 9:13] = (0, 0, 255)
    return image


def _write_rgb_png(path: Path):
    image = _rgb_pattern()
    Image.fromarray(image, mode='RGB').save(path)
    return image


def _write_gray_png(path: Path):
    image = rgb2gray(_rgb_pattern())
    Image.fromarray(image, mode='L').save(path)
    return image


def _source_with_template(template, *, origin=(17, 21), shape=(60, 70)):
    height, width = template.shape[:2]
    source = np.zeros((*shape, *template.shape[2:]), dtype=template.dtype)
    x, y = origin
    source[y:y + height, x:x + width] = template
    return source, origin


def _matched_origins(items):
    return {button.area[:2] for button in items}


def test_gif_template_matches_grayscale_source_without_channel_assertion(tmp_path):
    path = tmp_path / 'template.gif'
    rgb = _rgb_pattern()
    Image.fromarray(rgb, mode='RGB').save(path, format='GIF')
    template = Template(str(path))

    loaded = template.image[0]
    assert loaded.ndim == 3
    source, origin = _source_with_template(rgb2gray(loaded))

    items = template.match_multi(source, similarity=0.99)

    assert origin in _matched_origins(items)


def test_gif_template_match_matches_grayscale_source_without_channel_assertion(tmp_path):
    path = tmp_path / 'template.gif'
    rgb = _rgb_pattern()
    Image.fromarray(rgb, mode='RGB').save(path, format='GIF')
    template = Template(str(path))
    source, _ = _source_with_template(template.image_gray[0])

    assert template.match(source, similarity=0.99, direct_match=True)


def test_rgb_png_template_match_matches_grayscale_source(tmp_path):
    path = tmp_path / 'template.png'
    rgb = _write_rgb_png(path)
    template = Template(str(path))
    source, _ = _source_with_template(rgb2gray(rgb))

    assert template.match(source, similarity=0.99, direct_match=True)


def test_opsi_akashi_gif_template_matches_grayscale_map_crop():
    asset = Path(__file__).resolve().parents[1] / 'assets/en/template/TEMPLATE_SIREN_Akashi.gif'
    template = Template(str(asset))
    frame = template.image[0]
    source, origin = _source_with_template(rgb2gray(frame), shape=(60, 60))

    assert frame.ndim == 3
    assert frame.shape[2] == 3
    assert frame.dtype == np.uint8
    assert source.shape == (60, 60)
    assert source.ndim == 2
    assert source.dtype == np.uint8
    assert template.match(source, similarity=0.99, direct_match=True)
    assert template.image_gray[0].shape == frame.shape[:2]
    assert template.image_gray[0].dtype == np.uint8


def test_rgb_png_template_matches_grayscale_source(tmp_path):
    path = tmp_path / 'template.png'
    rgb = _write_rgb_png(path)
    template = Template(str(path))
    source, origin = _source_with_template(rgb2gray(rgb))

    items = template.match_multi(source, similarity=0.99)

    assert template.image_gray.ndim == 2
    assert origin in _matched_origins(items)


def test_grayscale_png_template_matches_grayscale_source(tmp_path):
    path = tmp_path / 'template.png'
    gray = _write_gray_png(path)
    template = Template(str(path))
    source, origin = _source_with_template(gray)

    items = template.match_multi(source, similarity=0.99)

    assert template.image.ndim == 2
    assert template.image_gray is template.image
    assert origin in _matched_origins(items)


def test_same_channel_rgb_matching_keeps_existing_path(tmp_path):
    path = tmp_path / 'template.png'
    _write_rgb_png(path)
    template = Template(str(path))
    source, origin = _source_with_template(template.image)

    items = template.match_multi(source, similarity=0.99)

    assert template.image.ndim == 3
    assert template._image_gray is None
    assert origin in _matched_origins(items)


def test_rgb_source_and_grayscale_template_use_symmetric_normalization(tmp_path):
    path = tmp_path / 'template.png'
    gray = _write_gray_png(path)
    template = Template(str(path))
    gray_source, origin = _source_with_template(gray)
    source = np.repeat(gray_source[:, :, None], 3, axis=2)

    items = template.match_multi(source, similarity=0.99)

    assert origin in _matched_origins(items)


def test_rgb_source_and_grayscale_template_match_use_symmetric_normalization(tmp_path):
    path = tmp_path / 'template.png'
    gray = _write_gray_png(path)
    template = Template(str(path))
    gray_source, _ = _source_with_template(gray)
    source = np.repeat(gray_source[:, :, None], 3, axis=2)

    assert template.match(source, similarity=0.99, direct_match=True)


def test_match_result_normalizes_channels_but_keeps_raw_rgb_metadata(tmp_path):
    path = tmp_path / 'template.png'
    gray = _write_gray_png(path)
    template = Template(str(path))
    gray_source, origin = _source_with_template(gray)
    source = np.repeat(gray_source[:, :, None], 3, axis=2)

    similarity, button = template.match_result(source)

    assert similarity > 0.99
    assert button.area[:2] == origin
    assert button.image.ndim == 3
    assert len(button.color) == 3


def test_template_match_normalizes_depth_only_when_needed(tmp_path):
    path = tmp_path / 'template.png'
    rgb = _write_rgb_png(path)
    template = Template(str(path))
    source, _ = _source_with_template(rgb)

    assert template.match(source.astype(np.float32), similarity=0.99, direct_match=True)


def test_match_result_keeps_raw_rgb_for_button_metadata(tmp_path):
    path = tmp_path / 'template.png'
    rgb = _write_rgb_png(path)
    template = Template(str(path))
    source, origin = _source_with_template(rgb)

    similarity, button = template.match_result(source)

    assert similarity > 0.99
    assert button.area[:2] == origin
    assert button.image.ndim == 3
    assert len(button.color) == 3


def test_match_luma_result_keeps_raw_rgb_for_button_metadata(tmp_path):
    path = tmp_path / 'template.png'
    rgb = _write_rgb_png(path)
    template = Template(str(path))
    source, origin = _source_with_template(rgb)

    similarity, button = template.match_luma_result(source)

    assert similarity > 0.99
    assert button.area[:2] == origin
    assert button.image.ndim == 3
    assert len(button.color) == 3


def test_match_luma_normalizes_grayscale_source_for_static_rgb_template(tmp_path):
    path = tmp_path / 'template.png'
    rgb = _write_rgb_png(path)
    template = Template(str(path))
    source, _ = _source_with_template(rgb2luma(rgb))

    assert template.match_luma(source, similarity=0.99)


def test_binary_matching_accepts_grayscale_source_and_template(tmp_path):
    path = tmp_path / 'template.png'
    gray = _write_gray_png(path)
    template = Template(str(path))
    source, _ = _source_with_template(gray)

    assert template.match_binary(source, similarity=0.99)


def test_button_match_normalizes_mismatched_channels(tmp_path):
    path = tmp_path / 'button.png'
    rgb = np.zeros((30, 30, 3), dtype=np.uint8)
    pattern = _rgb_pattern()
    area = (5, 5, 19, 17)
    rgb[area[1]:area[3], area[0]:area[2]] = pattern
    Image.fromarray(rgb, mode='RGB').save(path)
    button = Button(area=area, color=(), button=area, file=str(path))
    source = np.zeros((30, 30), dtype=np.uint8)
    source[area[1]:area[3], area[0]:area[2]] = rgb2gray(pattern)

    assert button.match(source, offset=(0, 0), similarity=0.99)
    assert button.match(source, offset=(2, 2), similarity=0.99)
    assert button.image_gray.ndim == 2


def test_template_match_wraps_cv2_error_with_typed_cause():
    first = np.zeros((20, 20), dtype=np.uint8)
    second = np.zeros((5, 5), dtype=np.uint8)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr('module.base.utils.cv2.matchTemplate', Mock(side_effect=cv2.error('assertion')))
        with pytest.raises(TemplateMatchError) as error_info:
            template_match(first, second, name='TEST_TEMPLATE')

    error = error_info.value
    assert isinstance(error.__cause__, cv2.error)
    assert 'TEST_TEMPLATE' in str(error)
    assert 'shape=(20, 20)' in str(error)
    assert 'channels=1' in str(error)


def test_opsi_map_template_failure_is_subject_specific_and_preserves_cause():
    class FailingTemplate:
        name = 'TEMPLATE_SIREN_AKASHI'

        @staticmethod
        def match(_image, *, similarity, direct_match):
            assert similarity == 0.9
            assert direct_match is True
            raise TemplateMatchError('первый shape=(60, 60), dtype=uint8, channels=1; второй shape=(18, 15, 3), dtype=uint8, channels=3')

    predictor = OSGridPredictor.__new__(OSGridPredictor)
    predictor.relative_crop = lambda *_args, **_kwargs: np.zeros((60, 60, 3), dtype=np.uint8)
    predictor._os_template_enemy = {'Akashi': FailingTemplate()}
    predictor._os_template_enemy_upper = {}

    with pytest.raises(OpsiMapDetectionTemplateMatchError) as error_info:
        predictor.predict_enemy_genre()

    error = error_info.value
    assert isinstance(error.__cause__, TemplateMatchError)
    assert isinstance(error, OpsiMapDetectionError)
    assert isinstance(error, ScriptError)
    assert 'TEMPLATE_SIREN_AKASHI' in str(error)
    assert 'shape=(60, 60)' in str(error)
    metadata = build_incident_metadata(profile='ap', exception=error, task='OpsiAbyssal')
    assert metadata.exception_type == 'OpsiMapDetectionTemplateMatchError'


def test_opsi_current_fleet_template_failure_is_subject_specific(monkeypatch):
    class FailingTemplate:
        @staticmethod
        def match(_image):
            raise TemplateMatchError('текущий флот: несовместимые каналы')

    predictor = OSGridPredictor.__new__(OSGridPredictor)
    predictor.relative_hsv_count = lambda **_kwargs: 700
    predictor.relative_crop = lambda *_args, **_kwargs: np.zeros((60, 60, 3), dtype=np.uint8)
    monkeypatch.setattr('module.map_detection.grid_predictor.TEMPLATE_FLEET_CURRENT', FailingTemplate())

    with pytest.raises(OpsiMapDetectionTemplateMatchError) as error_info:
        predictor.predict_current_fleet()

    assert isinstance(error_info.value.__cause__, TemplateMatchError)
    assert 'текущий флот' in str(error_info.value.__cause__)


def test_opsi_map_match_result_failure_is_subject_specific_and_preserves_cause():
    class FailingTemplate:
        name = 'TEMPLATE_FLEET_MECHANISM'

        @staticmethod
        def match_result(_image, *, name=None):
            assert name is None
            raise TemplateMatchError('первый shape=(60, 60), dtype=uint8, channels=1; второй shape=(38, 53, 3), dtype=uint8, channels=3')

    with pytest.raises(OpsiMapDetectionTemplateMatchError) as error_info:
        OSGridPredictor._match_result(FailingTemplate(), np.zeros((60, 60), dtype=np.uint8))

    error = error_info.value
    assert isinstance(error.__cause__, TemplateMatchError)
    assert 'TEMPLATE_FLEET_MECHANISM' in str(error)
    assert 'shape=(60, 60)' in str(error)


def test_grayscale_representation_is_cached_and_released(tmp_path):
    path = tmp_path / 'template.png'
    _write_rgb_png(path)
    template = Template(str(path))

    first = template.image_gray
    assert template.image_gray is first

    template.resource_release()

    assert template._image_gray is None
    second = template.image_gray
    assert second is not first
    np.testing.assert_array_equal(second, first)


def test_opsi_storage_template_error_preserves_cause_and_incident_type():
    class FailingTemplate:
        name = 'TEMPLATE_STORAGE_LOGGER'

        @staticmethod
        def match_multi(_image, *, similarity):
            assert similarity == 0.5
            raise cv2.error('несовместимые каналы')

    source = np.zeros((20, 30), dtype=np.uint8)

    with pytest.raises(OpsiStorageTemplateMatchError) as error_info:
        StorageHandler._match_storage_template(FailingTemplate(), source, similarity=0.5)

    error = error_info.value
    assert isinstance(error.__cause__, cv2.error)
    assert 'TEMPLATE_STORAGE_LOGGER' in str(error)
    assert 'shape=(20, 30)' in str(error)
    assert 'dtype=uint8' in str(error)
    assert 'channels=1' in str(error)

    metadata = build_incident_metadata(profile='alas', exception=error)
    assert metadata.exception_type == 'OpsiStorageTemplateMatchError'


def test_opsi_storage_template_match_error_wraps_base_matcher_error():
    class FailingTemplate:
        name = 'TEMPLATE_STORAGE_LOGGER'

        @staticmethod
        def match_multi(_image, *, similarity):
            assert similarity == 0.5
            try:
                raise cv2.error('несовместимые каналы')
            except cv2.error as cause:
                raise TemplateMatchError(
                    'Шаблонный поиск завершился ошибкой OpenCV: '
                    'первый shape=(20, 30), dtype=uint8, channels=1; '
                    'второй shape=(36, 41, 3), dtype=uint8, channels=3'
                ) from cause

    source = np.zeros((20, 30), dtype=np.uint8)

    with pytest.raises(OpsiStorageTemplateMatchError) as error_info:
        StorageHandler._match_storage_template(FailingTemplate(), source, similarity=0.5)

    error = error_info.value
    assert isinstance(error.__cause__, TemplateMatchError)
    assert isinstance(error.__cause__.__cause__, cv2.error)
    assert 'TEMPLATE_STORAGE_LOGGER' in str(error)


def test_opsi_storage_error_keeps_script_recovery_classification():
    assert issubclass(OpsiError, ScriptError)
    assert issubclass(OpsiStorageError, OpsiError)
    assert issubclass(OpsiStorageTemplateMatchError, OpsiStorageError)
    assert isinstance(OpsiStorageTemplateMatchError('ошибка'), ScriptError)
    assert issubclass(OpsiMapDetectionError, OpsiError)
    assert issubclass(OpsiMapDetectionTemplateMatchError, OpsiMapDetectionError)
    assert isinstance(OpsiMapDetectionTemplateMatchError('ошибка'), ScriptError)


def test_unknown_storage_item_uses_opsi_storage_exception():
    with pytest.raises(OpsiStorageError, match='Неизвестный предмет хранилища'):
        StorageHandler._storage_item_to_template('UNKNOWN')
