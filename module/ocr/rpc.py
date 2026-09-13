"""Безопасный loopback RPC-транспорт для OCR."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

import numpy as np
import zmq

from module.logger import logger
from module.ocr.rpc_security import (
    OcrRpcSecurityError,
    client_uri,
    decode_image_payload,
    encode_image_payload,
    loopback_bind_uri,
    normalize_loopback_address,
)
from module.webui.setting import State

process: multiprocessing.Process | None = None
_server_stop_event: Any = None

SUPPORTED_OCR_MODELS = frozenset({"azur_lane"})
SUPPORTED_RPC_METHODS = frozenset(
    {
        "hello",
        "ocr",
        "ocr_for_single_line",
        "ocr_for_single_lines",
        "set_cand_alphabet",
        "atomic_ocr",
        "atomic_ocr_for_single_line",
        "atomic_ocr_for_single_lines",
        "debug",
    }
)

MAX_RPC_BATCH_IMAGES = 64
MAX_RPC_BATCH_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_ALPHABET_LENGTH = 8192
MAX_RPC_CONTROL_BYTES = 64 * 1024
MAX_RPC_BINARY_FRAME_BYTES = 16 * 1024 * 1024
MAX_RPC_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_RPC_FRAMES = MAX_RPC_BATCH_IMAGES + 16
MAX_RPC_NESTING = 16
MAX_RPC_COMPOSITE_ITEMS = 1024
RPC_PROTOCOL_VERSION = 1
RPC_REQUEST_TIMEOUT_SECONDS = 5.0
RPC_STARTUP_TIMEOUT_SECONDS = 5.0
RPC_SHUTDOWN_TIMEOUT_SECONDS = 2.0


class OcrRpcProtocolError(ValueError):
    """Нарушение структуры управляющего RPC-сообщения."""


class OcrRpcTransportError(ConnectionError):
    """Недоступность loopback RPC-транспорта или истечение timeout."""


class OcrRpcRemoteError(RuntimeError):
    """Ошибка, возвращённая OCR RPC-сервером."""


def _validate_model_name(lang: str) -> str:
    if not isinstance(lang, str) or lang not in SUPPORTED_OCR_MODELS:
        raise ValueError(f"Неподдерживаемая модель OCR RPC: {lang!r}")
    return lang


def _validate_method_name(method: str) -> str:
    if not isinstance(method, str) or method not in SUPPORTED_RPC_METHODS:
        raise OcrRpcProtocolError(f"Неподдерживаемый метод OCR RPC: {method!r}")
    return method


def _validate_batch(items):
    if not isinstance(items, (list, tuple)):
        raise ValueError(  # noqa: TRY004 — сохранён публичный тип ошибки валидации
            "Пакет OCR RPC должен быть списком или кортежем."
        )
    if not 1 <= len(items) <= MAX_RPC_BATCH_IMAGES:
        raise ValueError(
            "Количество изображений OCR RPC должно быть в диапазоне "
            f"1–{MAX_RPC_BATCH_IMAGES}."
        )
    return list(items)


def _validate_candidate_alphabet(cand_alphabet):
    if cand_alphabet is None:
        return None
    if not isinstance(cand_alphabet, str):
        raise ValueError(  # noqa: TRY004 — сохранён публичный тип ошибки валидации
            "Алфавит OCR RPC должен быть строкой или None."
        )
    if len(cand_alphabet) > MAX_CANDIDATE_ALPHABET_LENGTH:
        raise ValueError(
            "Алфавит OCR RPC превышает допустимую длину "
            f"{MAX_CANDIDATE_ALPHABET_LENGTH}."
        )
    return cand_alphabet


def _encode_batch(images):
    payloads = []
    total_bytes = 0
    for image in _validate_batch(images):
        payload = encode_image_payload(image)
        total_bytes += len(payload)
        if total_bytes > MAX_RPC_BATCH_BYTES:
            raise ValueError(
                "Суммарный размер пакета OCR RPC превышает допустимый предел."
            )
        payloads.append(payload)
    return payloads


def _decode_batch(payloads):
    payloads = _validate_batch(payloads)
    if sum(len(payload) for payload in payloads) > MAX_RPC_BATCH_BYTES:
        raise ValueError("Суммарный размер пакета OCR RPC превышает допустимый предел.")
    return [decode_image_payload(payload) for payload in payloads]


def _get_server_model(container, lang):
    return getattr(container, _validate_model_name(lang))


def _get_local_model(lang):
    from module.ocr.models import OCR_MODEL

    return _get_server_model(OCR_MODEL, lang)


def _json_frame(value: Mapping[str, Any], *, limit: int) -> bytes:
    try:
        frame = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OcrRpcProtocolError(
            "OCR RPC не смог сериализовать управляющее сообщение."
        ) from exc
    if len(frame) > limit:
        raise OcrRpcProtocolError(
            "Управляющий кадр OCR RPC превышает допустимый размер."
        )
    return frame


def _reject_json_constant(_value: str) -> None:
    raise ValueError("Неподдерживаемая JSON-константа.")


def _decode_json_frame(frame: bytes, *, limit: int) -> dict[str, Any]:
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise OcrRpcProtocolError("Управляющий кадр OCR RPC должен быть бинарным.")
    raw = bytes(frame)
    if not raw or len(raw) > limit:
        raise OcrRpcProtocolError("Управляющий кадр OCR RPC имеет недопустимый размер.")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise OcrRpcProtocolError(
            "Управляющий кадр OCR RPC содержит повреждённый JSON."
        ) from exc
    if not isinstance(value, dict):
        raise OcrRpcProtocolError("Управляющий кадр OCR RPC должен быть JSON-объектом.")
    return value


def _append_binary_frame(
    payload: bytes,
    frames: list[bytes],
    *,
    kind: str,
) -> dict[str, Any]:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise OcrRpcProtocolError(
            f"Бинарное значение OCR RPC имеет неверный тип: {kind}."
        )
    raw = bytes(payload)
    if len(raw) > MAX_RPC_BINARY_FRAME_BYTES:
        raise OcrRpcProtocolError("Бинарный кадр OCR RPC превышает допустимый размер.")
    if len(frames) >= MAX_RPC_FRAMES:
        raise OcrRpcProtocolError("RPC-сообщение OCR содержит слишком много кадров.")
    index = len(frames)
    frames.append(raw)
    return {"kind": kind, "frame": index}


def _encode_wire_value(
    value: Any,
    frames: list[bytes],
    *,
    depth: int = 0,
) -> dict[str, Any]:
    if depth > MAX_RPC_NESTING:
        raise OcrRpcProtocolError("Значение OCR RPC имеет слишком глубокую структуру.")

    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, (str, bool, int, float)):
        return {"kind": "scalar", "value": value}
    if isinstance(value, np.ndarray):
        return _append_binary_frame(
            encode_image_payload(value),
            frames,
            kind="image",
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _append_binary_frame(bytes(value), frames, kind="bytes")
    if isinstance(value, list):
        if len(value) > MAX_RPC_COMPOSITE_ITEMS:
            raise OcrRpcProtocolError(
                "Список значения OCR RPC превышает допустимый размер."
            )
        return {
            "kind": "list",
            "items": [
                _encode_wire_value(item, frames, depth=depth + 1) for item in value
            ],
        }
    if isinstance(value, tuple):
        if len(value) > MAX_RPC_COMPOSITE_ITEMS:
            raise OcrRpcProtocolError(
                "Кортеж значения OCR RPC превышает допустимый размер."
            )
        return {
            "kind": "tuple",
            "items": [
                _encode_wire_value(item, frames, depth=depth + 1) for item in value
            ],
        }
    if isinstance(value, Mapping):
        if len(value) > MAX_RPC_COMPOSITE_ITEMS:
            raise OcrRpcProtocolError(
                "Словарь значения OCR RPC превышает допустимый размер."
            )
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise OcrRpcProtocolError("Ключ словаря OCR RPC должен быть строкой.")
            items.append([key, _encode_wire_value(item, frames, depth=depth + 1)])
        return {"kind": "dict", "items": items}
    raise OcrRpcProtocolError(
        f"Результат OCR RPC имеет неподдерживаемый тип: {type(value).__name__}."
    )


def _frame_index(descriptor: Mapping[str, Any], frames: list[bytes]) -> int:
    index = descriptor.get("frame")
    if not isinstance(index, int) or isinstance(index, bool):
        raise OcrRpcProtocolError("Индекс бинарного кадра OCR RPC имеет неверный тип.")
    if not 0 <= index < len(frames):
        raise OcrRpcProtocolError("Индекс бинарного кадра OCR RPC выходит за границы.")
    return index


def _decode_wire_value(
    descriptor: Any,
    frames: list[bytes],
    *,
    depth: int = 0,
) -> Any:
    if depth > MAX_RPC_NESTING or not isinstance(descriptor, dict):
        raise OcrRpcProtocolError("Значение OCR RPC имеет неверную структуру.")
    kind = descriptor.get("kind")
    if kind == "scalar":
        if "value" not in descriptor:
            raise OcrRpcProtocolError("Скалярное значение OCR RPC не содержит value.")
        value = descriptor.get("value")
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        raise OcrRpcProtocolError("Скалярное значение OCR RPC имеет неверный тип.")
    if kind == "image":
        return decode_image_payload(frames[_frame_index(descriptor, frames)])
    if kind == "bytes":
        return bytes(frames[_frame_index(descriptor, frames)])
    if kind in {"list", "tuple"}:
        items = descriptor.get("items")
        if not isinstance(items, list):
            raise OcrRpcProtocolError(
                "Составное значение OCR RPC имеет неверный список."
            )
        if len(items) > MAX_RPC_COMPOSITE_ITEMS:
            raise OcrRpcProtocolError(
                "Список значения OCR RPC превышает допустимый размер."
            )
        decoded = [_decode_wire_value(item, frames, depth=depth + 1) for item in items]
        return tuple(decoded) if kind == "tuple" else decoded
    if kind == "dict":
        items = descriptor.get("items")
        if not isinstance(items, list):
            raise OcrRpcProtocolError("Словарь OCR RPC имеет неверный список.")
        if len(items) > MAX_RPC_COMPOSITE_ITEMS:
            raise OcrRpcProtocolError(
                "Словарь значения OCR RPC превышает допустимый размер."
            )
        decoded = {}
        for item in items:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
            ):
                raise OcrRpcProtocolError("Словарь OCR RPC имеет неверную запись.")
            decoded[item[0]] = _decode_wire_value(
                item[1],
                frames,
                depth=depth + 1,
            )
        return decoded
    raise OcrRpcProtocolError(f"Неизвестный тип значения OCR RPC: {kind!r}.")


def _image_descriptor(image: Any, frames: list[bytes]) -> dict[str, Any]:
    if isinstance(image, (bytes, bytearray, memoryview)):
        payload = bytes(image)
    else:
        payload = encode_image_payload(image)
    return _append_binary_frame(payload, frames, kind="image")


def _batch_descriptor(images: Any, frames: list[bytes]) -> dict[str, Any]:
    items = _validate_batch(images)
    if all(isinstance(item, (bytes, bytearray, memoryview)) for item in items):
        payloads = [bytes(item) for item in items]
        if sum(len(payload) for payload in payloads) > MAX_RPC_BATCH_BYTES:
            raise ValueError(
                "Суммарный размер пакета OCR RPC превышает допустимый предел."
            )
    else:
        payloads = _encode_batch(items)
    return {
        "kind": "list",
        "items": [
            _append_binary_frame(payload, frames, kind="image") for payload in payloads
        ],
    }


def _require_arg_count(method: str, args: tuple[Any, ...], count: int) -> None:
    if len(args) != count:
        raise OcrRpcProtocolError(
            f"Метод OCR RPC {method} ожидает {count} аргумент(ов), "
            f"получено {len(args)}."
        )


def _validate_decoded_arguments(method: str, args: tuple[Any, ...]) -> None:
    expected_counts = {
        "ocr": 1,
        "ocr_for_single_line": 1,
        "ocr_for_single_lines": 1,
        "set_cand_alphabet": 1,
        "atomic_ocr": 2,
        "atomic_ocr_for_single_line": 2,
        "atomic_ocr_for_single_lines": 2,
        "debug": 1,
    }
    _require_arg_count(method, args, expected_counts[method])
    if method in {
        "ocr",
        "ocr_for_single_line",
        "atomic_ocr",
        "atomic_ocr_for_single_line",
    }:
        if not isinstance(args[0], np.ndarray):
            raise OcrRpcSecurityError("OCR RPC ожидает ndarray изображения.")
    elif method in {"ocr_for_single_lines", "atomic_ocr_for_single_lines", "debug"}:
        images = _validate_batch(args[0])
        if any(not isinstance(image, np.ndarray) for image in images):
            raise OcrRpcSecurityError(
                "OCR RPC ожидает ndarray в каждом изображении пакета."
            )
        if sum(image.nbytes for image in images) > MAX_RPC_BATCH_BYTES:
            raise OcrRpcSecurityError(
                "Размер декодированного пакета OCR RPC превышает предел."
            )
    if method in {
        "set_cand_alphabet",
        "atomic_ocr",
        "atomic_ocr_for_single_line",
        "atomic_ocr_for_single_lines",
    }:
        _validate_candidate_alphabet(args[-1])


def _encode_call(method: str, lang: str | None, args: tuple[Any, ...]):
    method = _validate_method_name(method)
    if method == "hello":
        if lang is not None or args:
            raise OcrRpcProtocolError("Метод hello не принимает модель или аргументы.")
    else:
        _validate_model_name(lang)

    frames: list[bytes] = []
    if method in {"ocr", "ocr_for_single_line"}:
        _require_arg_count(method, args, 1)
        encoded_args = [_image_descriptor(args[0], frames)]
    elif method in {"ocr_for_single_lines", "debug"}:
        _require_arg_count(method, args, 1)
        encoded_args = [_batch_descriptor(args[0], frames)]
    elif method == "set_cand_alphabet":
        _require_arg_count(method, args, 1)
        encoded_args = [
            _encode_wire_value(_validate_candidate_alphabet(args[0]), frames)
        ]
    elif method in {"atomic_ocr", "atomic_ocr_for_single_line"}:
        _require_arg_count(method, args, 2)
        encoded_args = [
            _image_descriptor(args[0], frames),
            _encode_wire_value(_validate_candidate_alphabet(args[1]), frames),
        ]
    elif method == "atomic_ocr_for_single_lines":
        _require_arg_count(method, args, 2)
        encoded_args = [
            _batch_descriptor(args[0], frames),
            _encode_wire_value(_validate_candidate_alphabet(args[1]), frames),
        ]
    else:
        encoded_args = []

    request_id = uuid.uuid4().hex
    control = _json_frame(
        {
            "version": RPC_PROTOCOL_VERSION,
            "kind": "request",
            "request_id": request_id,
            "method": method,
            "lang": lang,
            "args": encoded_args,
        },
        limit=MAX_RPC_CONTROL_BYTES,
    )
    return control, frames


def _request_id_from_frames(frames: list[bytes]) -> str:
    if not frames:
        return ""
    try:
        request_id = _decode_json_frame(
            frames[0],
            limit=MAX_RPC_CONTROL_BYTES,
        ).get("request_id")
    except OcrRpcProtocolError:
        return ""
    if isinstance(request_id, str) and 1 <= len(request_id) <= 64:
        return request_id
    return ""


def _response_frame(
    request_id: str,
    *,
    result: Any = None,
    error: Mapping[str, str] | None = None,
) -> list[bytes]:
    binary_frames: list[bytes] = []
    if error is None:
        descriptor = _encode_wire_value(result, binary_frames)
        control = {
            "version": RPC_PROTOCOL_VERSION,
            "kind": "response",
            "request_id": request_id,
            "ok": True,
            "result": descriptor,
        }
    else:
        control = {
            "version": RPC_PROTOCOL_VERSION,
            "kind": "response",
            "request_id": request_id,
            "ok": False,
            "error": dict(error),
        }
    control_frame = _json_frame(control, limit=MAX_RPC_CONTROL_BYTES)
    if (
        len(control_frame) + sum(len(frame) for frame in binary_frames)
        > MAX_RPC_RESPONSE_BYTES
    ):
        raise OcrRpcProtocolError("Ответ OCR RPC превышает допустимый размер.")
    return [control_frame, *binary_frames]


class _ZmqRpcClient:
    """Потокобезопасный клиент с отдельным DEALER-сокетом на запрос."""

    def __init__(
        self,
        address: str,
        *,
        timeout: float = RPC_REQUEST_TIMEOUT_SECONDS,
        context: zmq.Context | None = None,
    ) -> None:
        self.address = normalize_loopback_address(address)
        self.endpoint = client_uri(self.address)
        self.timeout = max(float(timeout), 0.001)
        self.context = context or zmq.Context()
        self._owns_context = context is None
        self._closed = False
        self._state_lock = threading.Lock()

    def _request(self, method: str, lang: str | None, args: tuple[Any, ...]):
        with self._state_lock:
            if self._closed:
                raise OcrRpcTransportError("Клиент OCR RPC уже закрыт.")
        control, binary_frames = _encode_call(method, lang, args)
        request_id = _request_id_from_frames([control])
        socket = None
        try:
            socket = self.context.socket(zmq.DEALER)
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.SNDTIMEO, max(int(self.timeout * 1000), 1))
            socket.setsockopt(zmq.RCVTIMEO, max(int(self.timeout * 1000), 1))
            socket.setsockopt(zmq.MAXMSGSIZE, MAX_RPC_RESPONSE_BYTES)
            socket.connect(self.endpoint)
            socket.send_multipart([control, *binary_frames])
            response_frames = socket.recv_multipart()
        except zmq.error.Again as exc:
            raise OcrRpcTransportError(
                f"Вызов OCR RPC {method} превысил timeout {self.timeout:.3g} с."
            ) from exc
        except zmq.error.ZMQError as exc:
            raise OcrRpcTransportError(
                f"Транспорт OCR RPC недоступен для метода {method}."
            ) from exc
        finally:
            if socket is not None:
                socket.close(linger=0)

        if not response_frames:
            raise OcrRpcProtocolError("OCR RPC вернул пустой ответ.")
        if len(response_frames) - 1 > MAX_RPC_FRAMES:
            raise OcrRpcProtocolError("Ответ OCR RPC содержит слишком много кадров.")
        if sum(len(frame) for frame in response_frames) > MAX_RPC_RESPONSE_BYTES:
            raise OcrRpcProtocolError("Ответ OCR RPC превышает допустимый размер.")
        response = _decode_json_frame(
            response_frames[0],
            limit=MAX_RPC_CONTROL_BYTES,
        )
        if (
            response.get("version") != RPC_PROTOCOL_VERSION
            or response.get("kind") != "response"
            or response.get("request_id") != request_id
        ):
            raise OcrRpcProtocolError("Ответ OCR RPC не соответствует запросу.")
        if response.get("ok") is not True:
            error = response.get("error")
            if not isinstance(error, dict):
                raise OcrRpcProtocolError("Ответ OCR RPC содержит неверную ошибку.")
            message = error.get("message")
            if not isinstance(message, str):
                raise OcrRpcProtocolError(
                    "Ответ OCR RPC содержит неверное описание ошибки."
                )
            raise OcrRpcRemoteError(message)
        return _decode_wire_value(response.get("result"), response_frames[1:])

    def __call__(self, method: str, lang: str, *args):
        return self._request(method, lang, args)

    def hello(self):
        return self._request("hello", None, ())

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        if self._owns_context:
            self.context.term()


class ModelProxy:
    """OCR-модель с loopback RPC и локальным fallback."""

    client: Any = None
    online = True

    @classmethod
    def init(cls, address="127.0.0.1:22268"):
        """Инициализировать клиент и проверить готовность локального сервера."""
        safe_address = normalize_loopback_address(address)
        logger.info(f"Подключение к локальному серверу OCR {safe_address}")
        cls.client = _ZmqRpcClient(safe_address)
        cls.online = True
        try:
            cls.client.hello()
            logger.info("Соединение с локальным сервером OCR установлено")
        except Exception as exc:  # noqa: BLE001 — граница транспорта переходит в fallback при любой ошибке
            cls.online = False
            logger.warning(
                f"Локальный сервер OCR недоступен; используется локальная модель: {exc}"
            )

    @classmethod
    def close(cls):
        """Закрыть клиентское соединение OCR RPC."""
        client = cls.client
        cls.client = None
        cls.online = True
        if client is not None:
            logger.info("Отключение от локального сервера OCR")
            client.close()
            logger.info("Соединение с локальным сервером OCR закрыто")

    def __init__(self, lang) -> None:
        self.lang = _validate_model_name(lang)

    def _rpc_or_fallback(self, method, fallback, args_factory):
        if self.online:
            args = args_factory()
            try:
                return self.client(method, self.lang, *args)
            except Exception as exc:  # noqa: BLE001 — граница транспорта переходит в fallback при любой ошибке
                self.online = False
                type(self).online = False
                logger.warning(
                    f"Вызов OCR RPC {method} завершился ошибкой; "
                    f"используется локальная модель: {exc}"
                )
        return fallback()

    def ocr(self, img_fp):
        """Выполнить OCR для изображения."""
        return self._rpc_or_fallback(
            "ocr",
            lambda: _get_local_model(self.lang).ocr(img_fp),
            lambda: (encode_image_payload(img_fp),),
        )

    def ocr_for_single_line(self, img_fp):
        """Выполнить OCR для изображения одной строки."""
        return self._rpc_or_fallback(
            "ocr_for_single_line",
            lambda: _get_local_model(self.lang).ocr_for_single_line(img_fp),
            lambda: (encode_image_payload(img_fp),),
        )

    def ocr_for_single_lines(self, img_list):
        """Выполнить пакетный OCR для изображений отдельных строк."""
        return self._rpc_or_fallback(
            "ocr_for_single_lines",
            lambda: _get_local_model(self.lang).ocr_for_single_lines(img_list),
            lambda: (_encode_batch(img_list),),
        )

    def set_cand_alphabet(self, cand_alphabet: str):
        """Установить набор кандидатов символов для OCR."""
        return self._rpc_or_fallback(
            "set_cand_alphabet",
            lambda: _get_local_model(self.lang).set_cand_alphabet(cand_alphabet),
            lambda: (_validate_candidate_alphabet(cand_alphabet),),
        )

    def atomic_ocr(self, img_fp, cand_alphabet=None):
        """Выполнить атомарный OCR изображения с набором кандидатов."""
        return self._rpc_or_fallback(
            "atomic_ocr",
            lambda: _get_local_model(self.lang).atomic_ocr(img_fp, cand_alphabet),
            lambda: (
                encode_image_payload(img_fp),
                _validate_candidate_alphabet(cand_alphabet),
            ),
        )

    def atomic_ocr_for_single_line(self, img_fp, cand_alphabet=None):
        """Выполнить атомарный OCR одной строки с набором кандидатов."""
        return self._rpc_or_fallback(
            "atomic_ocr_for_single_line",
            lambda: _get_local_model(self.lang).atomic_ocr_for_single_line(
                img_fp,
                cand_alphabet,
            ),
            lambda: (
                encode_image_payload(img_fp),
                _validate_candidate_alphabet(cand_alphabet),
            ),
        )

    def atomic_ocr_for_single_lines(self, img_list, cand_alphabet=None):
        """Выполнить пакетный атомарный OCR строк с набором кандидатов."""
        return self._rpc_or_fallback(
            "atomic_ocr_for_single_lines",
            lambda: _get_local_model(self.lang).atomic_ocr_for_single_lines(
                img_list,
                cand_alphabet,
            ),
            lambda: (
                _encode_batch(img_list),
                _validate_candidate_alphabet(cand_alphabet),
            ),
        )

    def debug(self, img_list):
        """Выполнить отладочный OCR для списка изображений."""
        return self._rpc_or_fallback(
            "debug",
            lambda: _get_local_model(self.lang).debug(img_list),
            lambda: (_encode_batch(img_list),),
        )


class ModelProxyFactory:
    """Ленивая фабрика прокси для поддерживаемых OCR-моделей."""

    def __getattribute__(self, __name: str, /) -> ModelProxy:
        if __name in SUPPORTED_OCR_MODELS:
            if ModelProxy.client is None:
                ModelProxy.init(address=State.deploy_config.OcrClientAddress)
            return ModelProxy(lang=__name)
        return super().__getattribute__(__name)

    def close(self):
        """Закрыть клиент OCR RPC."""
        ModelProxy.close()


def _configure_ocr_logging() -> None:
    """Настроить журналирование процесса OCR RPC."""
    logger.configure_runtime_logging(
        name="ocr-rpc",
        observability_profile=None,
        observability_component="ocr-rpc",
    )


class _OcrRpcMetrics:
    """Ограниченная агрегированная телеметрия без содержимого payload."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.successes = 0
        self.errors = 0
        self.total_duration_ms = 0.0

    def record(self, *, success: bool, duration_ms: float) -> int:
        with self._lock:
            self.requests += 1
            self.successes += int(success)
            self.errors += int(not success)
            self.total_duration_ms += duration_ms
            return self.requests

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "requests": self.requests,
                "successes": self.successes,
                "errors": self.errors,
                "total_duration_ms": round(self.total_duration_ms, 3),
            }


class _OcrRpcService:
    """Проверка, декодирование и вызов OCR-модели на стороне сервера."""

    def __init__(self, model_container) -> None:
        self.model_container = model_container
        self.metrics = _OcrRpcMetrics()

    def handle(self, request_frames: list[bytes]):
        if not request_frames:
            raise OcrRpcProtocolError("OCR RPC получил запрос без управляющего кадра.")
        if len(request_frames) - 1 > MAX_RPC_FRAMES:
            raise OcrRpcProtocolError("Запрос OCR RPC содержит слишком много кадров.")
        control = _decode_json_frame(
            request_frames[0],
            limit=MAX_RPC_CONTROL_BYTES,
        )
        if (
            control.get("version") != RPC_PROTOCOL_VERSION
            or control.get("kind") != "request"
        ):
            raise OcrRpcProtocolError("Запрос OCR RPC имеет неверную версию или тип.")
        request_id = control.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
            raise OcrRpcProtocolError("Запрос OCR RPC имеет неверный request_id.")
        method = _validate_method_name(control.get("method"))
        lang = control.get("lang")
        if method == "hello":
            if lang is not None:
                raise OcrRpcProtocolError("Метод hello не принимает модель.")
        else:
            _validate_model_name(lang)
        encoded_args = control.get("args")
        if not isinstance(encoded_args, list):
            raise OcrRpcProtocolError(
                "Запрос OCR RPC имеет неверный список аргументов."
            )
        args = [_decode_wire_value(item, request_frames[1:]) for item in encoded_args]

        if method == "hello":
            _require_arg_count(method, tuple(args), 0)
            result = "hello"
        else:
            _validate_decoded_arguments(method, tuple(args))
            model = _get_server_model(self.model_container, lang)
            result = getattr(model, method)(*args)
        return request_id, method, result


class _OcrRpcServer:
    """ROUTER-сервер pyzmq с bounded poll loop и корректной остановкой."""

    def __init__(
        self,
        port: int,
        service: _OcrRpcService,
        *,
        context: zmq.Context | None = None,
    ) -> None:
        self.endpoint = loopback_bind_uri(port)
        self.service = service
        self.context = context or zmq.Context()
        self._owns_context = context is None
        self.socket: zmq.Socket | None = None

    @staticmethod
    def _error_response(request_frames: list[bytes], error_type: str, message: str):
        return _response_frame(
            _request_id_from_frames(request_frames),
            error={"type": error_type, "message": message},
        )

    def run(self, *, stop_event=None, ready_event=None) -> bool:
        if stop_event is None:
            stop_event = threading.Event()
        socket = self.context.socket(zmq.ROUTER)
        self.socket = socket
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.MAXMSGSIZE, MAX_RPC_RESPONSE_BYTES)
        try:
            socket.bind(self.endpoint)
        except zmq.error.ZMQError as exc:
            logger.error(
                f"[OCR-RPC] Сервер OCR не смог привязаться к {self.endpoint}: {exc}"
            )
            socket.close(linger=0)
            self.socket = None
            if self._owns_context:
                self.context.term()
            return False

        if ready_event is not None:
            ready_event.set()
        logger.info(
            f"[OCR-RPC] Сервер OCR готов: transport=pyzmq endpoint={self.endpoint}"
        )

        try:
            while not stop_event.is_set():
                try:
                    events = socket.poll(100)
                except zmq.error.ZMQError as exc:
                    logger.error(f"[OCR-RPC] Ошибка poll сервера OCR: {exc}")
                    return False
                if not events:
                    continue
                try:
                    frames = socket.recv_multipart()
                except zmq.error.ZMQError as exc:
                    logger.error(f"[OCR-RPC] Ошибка чтения запроса OCR: {exc}")
                    continue
                if not frames:
                    continue
                identity, *request_frames = frames
                if sum(len(frame) for frame in request_frames) > MAX_RPC_RESPONSE_BYTES:
                    response_frames = self._error_response(
                        request_frames,
                        "validation",
                        "Запрос OCR RPC превышает допустимый размер.",
                    )
                    self.service.metrics.record(success=False, duration_ms=0.0)
                    try:
                        socket.send_multipart([identity, *response_frames])
                    except zmq.error.ZMQError as exc:
                        logger.warning(f"[OCR-RPC] Ответ OCR RPC не отправлен: {exc}")
                    continue
                started = time.perf_counter()
                method = "unknown"
                success = False
                try:
                    request_id, method, result = self.service.handle(request_frames)
                    response_frames = _response_frame(request_id, result=result)
                    success = True
                except OcrRpcSecurityError as exc:
                    response_frames = self._error_response(
                        request_frames,
                        "validation",
                        str(exc),
                    )
                except (OcrRpcProtocolError, ValueError, TypeError) as exc:
                    response_frames = self._error_response(
                        request_frames,
                        "validation",
                        str(exc),
                    )
                except Exception as exc:  # noqa: BLE001 — граница процесса изолирует ошибки модели
                    response_frames = self._error_response(
                        request_frames,
                        "server",
                        "Внутренняя ошибка сервера OCR RPC.",
                    )
                    logger.error(
                        f"[OCR-RPC] Ошибка метода {method}: {type(exc).__name__}"
                    )

                duration_ms = (time.perf_counter() - started) * 1000
                request_number = self.service.metrics.record(
                    success=success,
                    duration_ms=duration_ms,
                )
                if request_number <= 5 or request_number % 100 == 0:
                    status = "успешно" if success else "ошибка"
                    logger.info(
                        f"[OCR-RPC] Запрос №{request_number}: метод {method}; "
                        f"результат {status}; длительность {duration_ms:.1f} мс"
                    )
                try:
                    socket.send_multipart([identity, *response_frames])
                except zmq.error.ZMQError as exc:
                    logger.warning(f"[OCR-RPC] Ответ OCR RPC не отправлен: {exc}")
        except KeyboardInterrupt:
            logger.info("[OCR-RPC] Получен запрос остановки сервера OCR")
        finally:
            socket.close(linger=0)
            self.socket = None
            snapshot = self.service.metrics.snapshot()
            logger.info(
                "[OCR-RPC] Сервер OCR остановлен: "
                f"запросов={snapshot['requests']}, успешных={snapshot['successes']}, "
                f"ошибок={snapshot['errors']}"
            )
            if self._owns_context:
                self.context.term()
        return True


def start_ocr_server(port=22268, ready_event=None, stop_event=None):
    """Запустить OCR RPC-сервер только на loopback."""
    _configure_ocr_logging()

    from module.ocr.models import OcrModel

    service = _OcrRpcService(OcrModel())
    server = _OcrRpcServer(port, service)
    return server.run(stop_event=stop_event, ready_event=ready_event)


def start_ocr_server_process(port=22268):
    """Запустить OCR-сервер в отдельном процессе и дождаться bind-ready."""
    global _server_stop_event, process
    loopback_bind_uri(port)
    if alive():
        return True

    stop_event = multiprocessing.Event()
    ready_event = multiprocessing.Event()
    child = multiprocessing.Process(
        target=start_ocr_server,
        args=(port,),
        kwargs={"ready_event": ready_event, "stop_event": stop_event},
        name="AzurPilot-OCR-RPC",
    )
    _server_stop_event = stop_event
    process = child
    try:
        child.start()
    except Exception:
        process = None
        _server_stop_event = None
        raise
    if not ready_event.wait(RPC_STARTUP_TIMEOUT_SECONDS):
        logger.error("[OCR-RPC] Сервер OCR не сообщил о готовности в заданный срок")
        stop_ocr_server_process()
        return False
    logger.info(f"[OCR-RPC] Запущен процесс сервера OCR на loopback-порту {port}")
    return True


def stop_ocr_server_process():
    """Корректно остановить OCR-сервер и проверить отсутствие orphan process."""
    global _server_stop_event, process
    child = process
    if child is None:
        return True

    if child.is_alive():
        if _server_stop_event is not None:
            _server_stop_event.set()
        child.join(timeout=RPC_SHUTDOWN_TIMEOUT_SECONDS)
        if child.is_alive():
            child.terminate()
            child.join(timeout=RPC_SHUTDOWN_TIMEOUT_SECONDS)
        if child.is_alive() and hasattr(child, "kill"):
            child.kill()
            child.join(timeout=RPC_SHUTDOWN_TIMEOUT_SECONDS)

    still_alive = child.is_alive()
    if still_alive:
        logger.error("[OCR-RPC] Не удалось остановить процесс сервера OCR")
        return False
    process = None
    _server_stop_event = None
    logger.info("[OCR-RPC] Процесс сервера OCR остановлен")
    return True


def alive() -> bool:
    """Проверить, жив ли процесс OCR-сервера."""
    return process is not None and process.is_alive()


if __name__ == "__main__":
    _configure_ocr_logging()
    parser = argparse.ArgumentParser(description="Локальный сервис OCR AzurPilot")
    parser.add_argument(
        "--port",
        type=int,
        help="Loopback-порт; по умолчанию используется OcrServerPort из deploy config",
    )
    args, _ = parser.parse_known_args()
    port = args.port or State.deploy_config.OcrServerPort
    start_ocr_server(port=port)
