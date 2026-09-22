"""OCR-читатель ресурсов с одного нормализованного экрана Global/EN."""

from __future__ import annotations

import re
from typing import Final

from module.application.errors import OperationFailedError

_FRAME_WIDTH: Final = 1280
_FRAME_HEIGHT: Final = 720
_MAIN_RESOURCE_MAX_AREA: Final = (525, 0, 615, 20)
_MAIN_RESOURCE_OIL_AREA: Final = (540, 20, 610, 54)
_MAX_TEXT_RE: Final = re.compile(r"MAX:(?P<limit>[0-9]{1,9})")
_DIGITS_RE: Final = re.compile(r"[0-9]{1,9}")
_OCR_DIGIT_REPLACEMENTS: Final = str.maketrans(
    {
        "I": "1",
        "D": "0",
        "S": "5",
        "B": "8",
    }
)


def _read_text(
    image: object, area: tuple[int, int, int, int], *, alphabet: str, name: str
) -> str:
    """Распознать одну область без fallback в числовой ноль."""

    from module.base.button import Button
    from module.ocr.ocr import Ocr

    button = Button(
        area=area,
        color=(0, 0, 0),
        button=(),
        name=name,
    )
    result = Ocr(button, alphabet=alphabet, name=name).ocr(image)
    if not isinstance(result, str):
        raise OperationFailedError("OCR ресурса вернул неподдерживаемый результат.")
    return result


def _validate_frame(image: object) -> None:
    shape = getattr(image, "shape", None)
    if not isinstance(shape, tuple) or len(shape) != 3:
        raise OperationFailedError("Свежий экран имеет неподдерживаемый формат.")
    if shape[0] != _FRAME_HEIGHT or shape[1] != _FRAME_WIDTH:
        raise OperationFailedError(
            "Свежий экран не нормализован к поддерживаемому Global/EN layout."
        )


def read_main_oil_snapshot(image: object) -> tuple[int, int]:
    """Вернуть Oil и displayed MAX только после подтверждения Main/Home anchor.

    Обе величины читаются из одного переданного кадра. Неподдерживаемый экран,
    отсутствие anchor или пустой OCR-результат приводят к отказу, а не к
    синтетическому ``0``.
    """

    _validate_frame(image)

    anchor = _read_text(
        image,
        _MAIN_RESOURCE_MAX_AREA,
        alphabet="MAX:0123456789",
        name="MAIN_RESOURCE_MAX_ANCHOR",
    )
    anchor = re.sub(r"\s+", "", anchor).upper()
    match = _MAX_TEXT_RE.fullmatch(anchor)
    if match is None:
        raise OperationFailedError("Main/Home resource bar anchor не подтверждён.")
    limit = int(match.group("limit"))

    value_text = _read_text(
        image,
        _MAIN_RESOURCE_OIL_AREA,
        alphabet="0123456789IDSB",
        name="MAIN_RESOURCE_OIL_VALUE",
    )
    value_text = (
        re.sub(r"\s+", "", value_text).upper().translate(_OCR_DIGIT_REPLACEMENTS)
    )
    if _DIGITS_RE.fullmatch(value_text) is None:
        raise OperationFailedError("Main/Home resource bar не подтвердил числовой Oil.")
    return int(value_text), limit


__all__ = ["read_main_oil_snapshot"]
