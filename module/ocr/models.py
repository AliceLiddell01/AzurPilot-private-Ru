"""Ленивое управление экземплярами моделей OCR."""

from module.base.decorator import cached_property
from module.ocr.al_ocr import AlOcr
from module.ocr.stage8b_runtime import install_stage8b_runtime_patches

install_stage8b_runtime_patches()


class OcrModel:
    """Набор лениво создаваемых OCR-моделей для поддерживаемых языков."""

    @cached_property
    def azur_lane(self):
        """Англо-цифровая модель Azur Lane."""
        return AlOcr(name='azur_lane')

    @cached_property
    def azur_lane_jp(self):
        """Специализированная модель для японского сервера."""
        return AlOcr(name='azur_lane_jp')

    @cached_property
    def ppocr_v6(self):
        """Универсальная модель PP-OCRv6."""
        return AlOcr(name='ppocr_v6')

    @cached_property
    def cnocr(self):
        """Модель распознавания китайского и английского текста."""
        return AlOcr(name='cn')

    @cached_property
    def jp(self):
        """Модель распознавания японского текста."""
        return AlOcr(name='jp')

    @cached_property
    def tw(self):
        """Модель распознавания традиционного китайского текста."""
        return AlOcr(name='tw')


OCR_MODEL = OcrModel()
