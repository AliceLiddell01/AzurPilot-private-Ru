"""OCR-распознаватели текста, чисел, счётчиков и длительности."""

import re
import time
from datetime import timedelta
from typing import TYPE_CHECKING

import cv2
import numpy as np

from module.base.button import Button
from module.base.decorator import cached_property
from module.base.utils import crop, crop_to_text, extract_letters, float2str, rgb2luma
from module.logger import logger
from module.ocr.rpc import ModelProxyFactory
from module.webui.setting import State

if TYPE_CHECKING:
    from module.ocr.al_ocr import AlOcr

if not State.deploy_config.UseOcrServer:
    from module.ocr.models import OCR_MODEL
else:
    OCR_MODEL = ModelProxyFactory()


_COMPACT_MAX_LABEL_RE = re.compile(r"^\s*MAX\s*:\s*(?=\d)", re.IGNORECASE)
_COMPACT_NUMERIC_SEPARATOR_RE = re.compile(r"(?<=\d)\s*([:/-])\s*(?=\d)")


def normalize_ocr_text(model_name: str, text: str) -> str:
    """Исправляет только доказанные ложные пробелы English OCR.

    Нормализация ограничена меткой ``MAX`` и разделителями между цифрами.
    Обычные фразы, произвольные подписи с двоеточием и другие модели не
    изменяются.
    """
    if model_name != "azur_lane" or not text:
        return text
    text = _COMPACT_MAX_LABEL_RE.sub("MAX:", text)
    return _COMPACT_NUMERIC_SEPARATOR_RE.sub(r"\1", text)


class Ocr:
    SHOW_LOG = True
    SHOW_REVISE_WARNING = False

    def __init__(
        self,
        buttons,
        lang="azur_lane",
        letter=(255, 255, 255),
        threshold=128,
        alphabet=None,
        name=None,
    ):
        self.name = str(buttons) if isinstance(buttons, Button) else name
        self._buttons = buttons
        self.letter = letter
        self.threshold = threshold
        self.alphabet = alphabet
        self.lang = lang

    @property
    def cnocr(self) -> "AlOcr":
        return OCR_MODEL.__getattribute__(self.lang)

    @property
    def buttons(self):
        buttons = self._buttons
        buttons = buttons if isinstance(buttons, list) else [buttons]
        return [button.area if isinstance(button, Button) else button for button in buttons]

    @buttons.setter
    def buttons(self, value):
        self._buttons = value

    def pre_process(self, image):
        image = extract_letters(image, letter=self.letter, threshold=self.threshold)
        return image.astype(np.uint8)

    def after_process(self, result):
        model_name = getattr(self, "lang", "azur_lane")
        return normalize_ocr_text(model_name, result)

    def ocr(self, image, direct_ocr=False):
        start_time = time.time()

        if direct_ocr:
            image_list = [self.pre_process(item) for item in image]
        else:
            image_list = [self.pre_process(crop(image, area)) for area in self.buttons]

        image_list = [crop_to_text(item) for item in image_list]
        result_list = self.cnocr.atomic_ocr_for_single_lines(image_list, self.alphabet)
        result_list = ["".join(result) for result in result_list]
        result_list = [self.after_process(result) for result in result_list]

        if len(self.buttons) == 1:
            result_list = result_list[0]
        if self.SHOW_LOG:
            logger.attr(
                name="%s %ss" % (self.name, float2str(time.time() - start_time)),
                text=str(result_list),
            )
        return result_list


class OcrYuv(Ocr):
    """OCR по яркостному каналу YUV."""

    @cached_property
    def letter_y(self):
        array = np.array([[self.letter]], dtype=np.uint8)
        return rgb2luma(array)[0][0]

    def pre_process(self, image):
        y = rgb2luma(image)
        letter_y = (np.ones(y.shape) * self.letter_y).astype(np.uint8)
        diff = cv2.absdiff(y, letter_y)
        return cv2.multiply(diff, 255.0 / self.threshold)


class Digit(Ocr):
    """Распознаватель целых чисел."""

    def __init__(
        self,
        buttons,
        lang="azur_lane",
        letter=(255, 255, 255),
        threshold=128,
        alphabet="0123456789IDSB",
        name=None,
    ):
        super().__init__(
            buttons,
            lang=lang,
            letter=letter,
            threshold=threshold,
            alphabet=alphabet,
            name=name,
        )

    def after_process(self, result):
        result = super().after_process(result)
        result = result.replace("I", "1").replace("D", "0").replace("S", "5")
        result = result.replace("B", "8")

        previous = result
        result = int(result) if result else 0
        if self.SHOW_REVISE_WARNING and str(result) != previous:
            logger.warning(
                f'[OCR] {self.name}: результат "{previous}" исправлен на "{result}"'
            )
        return result


class DigitYuv(Digit, OcrYuv):
    pass


class DigitCounter(Ocr):
    def __init__(
        self,
        buttons,
        lang="azur_lane",
        letter=(255, 255, 255),
        threshold=128,
        alphabet="0123456789/IDSB",
        name=None,
    ):
        super().__init__(
            buttons,
            lang=lang,
            letter=letter,
            threshold=threshold,
            alphabet=alphabet,
            name=name,
        )

    def after_process(self, result):
        result = super().after_process(result)
        result = result.replace("I", "1").replace("D", "0").replace("S", "5")
        return result.replace("B", "8")

    def ocr(self, image, direct_ocr=False):
        result_list = super().ocr(image, direct_ocr=direct_ocr)
        result = result_list[0] if isinstance(result_list, list) else result_list

        match = re.search(r"(\d+)/(\d+)", result)
        if match:
            current, total = (int(value) for value in match.groups())
            current = min(current, total)
            return current, total - current, total
        logger.warning(f"[OCR] Неожиданный результат счётчика: {result_list}")
        return 0, 0, 0


class DigitCounterYuv(DigitCounter, OcrYuv):
    pass


class Duration(Ocr):
    def __init__(
        self,
        buttons,
        lang="azur_lane",
        letter=(255, 255, 255),
        threshold=128,
        alphabet="0123456789:IDSB",
        name=None,
    ):
        super().__init__(
            buttons,
            lang=lang,
            letter=letter,
            threshold=threshold,
            alphabet=alphabet,
            name=name,
        )

    def after_process(self, result):
        result = super().after_process(result)
        result = result.replace("I", "1").replace("D", "0").replace("S", "5")
        return result.replace("B", "8")

    def ocr(self, image, direct_ocr=False):
        result_list = super().ocr(image, direct_ocr=direct_ocr)
        if not isinstance(result_list, list):
            result_list = [result_list]
        result_list = [self.parse_time(result) for result in result_list]
        return result_list[0] if len(self.buttons) == 1 else result_list

    @staticmethod
    def parse_time(string):
        match = re.search(r"(\d{1,2}):?(\d{2}):?(\d{2})", string)
        if match:
            hours, minutes, seconds = (int(value) for value in match.groups())
            return timedelta(hours=hours, minutes=minutes, seconds=seconds)
        logger.warning(f"[OCR] Недопустимая длительность: {string}")
        return timedelta(hours=0, minutes=0, seconds=0)


class DurationYuv(Duration, OcrYuv):
    pass
