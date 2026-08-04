from __future__ import annotations

import hashlib
import os
import shutil
import stat
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


def _is_reparse_point(path: Path) -> bool:
    """Detect symlinks and Windows junction/reparse points without following them."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    if os.name == "nt" and path.exists():
        try:
            attributes = path.lstat().st_file_attributes
        except (AttributeError, OSError):
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attributes & reparse_flag:
            return True
    return False


def _reject_existing_reparse_components(path: Path) -> None:
    current = path
    while True:
        if current.exists() and _is_reparse_point(current):
            raise OcrDebugOutputError(
                "Каталог отладочных OCR-изображений не должен проходить "
                "через символическую ссылку, junction или reparse point."
            )
        if current == current.parent:
            break
        current = current.parent


def resolve_debug_directory(explicit: str | os.PathLike[str] | None = None) -> Path:
    configured = explicit or os.environ.get(DEBUG_DIR_ENV)
    if configured:
        original = Path(configured).expanduser()
    else:
        original = Path(tempfile.gettempdir()) / "azurpilot-ocr-debug" / str(os.getpid())

    absolute = _absolute_without_resolving(original)
    _reject_existing_reparse_components(absolute)
    candidate = absolute.resolve(strict=False)
    repository = _repository_root().resolve()
    if _is_relative_to(candidate, repository):
        raise OcrDebugOutputError(
            "Каталог отладочных OCR-изображений не должен находиться внутри Git-репозитория."
        )
    return candidate


def _secure_directory(directory: Path) -> None:
    absolute = _absolute_without_resolving(directory)
    _reject_existing_reparse_components(absolute)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_existing_reparse_components(absolute)
    if _is_reparse_point(directory) or directory.resolve(strict=True) != absolute:
        raise OcrDebugOutputError(
            "Каталог отладочных OCR-изображений был перенаправлен через "
            "символическую ссылку, junction или reparse point."
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
        input_path = Path(image)
        if _is_reparse_point(input_path):
            raise OcrDebugOutputError(
                "Входное OCR-изображение не должно быть ссылкой или reparse point."
            )
        array = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
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
    _reject_existing_reparse_components(_absolute_without_resolving(directory))
    files: list[Path] = []
    for entry in directory.iterdir():
        if _is_reparse_point(entry):
            raise OcrDebugOutputError(
                "В каталоге OCR debug обнаружена ссылка или reparse point; очистка остановлена."
            )
        if entry.is_file() and entry.suffix.lower() == ".png":
            files.append(entry)
    files.sort(key=lambda entry: entry.stat().st_mtime_ns)
    for entry in files[:-retention]:
        if _is_reparse_point(entry):
            raise OcrDebugOutputError("Отказано в удалении reparse-point OCR-файла.")
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
    safe_model = "".join(
        character for character in model_name if character.isalnum() or character in "_-"
    )
    safe_kind = "".join(
        character for character in kind if character.isalnum() or character in "_-"
    )
    safe_model = safe_model or "model"
    safe_kind = safe_kind or "ocr"
    filename = f"{safe_kind}_{safe_model}_{time.time_ns()}_{digest}.png"
    path = target / filename
    if path.exists() or _is_reparse_point(path):
        raise OcrDebugOutputError("Целевой OCR debug-файл уже существует или перенаправлен.")

    descriptor, temporary_name = tempfile.mkstemp(prefix=".ocr-", suffix=".png", dir=target)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if _is_reparse_point(temporary):
            raise OcrDebugOutputError("Временный OCR-файл оказался reparse point.")
        if not cv2.imwrite(str(temporary), array):
            raise OcrDebugOutputError("OpenCV не смог сохранить отладочное OCR-изображение.")
        _reject_existing_reparse_components(_absolute_without_resolving(target))
        if _is_reparse_point(temporary) or path.exists() or _is_reparse_point(path):
            raise OcrDebugOutputError("OCR debug path изменился перед atomic publish.")
        os.replace(temporary, path)
        if _is_reparse_point(path):
            path.unlink(missing_ok=True)
            raise OcrDebugOutputError("Опубликованный OCR debug-файл стал reparse point.")
    finally:
        temporary.unlink(missing_ok=True)

    _bounded_cleanup(target, retention)
    return path


def cleanup_debug_directory(directory: str | os.PathLike[str]) -> None:
    target = resolve_debug_directory(directory)
    if not target.exists():
        return
    absolute = _absolute_without_resolving(target)
    _reject_existing_reparse_components(absolute)
    if _is_reparse_point(target) or target.resolve(strict=True) != absolute:
        raise OcrDebugOutputError(
            "Отказано в удалении каталога OCR, перенаправленного ссылкой, "
            "junction или reparse point."
        )
    for entry in target.rglob("*"):
        if _is_reparse_point(entry):
            raise OcrDebugOutputError(
                "В каталоге OCR debug обнаружен reparse point; рекурсивное удаление запрещено."
            )
    shutil.rmtree(target)
