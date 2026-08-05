"""Semantic routing for the Global/English OCR contour.

The compact Azur Lane recognizer remains the default public ``azur_lane``
model. The bundled PP-OCRv6 recognizer is selected only for audited runtime
contours that historically requested a general OCR model because they contain
natural English text or unsupported UI fonts.
"""

from __future__ import annotations

import re
from typing import Any

import cv2
import numpy as np

from module.logger import logger
from module.ocr.al_ocr import (
    DET_MODEL_PATH,
    GENERIC_PPOCR_V6_PARAMS,
    AlOcr,
    RapidOCR,
    RapidOCROutput,
    RecOnlyOCR,
    _configure_windows_ml_sessions,
    config,
)

GENERAL_ENGLISH_MODEL_NAME = "english_text"
_DIGIT_CONFUSION_LETTERS = frozenset("IDSB")
_GENERAL_OCR_NAMES = frozenset(
    {
        "COMMISSION",
        "ENEMY_NAME",
        "OCR_ACTION_POINT_BUY_REMAIN",
        "OCR_EVENT_SHOP_DEADLINE",
        "OCR_OS_ADAPTABILITY",
        "OCR_OS_MAP_NAME",
        "OCR_PT",
        "OCR_TRANSPORT_TIME",
        "SKILL_LEVEL",
        "pearl_current_count",
        "pearl_price",
        "pearl_rank_price",
        "pearl_trade_count",
        "pearl_weekly_purchase",
    }
)
_GENERAL_OCR_NAME_PREFIXES = ("TEXT_POS", "pearl_rank_price")
_GENERAL_OCR_TYPES = frozenset({"RaidCounter", "RaidCounterPostMixin"})
_TRAILING_ROMAN_RE = re.compile(r"(?<![A-Z])(VI|IV|V|III|II|I)$", re.IGNORECASE)
_ROMAN_COMPONENT_MAP = {
    ("I",): "I",
    ("I", "I"): "II",
    ("I", "I", "I"): "III",
    ("I", "V"): "IV",
    ("V",): "V",
    ("V", "I"): "VI",
}


def should_use_general_english(
    alphabet: str | None,
    *,
    name: str | None = None,
    recognizer_type: str | None = None,
    direct: bool = False,
) -> bool:
    """Return whether an audited request needs the general English model.

    ``direct`` is reserved for explicit direct-model callers. Normal Ocr
    wrappers are routed by their stable OCR name or recognizer class, keeping
    all unlisted ``azur_lane`` callsites on their previous compact model.
    """

    if direct:
        if alphabet is None:
            return False
        letters = {char.upper() for char in alphabet if char.isalpha()}
        return bool(letters - _DIGIT_CONFUSION_LETTERS)

    normalized_name = str(name or "")
    if normalized_name in _GENERAL_OCR_NAMES:
        return True
    if any(normalized_name.startswith(prefix) for prefix in _GENERAL_OCR_NAME_PREFIXES):
        return True
    return str(recognizer_type or "") in _GENERAL_OCR_TYPES


def _roman_suffix_from_preprocessed(image: Any) -> str | None:
    """Classify a detached trailing Roman numeral I..VI from OCR input pixels.

    ``Ocr`` passes an ``extract_letters``/``crop_to_text`` image here: dark text
    on a light background. The classifier is intentionally narrow. It only
    accepts one detached final glyph group no wider than 20 pixels whose
    connected components match the game's simple I/V font geometry.
    """

    array = np.asarray(image)
    if array.ndim == 3:
        array = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    if array.ndim != 2 or array.size == 0:
        return None
    array = np.clip(array, 0, 255).astype(np.uint8, copy=False)

    # Dark text becomes white foreground for connected-component analysis.
    _, foreground = cv2.threshold(
        array,
        0,
        255,
        cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
    )
    min_column_pixels = max(2, int(round(array.shape[0] * 0.15)))
    columns = np.flatnonzero(
        np.count_nonzero(foreground, axis=0) >= min_column_pixels
    )
    if columns.size == 0:
        return None

    gaps = np.diff(columns)
    word_breaks = np.flatnonzero(gaps >= 6)
    left = int(columns[word_breaks[-1] + 1]) if word_breaks.size else int(columns[0])
    right = int(columns[-1]) + 1
    if right - left > 20:
        return None

    tail = foreground[:, left:right]
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        tail,
        connectivity=8,
    )
    components = []
    # Commission crops may include a taller decorative/chibi fragment on the
    # far left. Cap the reference height so 11-12 px Roman strokes are not
    # rejected merely because unrelated pixels make the full crop 30 px tall.
    reference_height = min(array.shape[0], 24)
    min_height = max(7, int(round(reference_height * 0.35)))
    for x, _y, width, height, area in stats[1:count]:
        if area < 8 or height < min_height:
            continue
        components.append((int(x), int(width), int(height), int(area)))
    components.sort(key=lambda item: item[0])
    if not 1 <= len(components) <= 3:
        return None

    shapes: list[str] = []
    for _x, width, height, area in components:
        if width <= 4 and area <= height * 4:
            shapes.append("I")
        elif 6 <= width <= 12 and area <= height * 8:
            shapes.append("V")
        else:
            return None
    return _ROMAN_COMPONENT_MAP.get(tuple(shapes))


def reconcile_trailing_roman_suffix(text: str, image: Any) -> str:
    """Replace a collapsed OCR Roman suffix with the observed glyph sequence."""

    normalized = str(text or "")
    observed = _roman_suffix_from_preprocessed(image)
    if observed is None:
        return normalized

    match = _TRAILING_ROMAN_RE.search(normalized.rstrip())
    if match is None:
        return normalized
    recognized = match.group(1).upper()
    if recognized == observed:
        return normalized

    corrected = normalized[: match.start(1)] + observed
    logger.info(
        "[OCR] Исправлен римский суффикс по геометрии: %s -> %s",
        normalized,
        corrected,
    )
    return corrected


def _general_recognition_model() -> Any:
    model_path, rec_keys_path, ocr_version = GENERIC_PPOCR_V6_PARAMS
    ocr_device = config.ocr_device
    params = {
        "Global.use_det": False,
        "Global.use_cls": False,
        "Det.model_path": None,
        "Cls.model_path": None,
        "Rec.ocr_version": ocr_version,
        "Rec.model_path": model_path,
        "Rec.rec_keys_path": rec_keys_path,
        "EngineConfig.onnxruntime.use_dml": False,
        "EngineConfig.onnxruntime.use_coreml": ocr_device == "ane",
        "EngineConfig.onnxruntime.coreml_ep_cfg.MLComputeUnits": "CPUAndNeuralEngine",
    }
    ocr = RecOnlyOCR(params=params)
    return _configure_windows_ml_sessions(
        ocr,
        [("Rec", "text_rec", model_path)],
        ocr_device,
        config.Optimization_OcrWindowsMlVendorEp,
    )


def _general_detection_model() -> Any:
    model_path, rec_keys_path, ocr_version = GENERIC_PPOCR_V6_PARAMS
    ocr_device = config.ocr_device
    params = {
        "Global.use_det": True,
        "Global.use_cls": False,
        "Det.model_path": DET_MODEL_PATH,
        "Cls.model_path": None,
        "Rec.ocr_version": ocr_version,
        "Rec.model_path": model_path,
        "Rec.rec_keys_path": rec_keys_path,
        "EngineConfig.onnxruntime.use_dml": False,
        "EngineConfig.onnxruntime.use_coreml": ocr_device == "ane",
        "EngineConfig.onnxruntime.coreml_ep_cfg.MLComputeUnits": "CPUAndNeuralEngine",
    }
    ocr = RapidOCR(params=params)
    return _configure_windows_ml_sessions(
        ocr,
        [("Det", "text_det", DET_MODEL_PATH), ("Rec", "text_rec", model_path)],
        ocr_device,
        config.Optimization_OcrWindowsMlVendorEp,
    )


class GeneralEnglishOcr(AlOcr):
    """Fixed PP-OCRv6 recognizer for audited general-English requests."""

    def __init__(self) -> None:
        super().__init__(name=GENERAL_ENGLISH_MODEL_NAME)

    def init(self) -> None:
        self.model = _general_recognition_model()
        self._model_loaded = True

    def atomic_ocr_for_single_lines(self, img_list, cand_alphabet=None):
        results = super().atomic_ocr_for_single_lines(img_list, cand_alphabet)
        if cand_alphabet:
            return results
        return [
            reconcile_trailing_roman_suffix(text, image)
            for text, image in zip(results, img_list)
        ]

    def _ensure_det_loaded(self) -> None:
        if not self._det_loaded:
            self._det_model = _general_detection_model()
            self._det_loaded = True

    def _det_direct(self, img_fp):
        self._ensure_det_loaded()
        try:
            result = self._det_model(img_fp, use_det=True, use_rec=True)
            if not isinstance(result, RapidOCROutput) or result.boxes is None:
                return []

            txts = result.txts if result.txts is not None else ("",) * len(result.boxes)
            scores = result.scores if result.scores is not None else (0.0,) * len(result.boxes)
            rows = [
                (text, box.tolist(), float(score))
                for box, text, score in zip(result.boxes, txts, scores)
                if str(text).strip()
            ]
            if rows:
                self._save_det_debug(img_fp, rows)
            return rows
        except Exception as exc:
            logger.error(f"Ошибка GeneralEnglishOcr.det: {exc}")
            raise


class GlobalEnglishOcr:
    """Public EN/Global OCR facade with constrained semantic routing."""

    name = "azur_lane"

    def __init__(self) -> None:
        self.compact = AlOcr(name="azur_lane")
        self.text = GeneralEnglishOcr()

    def for_request(
        self,
        alphabet: str | None,
        *,
        name: str | None = None,
        recognizer_type: str | None = None,
    ):
        use_text = should_use_general_english(
            alphabet,
            name=name,
            recognizer_type=recognizer_type,
        )
        return self.text if use_text else self.compact

    def for_alphabet(self, alphabet: str | None):
        use_text = should_use_general_english(alphabet, direct=True)
        return self.text if use_text else self.compact

    def ocr(self, img_fp):
        return self.compact.ocr(img_fp)

    def ocr_for_single_line(self, img_fp):
        return self.compact.ocr_for_single_line(img_fp)

    def ocr_for_single_lines(self, img_list):
        return self.compact.ocr_for_single_lines(img_list)

    def atomic_ocr(self, img_fp, cand_alphabet=None):
        return self.for_alphabet(cand_alphabet).atomic_ocr(img_fp, cand_alphabet)

    def atomic_ocr_for_single_line(self, img_fp, cand_alphabet=None):
        return self.for_alphabet(cand_alphabet).atomic_ocr_for_single_line(
            img_fp,
            cand_alphabet,
        )

    def atomic_ocr_for_single_lines(self, img_list, cand_alphabet=None):
        return self.for_alphabet(cand_alphabet).atomic_ocr_for_single_lines(
            img_list,
            cand_alphabet,
        )

    def det(self, img_fp):
        return self.text.det(img_fp)

    def debug(self, img_list):
        return self.text.debug(img_list)

    def set_cand_alphabet(self, cand_alphabet):
        self.compact.set_cand_alphabet(cand_alphabet)
        self.text.set_cand_alphabet(cand_alphabet)


GLOBAL_ENGLISH_OCR = GlobalEnglishOcr()
