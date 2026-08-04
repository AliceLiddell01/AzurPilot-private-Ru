from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import numpy as np

REAL_MODEL_NAME = "azur_lane"
REAL_MODEL_VERSION = "alocr_en_v2_6"
REAL_FIXTURE_LIMIT = 16
PACKAGE_NAMES = (
    "rapidocr",
    "onnxruntime",
    "ncnn",
    "numpy",
    "opencv-python",
)


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head(source_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in PACKAGE_NAMES:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return repr(value)


def _find_archive(root: Path, prefix: str = "sets_num") -> Path:
    for suffix in (".zip", ".tar", ".tar.xz", ".tar.gz"):
        candidate = root / "module" / "daemon" / f"{prefix}{suffix}"
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"Bundled OCR fixture archive not found: {prefix}")


def _load_cases(extract_dir: Path, subfolder: str = "sets_num") -> list[tuple[Path, str]]:
    validation = extract_dir / "val.txt"
    if not validation.is_file():
        validation = extract_dir / subfolder / "val.txt"
    if not validation.is_file():
        raise RuntimeError(f"Fixture val.txt not found under {extract_dir}")
    root = validation.parent
    cases: list[tuple[Path, str]] = []
    for line in validation.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        image = root / parts[0]
        if not image.is_file():
            image = root / "imgs" / parts[0]
        if image.is_file():
            cases.append((image, parts[1]))
    if not cases:
        raise RuntimeError("Bundled OCR fixture dataset is empty")
    return sorted(cases, key=lambda row: (row[1], row[0].as_posix()))


def _select_cases(cases: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    selected: list[tuple[Path, str]] = []
    predicates = (
        lambda value: "/" in value,
        lambda value: ":" in value,
        lambda value: "-" in value,
        lambda value: value.isdigit(),
        lambda value: any(character.isalpha() for character in value),
    )
    for predicate in predicates:
        match = next((row for row in cases if predicate(row[1]) and row not in selected), None)
        if match is not None:
            selected.append(match)
    for row in cases:
        if row not in selected:
            selected.append(row)
        if len(selected) >= REAL_FIXTURE_LIMIT:
            break
    return selected


def _session_evidence(model: Any) -> dict[str, Any]:
    session = getattr(model, "session", None)
    if session is None:
        nested = getattr(model, "text_rec", None)
        wrapper = getattr(nested, "session", None)
        session = getattr(wrapper, "session", wrapper)
    if session is None:
        return {"providers": [], "provider_options": {}}
    try:
        providers = list(session.get_providers())
    except Exception:  # noqa: BLE001 - optional provider diagnostics.
        providers = []
    try:
        options = session.get_provider_options()
    except Exception:  # noqa: BLE001 - optional provider diagnostics.
        options = {}
    return {"providers": providers, "provider_options": _jsonable(options)}


def _build_detection_canvas(images: list[np.ndarray]) -> np.ndarray:
    import cv2

    normalized: list[np.ndarray] = []
    target_width = max(image.shape[1] for image in images)
    for image in images:
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        scale = min(1.0, 500.0 / max(1, image.shape[1]))
        if scale != 1.0:
            image = cv2.resize(image, None, fx=scale, fy=scale)
        left = 20
        right = max(20, target_width - image.shape[1] + 20)
        normalized.append(
            cv2.copyMakeBorder(
                image,
                20,
                20,
                left,
                right,
                cv2.BORDER_CONSTANT,
                value=(0, 0, 0),
            )
        )
    return np.vstack(normalized)


def build_probe(source_root: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    sys.path.insert(0, str(source_root))
    os.chdir(source_root)
    os.environ.update(
        {
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "AZURPILOT_OCR_DEBUG": "0",
            "AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD": "0",
        }
    )
    random.seed(0)
    np.random.seed(0)

    import cv2
    import onnxruntime as ort

    from module.ocr import al_ocr, ocr as ocr_module
    normalize_ocr_text = getattr(
        ocr_module,
        "normalize_ocr_text",
        lambda _model_name, text: text,
    )

    config = SimpleNamespace(
        ocr_backend="onnx",
        ocr_device="cpu",
        Optimization_OcrWindowsMlVendorEp=False,
        ocr_model_version=lambda name: (
            REAL_MODEL_VERSION
            if name == REAL_MODEL_NAME
            else (_ for _ in ()).throw(ValueError(name))
        ),
    )

    archive = _find_archive(source_root)
    with tempfile.TemporaryDirectory(prefix="stage8b-real-fixtures-") as directory:
        extract_dir = Path(directory) / "dataset"
        shutil.unpack_archive(archive, extract_dir)
        selected = _select_cases(_load_cases(extract_dir))
        fixture_rows = [
            {
                "path": image.relative_to(extract_dir).as_posix(),
                "expected": expected,
                "sha256": _sha256(image),
            }
            for image, expected in selected
        ]

        with patch.object(al_ocr, "config", config):
            al_ocr.release_ocr_models()
            engine = al_ocr.AlOcr(name=REAL_MODEL_NAME)
            engine.init()
            recognition: list[dict[str, Any]] = []
            loaded_images: list[np.ndarray] = []
            try:
                for image_path, expected in selected:
                    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
                    if image is None:
                        raise RuntimeError(f"OpenCV failed to load fixture: {image_path}")
                    loaded_images.append(image)
                    output = engine.model(image)
                    texts = list(getattr(output, "txts", None) or ())
                    scores = [float(value) for value in (getattr(output, "scores", None) or ())]
                    raw_text = texts[0] if texts else ""
                    recognition.append(
                        {
                            "fixture_sha256": _sha256(image_path),
                            "expected": expected,
                            "raw_text": raw_text,
                            "normalized_text": normalize_ocr_text(REAL_MODEL_NAME, raw_text),
                            "scores": scores,
                            "word_results": _jsonable(getattr(output, "word_results", None)),
                            "elapse": _jsonable(getattr(output, "elapse", None)),
                        }
                    )

                canvas = _build_detection_canvas(loaded_images[: min(4, len(loaded_images))])
                detection = [
                    {"text": text, "box": box, "score": score}
                    for text, box, score in engine.det(canvas)
                ]
                provider = _session_evidence(engine.model)
            finally:
                al_ocr.release_ocr_models()

    model_path, dictionary_path, _ocr_version = al_ocr.ONNX_MODEL_PARAMS[REAL_MODEL_NAME][REAL_MODEL_VERSION]
    detector_path = al_ocr.DET_MODEL_PATH
    registered = list(ort.get_available_providers())
    return {
        "source_sha": _git_head(source_root),
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "architecture": platform.machine(),
            "packages": _versions(),
            "thread_environment": {
                key: os.environ.get(key)
                for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
            },
            "random_seed": 0,
        },
        "model": {
            "name": REAL_MODEL_NAME,
            "version": REAL_MODEL_VERSION,
            "backend": "onnx",
            "device": "cpu",
            "model_path": model_path,
            "dictionary_path": dictionary_path,
            "detector_path": detector_path,
            "hashes": {
                "model": _sha256(source_root / model_path),
                "dictionary": _sha256(source_root / dictionary_path),
                "detector": _sha256(source_root / detector_path),
                "fixture_archive": _sha256(archive),
            },
        },
        "providers": {
            "requested": ["CPUExecutionProvider"],
            "registered": registered,
            "session": provider["providers"],
            "session_options": provider["provider_options"],
        },
        "fixtures": fixture_rows,
        "recognition": recognition,
        "detection": detection,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_probe(args.source_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
