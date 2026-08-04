from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np

PACKAGE_NAMES = (
    "rapidocr", "onnxruntime", "onnxruntime-windowsml", "windowsml",
    "ncnn", "numpy", "opencv-python",
)
MODEL_FILES = (
    "bin/ocr_models/azur_lane/ap_azurlane-v6.6_small_rec_dcu.onnx",
    "bin/ocr_models/azur_lane/ppocrv6_azurlane_dict.txt",
    "bin/ocr_models/azur_lane/alocr-en-us-900k-w768.dml.onnx",
    "bin/ocr_models/det/PP-OCRv6_tiny_det.onnx",
)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in PACKAGE_NAMES:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def _set_fields(output_type, **fields):
    output = object.__new__(output_type)
    for name, value in fields.items():
        object.__setattr__(output, name, value)
    return output


def _model_versions(al_ocr) -> dict[str, object]:
    return {
        "defaults": dict(al_ocr.DEFAULT_ONNX_MODEL_VERSION),
        "onnx_versions": {
            name: sorted(specs)
            for name, specs in al_ocr.ONNX_MODEL_PARAMS.items()
        },
        "custom_versions": {
            name: sorted(specs)
            for name, specs in al_ocr.CUSTOM_CTC_MODEL_PARAMS.items()
        },
        "detector": al_ocr.DET_MODEL_PATH,
    }


def _detection_values(al_ocr) -> list[dict[str, object]]:
    boxes = np.array(
        [
            [[1, 1], [5, 1], [5, 5], [1, 5]],
            [[7, 7], [11, 7], [11, 11], [7, 11]],
        ],
        dtype=np.float32,
    )
    output = _set_fields(
        al_ocr.RapidOCROutput,
        boxes=boxes,
        txts=("FIRST", "SECOND"),
        scores=(0.9, 0.8),
    )
    instance = al_ocr.AlOcr.__new__(al_ocr.AlOcr)
    instance._ensure_loaded = lambda: None
    instance._ensure_det_loaded = lambda: None
    instance._save_det_debug = lambda *_args: None
    instance._det_model = lambda *_args, **_kwargs: output
    with patch.object(al_ocr.config, "ocr_backend", "onnx"):
        results = instance._det_direct(np.zeros((16, 16, 3), dtype=np.uint8))
    return [
        {"text": text, "box": box, "score": score}
        for text, box, score in results
    ]


def _constructor_defaults(ocr_module) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in ("Ocr", "Digit", "DigitCounter", "Duration"):
        signature = inspect.signature(getattr(ocr_module, name).__init__)
        result[name] = {
            parameter: repr(value.default)
            for parameter, value in signature.parameters.items()
            if parameter != "self"
        }
    return result


def build_probe(source_root: Path) -> dict[str, object]:
    sys.path.insert(0, str(source_root))
    os.chdir(source_root)

    import module.ocr.al_ocr as al_ocr
    import module.ocr.ocr as ocr_module
    from module.campaign.campaign_ocr import CampaignOcr
    from module.ocr.ncnn_ocr import NcnnRecOCR
    from module.ocr.windows_ml import _vendor_execution_provider_names, _video_memory_mib

    digit = ocr_module.Digit.__new__(ocr_module.Digit)
    counter = ocr_module.DigitCounter.__new__(ocr_module.DigitCounter)
    ncnn = NcnnRecOCR.__new__(NcnnRecOCR)
    ncnn.class_count = 4
    ctc = al_ocr.AlOcrCtcRecOCR.__new__(al_ocr.AlOcrCtcRecOCR)
    ctc.charset = "ABC"
    ctc.blank_id = 0

    logits = np.array(
        [
            [0.0, 8.0, 0.0, 0.0],
            [0.0, 8.0, 0.0, 0.0],
            [8.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 8.0, 0.0],
        ],
        dtype=np.float32,
    )
    ctc_text, ctc_score = ctc._decode(logits[np.newaxis, :, :], np.array([4]))
    ncnn_input = np.arange(12, dtype=np.float32).reshape(3, 4)
    normalized = ncnn._normalize_output(ncnn_input)

    with patch.object(al_ocr.config, "ocr_backend", "onnx"), \
         patch.object(al_ocr.config, "ocr_device", "cpu"), \
         patch.object(al_ocr.config, "Optimization_OcrWindowsMlVendorEp", False), \
         patch.object(al_ocr.config, "ocr_model_version", return_value="fixture-version"):
        cache_key = list(al_ocr._model_cache_key("azur_lane"))

    original_worker_ident = al_ocr._ocr_worker_ident
    al_ocr._ocr_worker_ident = threading.get_ident()
    try:
        queue_value = al_ocr._run_ocr_queued(lambda: "queue-ok")
    finally:
        al_ocr._ocr_worker_ident = original_worker_ident

    detection = _detection_values(al_ocr)
    values: dict[str, object] = {
        "digit": digit.after_process("IDSB"),
        "counter": counter.after_process("I4/I5"),
        "duration_valid": ocr_module.Duration.parse_time("01:30:00").total_seconds(),
        "duration_compact": ocr_module.Duration.parse_time("013000").total_seconds(),
        "duration_invalid": ocr_module.Duration.parse_time("bad").total_seconds(),
        "campaign_double_hyphen": CampaignOcr._campaign_ocr_result_process("7--2"),
        "campaign_i_correction": CampaignOcr._campaign_ocr_result_process("I1-I"),
        "campaign_two_digit": CampaignOcr._campaign_ocr_result_process("72"),
        "video_memory": [
            _video_memory_mib("1024 MiB"),
            _video_memory_mib("2 GiB"),
            _video_memory_mib("bad"),
        ],
        "provider_auto": list(_vendor_execution_provider_names("auto")),
        "provider_gpu": list(_vendor_execution_provider_names("gpu")),
        "ncnn_shape": list(normalized.shape),
        "ncnn_dtype": str(normalized.dtype),
        "ncnn_values": normalized.tolist(),
        "ctc_text": ctc_text,
        "ctc_score": ctc_score,
        "detection": detection,
        "detection_text": [item["text"] for item in detection],
        "detection_scores": [item["score"] for item in detection],
        "detection_boxes": [item["box"] for item in detection],
        "model_versions": _model_versions(al_ocr),
        "constructor_defaults": _constructor_defaults(ocr_module),
        "ctc_alphabet": al_ocr.ALAS_CTC_CHARSET,
        "ctc_blank_id": al_ocr.ALAS_CTC_BLANK_ID,
        "ctc_image_height": al_ocr.ALAS_CTC_IMAGE_HEIGHT,
        "ctc_max_width": al_ocr.ALAS_CTC_MAX_WIDTH,
        "cache_key": cache_key,
        "queue_value": queue_value,
    }
    return {
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "architecture": __import__("platform").machine(),
            "packages": _versions(),
            "model_hashes": {
                relative: _sha256(source_root / relative) for relative in MODEL_FILES
            },
            "random_seed": None,
            "thread_count": None,
        },
        "values": values,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_probe(args.source_root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
