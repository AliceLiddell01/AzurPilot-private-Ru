"""Ленивое управление OCR-моделями персонального EN/Global-форка."""

from module.base.decorator import cached_property
from module.ocr.al_ocr import AlOcr


class OcrModel:
    """Набор поддерживаемых OCR-моделей английской версии игры."""

    @cached_property
    def azur_lane(self):
        """Специализированная английская модель Azur Lane."""
        return AlOcr(name="azur_lane")

    @cached_property
    def ppocr_v6(self):
        """Универсальная PP-OCRv6 как дополнительный английский вариант."""
        return AlOcr(name="ppocr_v6")

    @cached_property
    def cnocr(self):
        """Совместимость legacy-вызовов через универсальную PP-OCRv6.

        Историческое имя ``cnocr`` используется несколькими игровыми модулями
        независимо от выбранного сервера. В EN/Global-форке оно не загружает
        удалённые китайские веса, а направляется на сохранённую универсальную
        модель PP-OCRv6.
        """
        return self.ppocr_v6


OCR_MODEL = OcrModel()
