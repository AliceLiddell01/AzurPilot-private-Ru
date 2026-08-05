from __future__ import annotations

import ipaddress
import json
import math
import re
import struct

import numpy as np

MAX_SERIALIZED_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_ELEMENTS = 1280 * 720 * 4 * 4
MAX_HEADER_BYTES = 512
_IMAGE_MAGIC = b"AZUR_OCR_IMAGE_V1\x00"
_ENDPOINT_RE = re.compile(r"^(?P<host>\[[^\]]+\]|[^:]+):(?P<port>\d{1,5})$")


class OcrRpcSecurityError(ValueError):
    """Нарушение безопасной локальной границы OCR RPC."""


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
            "OCR RPC разрешён только на loopback-адресе; wildcard и удалённые hosts запрещены."
        )

    # Сервер намеренно слушает одну IPv4 loopback-точку. Все допустимые
    # loopback-алиасы канонизируются к ней, чтобы клиент и bind не расходились.
    return f"127.0.0.1:{port}"


def loopback_bind_uri(port: int) -> str:
    if not 1 <= int(port) <= 65535:
        raise OcrRpcSecurityError("Порт OCR RPC находится вне диапазона 1–65535.")
    return f"tcp://127.0.0.1:{int(port)}"


def client_uri(address: str) -> str:
    return "tcp://" + normalize_loopback_address(address)


def _validate_image(image: np.ndarray) -> np.ndarray:
    if not isinstance(image, np.ndarray):
        raise OcrRpcSecurityError("OCR RPC ожидает numpy.ndarray.")
    if image.ndim not in (2, 3):
        raise OcrRpcSecurityError("OCR RPC изображение должно иметь 2 или 3 измерения.")
    if image.size == 0 or image.size > MAX_IMAGE_ELEMENTS:
        raise OcrRpcSecurityError("OCR RPC изображение имеет недопустимый размер.")
    if image.dtype.kind not in "buif":
        raise OcrRpcSecurityError("OCR RPC изображение имеет неподдерживаемый dtype.")
    if image.dtype.itemsize not in (1, 2, 4, 8):
        raise OcrRpcSecurityError("OCR RPC изображение имеет неподдерживаемую ширину dtype.")
    return np.ascontiguousarray(image)


def encode_image_payload(image: np.ndarray) -> bytes:
    """Кодирует ndarray без pickle и исполняемой объектной десериализации."""
    normalized = _validate_image(image)
    header = json.dumps(
        {"shape": list(normalized.shape), "dtype": normalized.dtype.str},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(header) > MAX_HEADER_BYTES:
        raise OcrRpcSecurityError("Заголовок OCR RPC payload превышает допустимый размер.")

    payload = _IMAGE_MAGIC + struct.pack("!H", len(header)) + header + normalized.tobytes()
    if len(payload) > MAX_SERIALIZED_IMAGE_BYTES:
        raise OcrRpcSecurityError("OCR RPC payload превышает допустимый размер.")
    return payload


def decode_image_payload(payload: bytes | bytearray | memoryview) -> np.ndarray:
    """Декодирует только фиксированный ndarray wire format без pickle."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise OcrRpcSecurityError("OCR RPC ожидает бинарный payload.")
    raw = bytes(payload)
    minimum_size = len(_IMAGE_MAGIC) + 2
    if len(raw) < minimum_size or len(raw) > MAX_SERIALIZED_IMAGE_BYTES:
        raise OcrRpcSecurityError("OCR RPC получил payload недопустимого размера.")
    if not raw.startswith(_IMAGE_MAGIC):
        raise OcrRpcSecurityError("OCR RPC получил payload неизвестного формата.")

    header_size = struct.unpack("!H", raw[len(_IMAGE_MAGIC):minimum_size])[0]
    if not 1 <= header_size <= MAX_HEADER_BYTES:
        raise OcrRpcSecurityError("OCR RPC получил заголовок недопустимого размера.")
    header_end = minimum_size + header_size
    if header_end > len(raw):
        raise OcrRpcSecurityError("OCR RPC получил усечённый заголовок payload.")

    try:
        header = json.loads(raw[minimum_size:header_end].decode("ascii"))
        shape = tuple(int(value) for value in header["shape"])
        dtype = np.dtype(header["dtype"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OcrRpcSecurityError("OCR RPC получил повреждённый заголовок payload.") from exc

    if len(shape) not in (2, 3) or any(value <= 0 for value in shape):
        raise OcrRpcSecurityError("OCR RPC изображение имеет недопустимую форму.")
    element_count = math.prod(shape)
    if element_count <= 0 or element_count > MAX_IMAGE_ELEMENTS:
        raise OcrRpcSecurityError("OCR RPC изображение имеет недопустимый размер.")
    if dtype.kind not in "buif" or dtype.itemsize not in (1, 2, 4, 8):
        raise OcrRpcSecurityError("OCR RPC изображение имеет неподдерживаемый dtype.")

    image_bytes = raw[header_end:]
    expected_size = element_count * dtype.itemsize
    if len(image_bytes) != expected_size:
        raise OcrRpcSecurityError("Размер OCR RPC payload не соответствует форме изображения.")

    image = np.frombuffer(image_bytes, dtype=dtype).reshape(shape)
    return image.copy()
