"""Ленивое управление единственным публичным EN/Global OCR namespace."""

from module.base.decorator import cached_property
from module.ocr.global_english import GlobalEnglishOcr


class OcrModel:
    """Коллекция локальных OCR-моделей персональной EN/Global сборки."""

    @cached_property
    def azur_lane(self):
        """EN/Global OCR с маршрутизацией compact values и natural text."""
        return GlobalEnglishOcr()


OCR_MODEL = OcrModel()
