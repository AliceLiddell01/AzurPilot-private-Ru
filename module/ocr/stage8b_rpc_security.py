from __future__ import annotations

import ipaddress
import pickle
import re
from typing import Any

import numpy as np

MAX_SERIALIZED_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_ELEMENTS = 1280 * 720 * 4 * 4
_ENDPOINT_RE = re.compile(r"^(?P<host>\[[^\]]+\]|[^:]+):(?P<port>\d{1,5})$")


class OcrRpcSecurityError(ValueError):
    """Нарушение локальной границы доверия OCR RPC."""


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def normalize_loopback_address(address: str, *, default_port: int = 22268) -> str:
    value = str(address or "").strip()
    if not value:
        value = f"127.0.0.1:{default_port}"
    match = _ENDPOINT_RE.fullmatch(value)
    if match is None:
        raise OcrRpcSecurityError("Адрес OCR RPC должен иметь формат loopback-host:port.")

    host = match.group("host")
    port = int(match.group("port"))
    if not 1 <= port <= 65535:
        raise OcrRpcSecurityError("Порт OCR RPC находится вне диапазона 1–65535.")
    if not _is_loopback_host(host):
        raise OcrRpcSecurityError(
            "OCR RPC с pickle разрешён только на loopback-адресе; wildcard и удалённые hosts запрещены."
        )
    canonical_host = "::1" if host.strip("[]").lower() == "::1" else "127.0.0.1"
    if canonical_host == "::1":
        return f"[::1]:{port}"
    return f"{canonical_host}:{port}"


def loopback_bind_uri(port: int) -> str:
    if not 1 <= int(port) <= 65535:
        raise OcrRpcSecurityError("Порт OCR RPC находится вне диапазона 1–65535.")
    return f"tcp://127.0.0.1:{int(port)}"


def client_uri(address: str) -> str:
    return "tcp://" + normalize_loopback_address(address)


def decode_trusted_local_image(payload: bytes | bytearray | memoryview) -> np.ndarray:
    """Декодирует legacy pickle только внутри подтверждённой loopback process boundary."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise OcrRpcSecurityError("OCR RPC ожидает бинарный serialized payload.")
    raw = bytes(payload)
    if not raw:
        raise OcrRpcSecurityError("OCR RPC получил пустой serialized payload.")
    if len(raw) > MAX_SERIALIZED_IMAGE_BYTES:
        raise OcrRpcSecurityError("OCR RPC payload превышает допустимый размер.")

    try:
        image: Any = pickle.loads(raw)
    except Exception as exc:
        raise OcrRpcSecurityError("OCR RPC получил повреждённый serialized payload.") from exc

    if not isinstance(image, np.ndarray):
        raise OcrRpcSecurityError("OCR RPC payload не содержит numpy.ndarray.")
    if image.ndim not in (2, 3):
        raise OcrRpcSecurityError("OCR RPC изображение должно иметь 2 или 3 измерения.")
    if image.size == 0 or image.size > MAX_IMAGE_ELEMENTS:
        raise OcrRpcSecurityError("OCR RPC изображение имеет недопустимый размер.")
    if image.dtype.kind not in "buif":
        raise OcrRpcSecurityError("OCR RPC изображение имеет неподдерживаемый dtype.")
    return image
