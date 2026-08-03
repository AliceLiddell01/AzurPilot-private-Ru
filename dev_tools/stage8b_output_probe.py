from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

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


def build_probe(source_root: Path) -> dict[str, object]:
    sys.path.insert(0, str(source_root))
    os.chdir(source_root)

    from module.campaign.campaign_ocr import CampaignOcr
    from module.ocr.al_ocr import AlOcrCtcRecOCR
    from module.ocr.ncnn_ocr import NcnnRecOCR
    from module.ocr.ocr import Digit, DigitCounter, Duration
    from module.ocr.windows_ml import _vendor_execution_provider_names, _video_memory_mib

    digit = Digit.__new__(Digit)
    counter = DigitCounter.__new__(DigitCounter)
    ncnn = NcnnRecOCR.__new__(NcnnRecOCR)
    ncnn.class_count = 4
    ctc = AlOcrCtcRecOCR.__new__(AlOcrCtcRecOCR)
    ctc.charset = "ABC"
    ctc.blank_id = 0

    logits = np.array(
        [
            [0.0, 8.0, 0.0, 0.0], [0.0, 8.0, 0.0, 0.0],
            [8.0, 0.0, 0.0, 0.0], [0.0, 0.0, 8.0, 0.0],
        ],
        dtype=np.float32,
    )
    ctc_text, ctc_score = ctc._decode(logits[np.newaxis, :, :], np.array([4]))
    ncnn_input = np.arange(12, dtype=np.float32).reshape(3, 4)
    normalized = ncnn._normalize_output(ncnn_input)

    values: dict[str, object] = {
        "digit": digit.after_process("IDSB"),
        "counter": counter.after_process("I4/I5"),
        "duration_valid": Duration.parse_time("01:30:00").total_seconds(),
        "duration_compact": Duration.parse_time("013000").total_seconds(),
        "duration_invalid": Duration.parse_time("bad").total_seconds(),
        "campaign_double_hyphen": CampaignOcr._campaign_ocr_result_process("7--2"),
        "campaign_i_correction": CampaignOcr._campaign_ocr_result_process("I1-I"),
        "campaign_two_digit": CampaignOcr._campaign_ocr_result_process("72"),
        "video_memory": [
            _video_memory_mib("1024 MiB"), _video_memory_mib("2 GiB"),
            _video_memory_mib("bad"),
        ],
        "vendor_auto": list(_vendor_execution_provider_names("auto")),
        "vendor_gpu": list(_vendor_execution_provider_names("gpu")),
        "ncnn_shape": list(normalized.shape),
        "ncnn_dtype": str(normalized.dtype),
        "ncnn_values": normalized.tolist(),
        "ctc_text": ctc_text,
        "ctc_score": ctc_score,
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
