from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from module.base.template import Template
from module.base.utils import rgb2gray
from module.exception import (
    OpsiError,
    OpsiStorageError,
    OpsiStorageTemplateMatchError,
    ScriptError,
)
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
    assert origin in _matched_origins(items)


def test_rgb_source_and_grayscale_template_use_symmetric_normalization(tmp_path):
    path = tmp_path / 'template.png'
    gray = _write_gray_png(path)
    template = Template(str(path))
    gray_source, origin = _source_with_template(gray)
    source = np.repeat(gray_source[:, :, None], 3, axis=2)

    items = template.match_multi(source, similarity=0.99)

    assert origin in _matched_origins(items)


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


def test_opsi_storage_error_keeps_script_recovery_classification():
    assert issubclass(OpsiError, ScriptError)
    assert issubclass(OpsiStorageError, OpsiError)
    assert issubclass(OpsiStorageTemplateMatchError, OpsiStorageError)
    assert isinstance(OpsiStorageTemplateMatchError('ошибка'), ScriptError)


def test_unknown_storage_item_uses_opsi_storage_exception():
    with pytest.raises(OpsiStorageError, match='Неизвестный предмет хранилища'):
        StorageHandler._storage_item_to_template('UNKNOWN')
