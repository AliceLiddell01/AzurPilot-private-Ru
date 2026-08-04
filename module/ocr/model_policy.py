"""Политика OCR-моделей персонального EN/Global-форка.

В пользовательском контуре поддерживается только международная английская
версия Azur Lane. Китайские, японские и традиционно-китайские OCR-веса не
поставляются и не показываются в WebUI.
"""

from __future__ import annotations

ENGLISH_OCR_MODEL_NAME = "azur_lane"
GENERIC_ENGLISH_MODEL_NAME = "ppocr_v6"

ENGLISH_ONNX_MODEL_VERSIONS = (
    "alocr_en_900k",
    "azur_lane_v6_6",
    "azur_lane_v6_5",
    "ppocr_v6",
    "alocr_en_v2_6",
    "alocr_en_v2_0",
    "alocr_en_v1_0",
)

SUPPORTED_RUNTIME_MODEL_NAMES = frozenset(
    {
        ENGLISH_OCR_MODEL_NAME,
        GENERIC_ENGLISH_MODEL_NAME,
    }
)

HIDDEN_PERSONAL_OCR_ARGUMENTS = frozenset(
    {
        "OcrModelVersionChinese",
        "OcrModelVersionJapanese",
        "OcrModelVersionTraditionalChinese",
    }
)

REMOVED_MODEL_NAMES = frozenset(
    {
        "azur_lane_jp",
        "cn",
        "jp",
        "tw",
    }
)


def is_supported_runtime_model(name: str) -> bool:
    """Возвращает ``True`` только для моделей EN/Global-контура."""
    return name in SUPPORTED_RUNTIME_MODEL_NAMES


def should_hide_personal_ocr_argument(
    *,
    task: str,
    group: str,
    argument: str,
) -> bool:
    """Определяет, нужно ли скрыть языковой OCR-параметр в WebUI."""
    return (
        task == "Alas"
        and group == "Optimization"
        and argument in HIDDEN_PERSONAL_OCR_ARGUMENTS
    )
