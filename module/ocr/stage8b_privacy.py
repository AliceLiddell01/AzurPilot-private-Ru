from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

DEBUG_ENV = "AZURPILOT_OCR_DEBUG"
DEBUG_DIR_ENV = "AZURPILOT_OCR_DEBUG_DIR"
DEFAULT_RETENTION = 100
_TRUE_VALUES = {"1", "true", "yes", "on"}


class OcrDebugOutputError(RuntimeError):
    """Ошибка безопасного сохранения отладочного OCR-изображения."""


def debug_output_enabled() -> bool:
    return os.environ.get(DEBUG_ENV, "").strip().lower() in _TRUE_VALUES


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_existing_symlink_components(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.exists() and current.is_symlink():
            raise OcrDebugOutputError(
                "Каталог отладочных OCR-изображений не должен проходить через символическую ссылку."
            )
        current = current.parent


def resolve_debug_directory(explicit: str | os.PathLike[str] | None = None) -> Path:
    configured = explicit or os.environ.get(DEBUG_DIR_ENV)
    if configured:
        original = Path(configured).expanduser()
    else:
        original = Path(tempfile.gettempdir()) / "azurpilot-ocr-debug" / str(os.getpid())

    absolute = _absolute_without_resolving(original)
    _reject_existing_symlink_components(absolute)
    candidate = absolute.resolve(strict=False)
    repository = _repository_root().resolve()
    if _is_relative_to(candidate, repository):
        raise OcrDebugOutputError(
            "Каталог отладочных OCR-изображений не должен находиться внутри Git-репозитория."
        )
    return candidate


def _secure_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_existing_symlink_components(_absolute_without_resolving(directory))
    if directory.is_symlink() or directory.resolve(strict=True) != directory:
        raise OcrDebugOutputError(
            "Каталог отладочных OCR-изображений не должен быть символической ссылкой."
        )
    try:
        directory.chmod(0o700)
    except OSError:
        pass


def _image_array(image: Any) -> np.ndarray:
    if isinstance(image, np.ndarray):
        array = image
    elif isinstance(image, Image.Image):
        rgb = np.asarray(image.convert("RGB"))
        array = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    elif isinstance(image, (str, os.PathLike)):
        array = cv2.imread(str(image), cv2.IMREAD_UNCHANGED)
        if array is None:
            raise OcrDebugOutputError("Не удалось прочитать входное OCR-изображение.")
    else:
        raise OcrDebugOutputError(
            f"Неподдерживаемый тип отладочного OCR-изображения: {type(image).__name__}."
        )

    if array.size == 0:
        raise OcrDebugOutputError("Нельзя сохранить пустое отладочное OCR-изображение.")
    return np.ascontiguousarray(array)


def image_fingerprint(image: Any) -> str:
    array = _image_array(image)
    digest = hashlib.sha256()
    digest.update(str(array.shape).encode("ascii"))
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def _bounded_cleanup(directory: Path, retention: int) -> None:
    if retention < 1:
        raise ValueError("Retention должен быть положительным числом.")
    files = sorted(
        (entry for entry in directory.iterdir() if entry.is_file() and entry.suffix == ".png"),
        key=lambda entry: entry.stat().st_mtime_ns,
    )
    for entry in files[:-retention]:
        entry.unlink(missing_ok=True)


def save_debug_image(
    image: Any,
    *,
    model_name: str,
    kind: str = "rec",
    directory: str | os.PathLike[str] | None = None,
    retention: int = DEFAULT_RETENTION,
) -> Path | None:
    """Сохраняет crop только при явном opt-in и не включает OCR-текст в имя файла."""
    if not debug_output_enabled():
        return None

    target = resolve_debug_directory(directory)
    _secure_directory(target)

    array = _image_array(image)
    digest = image_fingerprint(array)[:16]
    safe_model = "".join(character for character in model_name if character.isalnum() or character in "_-")
    safe_kind = "".join(character for character in kind if character.isalnum() or character in "_-")
    safe_model = safe_model or "model"
    safe_kind = safe_kind or "ocr"
    filename = f"{safe_kind}_{safe_model}_{time.time_ns()}_{digest}.png"
    path = target / filename

    descriptor, temporary_name = tempfile.mkstemp(prefix=".ocr-", suffix=".png", dir=target)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if not cv2.imwrite(str(temporary), array):
            raise OcrDebugOutputError("OpenCV не смог сохранить отладочное OCR-изображение.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

    _bounded_cleanup(target, retention)
    return path


def cleanup_debug_directory(directory: str | os.PathLike[str]) -> None:
    target = resolve_debug_directory(directory)
    if not target.exists():
        return
    _reject_existing_symlink_components(_absolute_without_resolving(target))
    if target.is_symlink():
        raise OcrDebugOutputError(
            "Отказано в удалении каталога OCR, перенаправленного символической ссылкой."
        )
    shutil.rmtree(target)
