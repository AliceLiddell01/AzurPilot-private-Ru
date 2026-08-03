from __future__ import annotations

import functools
import os
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from module.logger import logger
from module.ocr.stage8b_privacy import OcrDebugOutputError, save_debug_image

_INSTALLED = False
_LOGGER_PATCHED = False

_LITERAL_TRANSLATIONS = {
    "[OCR] 若不支持 GPU 加速，请关闭 GPU 加速。": "[OCR] Если ускорение GPU не поддерживается, отключите его в настройках.",
    "[OCR] 若仍然无法解决，请尝试寻求社区的帮助。": "[OCR] Если проблема сохраняется, обратитесь в сообщество поддержки.",
    "[OCR] 使用 ncnn 后端，正在使用专用的 ncnn OCR 识别模型": "[OCR] Выбран backend ncnn; используется специализированная модель распознавания ncnn",
    "[战役] 数字与文本之间未找到间隔。": "[Кампания — OCR] Не найден интервал между номером этапа и текстом.",
    "[战役] 未找到关卡。": "[Кампания — OCR] Этапы не найдены.",
    "[战役-OCR] 获取章节索引时出现撤退按钮": "[Кампания — OCR] При определении главы обнаружена кнопка отступления",
}
_PATTERN_TRANSLATIONS = (
    (re.compile(r"^OCR dependencies failed to load: (.*)$"), r"Не удалось загрузить зависимости OCR: \1"),
    (re.compile(r"^Created AlOcr instance with name='([^']+)', kwargs=(.*), PID=(\d+)$"), r"Создан экземпляр AlOcr: name='\1', kwargs=\2, PID=\3"),
    (re.compile(r"^Loaded OCR model '([^']+)' with (.*)$"), r"Загружена модель OCR '\1' через \2"),
    (re.compile(r"^OCR model '([^']+)' is recognition-only; RapidOCR-compatible callers will use '([^']+)'$"), r"Модель OCR '\1' поддерживает только распознавание; для совместимого конвейера RapidOCR используется '\2'"),
    (re.compile(r"^OCR model version '([^']+)' is unavailable for '([^']+)'; falling back to '([^']+)'$"), r"Версия модели OCR '\1' недоступна для '\2'; используется '\3'"),
    (re.compile(r"^\[VERBOSE\] AlOcr\.ocr: ensuring model loaded\.\.\.$"), r"[VERBOSE] AlOcr.ocr: проверка загрузки модели..."),
    (re.compile(r"^AlOcr\.ocr error: (.*)$"), r"Ошибка AlOcr.ocr: \1"),
    (re.compile(r"^AlOcr\.det error: (.*)$"), r"Ошибка AlOcr.det: \1"),
    (re.compile(r"^Batch OCR failed on image (\d+): (.*)$"), r"Пакетный OCR завершился ошибкой для изображения \1: \2"),
    (re.compile(r"^\[战役-OCR\] 未知的关卡名称: (.*)$"), r"[Кампания — OCR] Неизвестное имя этапа: \1"),
)
_EXCEPTION_TRANSLATIONS = (
    (re.compile(r"^OCR model not found: (.*)$"), r"Модель OCR не найдена: \1"),
    (re.compile(r"^OCR model is closed$"), r"Модель OCR уже закрыта"),
    (re.compile(r"^Invalid OCR image size: (.*)$"), r"Недопустимый размер OCR-изображения: \1"),
    (re.compile(r"^Unsupported OCR image shape: (.*)$"), r"Неподдерживаемая форма OCR-изображения: \1"),
    (re.compile(r"^Unsupported OCR model: (.*)$"), r"Неподдерживаемая модель OCR: \1"),
    (re.compile(r"^Unsupported ncnn OCR model: (.*)$"), r"Неподдерживаемая модель OCR ncnn: \1"),
)


def _translate_message(message: Any) -> Any:
    if not isinstance(message, str):
        return message
    translated = _LITERAL_TRANSLATIONS.get(message)
    if translated is not None:
        return translated
    for pattern, replacement in _PATTERN_TRANSLATIONS:
        if pattern.fullmatch(message):
            return pattern.sub(replacement, message)
    return message


def _install_logger_translation_filter() -> None:
    global _LOGGER_PATCHED
    if _LOGGER_PATCHED:
        return
    for method_name in ("debug", "info", "warning", "error", "critical"):
        original = getattr(logger, method_name)

        @functools.wraps(original)
        def translated(message, *args, __original=original, **kwargs):
            return __original(_translate_message(message), *args, **kwargs)

        setattr(logger, method_name, translated)

    original_attr = logger.attr
    attr_names = {"章节": "Глава", "关卡": "Этапы"}

    @functools.wraps(original_attr)
    def translated_attr(name, text, *args, **kwargs):
        return original_attr(attr_names.get(name, name), text, *args, **kwargs)

    logger.attr = translated_attr
    _LOGGER_PATCHED = True


def _translate_exception(error: Exception) -> Exception:
    message = str(error)
    for pattern, replacement in _EXCEPTION_TRANSLATIONS:
        if pattern.fullmatch(message):
            return type(error)(pattern.sub(replacement, message))
    return error


def _wrap_exceptions(function):
    if getattr(function, "_stage8b_exception_wrapper", False):
        return function

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            translated = _translate_exception(error)
            if translated is error:
                raise
            raise translated from error

    wrapped._stage8b_exception_wrapper = True
    return wrapped


def _safe_rec_debug(self, image, _result):
    try:
        return save_debug_image(image, model_name=self.name, kind="rec")
    except OcrDebugOutputError as error:
        logger.warning(f"Не удалось сохранить отладочное OCR-изображение: {error}")
        return None


def _safe_det_debug(self, image, results):
    from PIL import Image

    if isinstance(image, Image.Image):
        image = np.asarray(image.convert("RGB"))
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    elif isinstance(image, (str, Path)):
        image = cv2.imread(str(image))
        if image is None:
            return None
    if not isinstance(image, np.ndarray):
        return None

    drawn = image.copy()
    for text, box, score in results:
        points = np.asarray(box, dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(drawn, [points], True, (0, 255, 0), 2)
        center_x = int(sum(point[0] for point in box) / len(box))
        center_y = int(sum(point[1] for point in box) / len(box))
        cv2.putText(
            drawn,
            f"{text} {score:.2f}",
            (center_x - 20, center_y - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 255, 0),
            1,
        )
    try:
        return save_debug_image(drawn, model_name=self.name, kind="det")
    except OcrDebugOutputError as error:
        logger.warning(f"Не удалось сохранить отладочное изображение OCR detection: {error}")
        return None


def install_stage8b_runtime_patches(target: Any | None = None) -> None:
    """Переводит first-party OCR diagnostics и включает безопасный debug output."""
    global _INSTALLED
    if _INSTALLED:
        return
    if target is None:
        import module.ocr.al_ocr as target

    _install_logger_translation_filter()
    target.AlOcr._save_debug_image = _safe_rec_debug
    target.AlOcr._save_det_debug = _safe_det_debug

    target.AlOcrCtcRecOCR.__init__ = _wrap_exceptions(target.AlOcrCtcRecOCR.__init__)
    target.AlOcrCtcRecOCR.__call__ = _wrap_exceptions(target.AlOcrCtcRecOCR.__call__)
    target.AlOcrCtcRecOCR._preprocess = _wrap_exceptions(target.AlOcrCtcRecOCR._preprocess)
    target.AlOcrCtcRecOCR._to_gray = staticmethod(
        _wrap_exceptions(target.AlOcrCtcRecOCR._to_gray)
    )
    target._resolve_onnx_model_version = _wrap_exceptions(
        target._resolve_onnx_model_version
    )
    target._create_ocr = _wrap_exceptions(target._create_ocr)
    _INSTALLED = True
