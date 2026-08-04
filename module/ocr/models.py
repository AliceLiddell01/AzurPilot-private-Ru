"""Ленивое управление единственной Global/English OCR-моделью AzurPilot."""

from module.base.decorator import cached_property
from module.ocr.al_ocr import AlOcr


class OcrModel:
    """Коллекция локальных OCR-моделей персональной EN/Global сборки."""

    @cached_property
    def azur_lane(self):
        """Английская OCR-модель для чисел и компактных значений интерфейса."""
        return AlOcr(name="azur_lane")


OCR_MODEL = OcrModel()
