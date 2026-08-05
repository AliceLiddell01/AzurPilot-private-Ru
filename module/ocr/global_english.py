"""Semantic routing for the Global/English OCR contour.

The compact Azur Lane recognizer remains the default public ``azur_lane``
model.  The bundled PP-OCRv6 recognizer is selected only for audited runtime
contours that historically requested a general OCR model because they contain
natural English text or unsupported UI fonts.
"""

from __future__ import annotations

from typing import Any

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


def should_use_general_english(
    alphabet: str | None,
    *,
    name: str | None = None,
    recognizer_type: str | None = None,
    direct: bool = False,
) -> bool:
    """Return whether an audited request needs the general English model.

    ``direct`` is reserved for explicit direct-model callers.  Normal Ocr
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
            from module.logger import logger

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
