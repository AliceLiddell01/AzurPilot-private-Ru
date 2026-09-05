"""Фиксированный локальный control plane для WebUI runtime на стороне owner."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from deploy.atomic import atomic_write
from module.application.host_lock import application_host_lock
from module.application.runtime_state import RuntimeStateError, _scoped_path

_SCHEMA_VERSION = 2
_MAX_REQUEST_BYTES = 32 * 1024
_MAX_RESULT_BYTES = 128 * 1024
_MAX_REQUEST_FILES = 128
_MAX_RESULT_FILES = 128
_MAX_CONTROL_TIMEOUT_SECONDS = 120.0
_MAX_TEXT = 512
_SAFE_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
_SAFE_CODE = r"[A-Z][A-Z0-9_]{1,96}"


class RuntimeControlOperation(StrEnum):
    START_PROFILE = "start_profile"
    STOP_PROFILE = "stop_profile"


class RuntimeControlError(RuntimeError):
    """Ожидаемая ошибка локального control plane."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RuntimeOwnerIdentity:
    pid: int
    created_at: float

    def as_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "created_at": self.created_at}

    @classmethod
    def from_value(cls, value: object) -> RuntimeOwnerIdentity:
        if isinstance(value, cls):
            value = value.as_dict()
        if not isinstance(value, Mapping):
            raise RuntimeControlError("RUNTIME_OWNER_INVALID", "Идентичность WebUI owner имеет неверный тип")
        if set(value) != {"pid", "created_at"}:
            raise RuntimeControlError("RUNTIME_OWNER_INVALID", "Идентичность WebUI owner имеет неизвестные поля")
        pid = value.get("pid")
        created_at = value.get("created_at")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
            or not float(created_at) > 0
        ):
            raise RuntimeControlError("RUNTIME_OWNER_INVALID", "Идентичность WebUI owner имеет неверные поля")
        return cls(pid, float(created_at))


@dataclass(frozen=True, slots=True)
class RuntimeControlResult:
    ok: bool
    code: str
    message: str
    operation: RuntimeControlOperation
    profile: str
    request_id: str
    idempotency_key: str
    state: Mapping[str, object] | None = None
    details: Mapping[str, object] = field(default_factory=dict)
    owner: RuntimeOwnerIdentity | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "operation": self.operation.value,
            "profile": self.profile,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "state": dict(self.state) if isinstance(self.state, Mapping) else None,
            "details": dict(self.details) if isinstance(self.details, Mapping) else {},
            "owner": self.owner.as_dict() if self.owner is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RuntimeControlResult:
        if not isinstance(payload, Mapping):
            raise RuntimeControlError("RUNTIME_RESULT_INVALID", "Результат control operation должен быть объектом")
        required = {
            "schema_version",
            "ok",
            "code",
            "message",
            "operation",
            "profile",
            "request_id",
            "idempotency_key",
            "state",
            "details",
            "owner",
        }
        if set(payload) != required or payload.get("schema_version") != _SCHEMA_VERSION:
            raise RuntimeControlError("RUNTIME_RESULT_INVALID", "Результат control operation имеет неизвестные поля")
        if type(payload.get("ok")) is not bool:
            raise RuntimeControlError("RUNTIME_RESULT_INVALID", "Результат control operation имеет неверный ok")
        code = _text(payload.get("code"), maximum=100, pattern=_SAFE_CODE, field="code")
        message = _text(payload.get("message"), maximum=_MAX_TEXT, field="message")
        try:
            operation = RuntimeControlOperation(str(payload.get("operation")))
        except ValueError as exc:
            raise RuntimeControlError("RUNTIME_RESULT_INVALID", "Результат содержит неизвестную operation") from exc
        profile = _profile(payload.get("profile"))
        request_id = _token(payload.get("request_id"), field="request_id")
        idempotency_key = _token(payload.get("idempotency_key"), field="idempotency_key")
        state = payload.get("state")
        if state is not None and not isinstance(state, Mapping):
            raise RuntimeControlError("RUNTIME_RESULT_INVALID", "state результата должен быть объектом или null")
        details = payload.get("details")
        if not isinstance(details, Mapping):
            raise RuntimeControlError("RUNTIME_RESULT_INVALID", "details результата должен быть объектом")
        owner_payload = payload.get("owner")
        owner = None if owner_payload is None else RuntimeOwnerIdentity.from_value(owner_payload)
        return cls(
            ok=bool(payload["ok"]),
            code=code,
            message=message,
            operation=operation,
            profile=profile,
            request_id=request_id,
            idempotency_key=idempotency_key,
            state=dict(state) if isinstance(state, Mapping) else None,
            details=dict(details),
            owner=owner,
        )


class RuntimeControlExecutor(Protocol):
    def __call__(
        self,
        operation: RuntimeControlOperation,
        profile: str,
        *,
        request_id: str,
        idempotency_key: str,
        session_id: str | None,
        expires_at: str,
    ) -> RuntimeControlResult | Mapping[str, object]: ...


def _text(value: object, *, maximum: int, field: str, pattern: str | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or (pattern is not None and re.fullmatch(pattern, value) is None)
    ):
        raise RuntimeControlError("RUNTIME_CONTROL_FIELD_INVALID", f"Поле {field} имеет недопустимый формат")
    return value


def _token(value: object, *, field: str) -> str:
    return _text(value, maximum=128, pattern=_SAFE_TOKEN, field=field)


def _profile(value: object) -> str:
    return _text(value, maximum=64, pattern=r"[A-Za-z0-9_-]{1,64}", field="profile")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _expires_at(timeout: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=timeout)).isoformat()


def _parse_utc_timestamp(value: object, *, field: str) -> datetime:
    text = _text(value, maximum=80, field=field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeControlError(
            "RUNTIME_CONTROL_FIELD_INVALID",
            f"Поле {field} не является ISO timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise RuntimeControlError(
            "RUNTIME_CONTROL_FIELD_INVALID",
            f"Поле {field} должно быть в UTC",
        )
    return parsed


def _request_is_expired(payload: object, *, now: datetime) -> bool:
    if not isinstance(payload, Mapping):
        return False
    try:
        expires_at = _parse_utc_timestamp(payload.get("expires_at"), field="expires_at")
    except RuntimeControlError:
        return False
    return now >= expires_at


def _safe_plane_path(repository_root: Path, relative: str) -> Path:
    try:
        return _scoped_path(repository_root, relative)
    except RuntimeStateError as exc:
        raise RuntimeControlError(
            "RUNTIME_CONTROL_UNSAFE_PATH",
            "Путь control plane проходит через ссылку или выходит за рабочую копию",
        ) from exc


def _read_bounded(path: Path, maximum: int) -> bytes:
    raw: bytes
    for attempt in range(5):
        try:
            raw = path.read_bytes()
            break
        except FileNotFoundError:
            raise
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise RuntimeControlError(
                    "RUNTIME_CONTROL_READ_FAILED",
                    "Файл control plane невозможно прочитать",
                ) from None
            time.sleep(0.01 * (2**attempt))
        except OSError as exc:
            raise RuntimeControlError("RUNTIME_CONTROL_READ_FAILED", "Файл control plane невозможно прочитать") from exc
    if len(raw) > maximum:
        raise RuntimeControlError("RUNTIME_CONTROL_TOO_LARGE", "Файл control plane превышает допустимый размер")
    return raw


def _read_json(path: Path, maximum: int) -> object:
    try:
        raw = _read_bounded(path, maximum)
    except FileNotFoundError:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeControlError("RUNTIME_CONTROL_CORRUPT", "Файл control plane содержит некорректный JSON") from exc


def _write_json(path: Path, payload: Mapping[str, object], maximum: int) -> None:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > maximum:
        raise RuntimeControlError("RUNTIME_CONTROL_TOO_LARGE", "Payload control plane превышает допустимый размер")
    try:
        atomic_write(path, encoded)
    except OSError as exc:
        raise RuntimeControlError("RUNTIME_CONTROL_WRITE_FAILED", "Payload control plane невозможно записать") from exc


def _owner_equal(left: RuntimeOwnerIdentity, right: RuntimeOwnerIdentity) -> bool:
    return left.pid == right.pid and left.created_at == right.created_at


class WebUIControlClient:
    """Клиент, который может только отправить фиксированную typed operation."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        owner_reader: Callable[[], object | None],
        owner_matches: Callable[[RuntimeOwnerIdentity], bool],
        bootstrapper: SharedWebUIBootstrapper | None = None,
        timeout: float = 15.0,
        poll_interval: float = 0.05,
    ) -> None:
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= _MAX_CONTROL_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout control plane должен быть в диапазоне (0, 120] секунд")
        if type(poll_interval) not in (int, float) or not 0 < float(poll_interval) <= 1:
            raise ValueError("poll_interval control plane должен быть в диапазоне (0, 1]")
        self.repository_root = Path(repository_root).resolve()
        self.root = _safe_plane_path(self.repository_root, "config/state/webui-control")
        self.requests = _safe_plane_path(self.repository_root, "config/state/webui-control/requests")
        self.results = _safe_plane_path(self.repository_root, "config/state/webui-control/results")
        self.lock_path = _safe_plane_path(self.repository_root, "config/state/webui-control/plane.lock")
        self.owner_reader = owner_reader
        self.owner_matches = owner_matches
        self.bootstrapper = bootstrapper
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)

    def _request_path(self, key: str) -> Path:
        return _safe_plane_path(
            self.repository_root,
            f"config/state/webui-control/requests/{key}.json",
        )

    def _result_path(self, key: str) -> Path:
        return _safe_plane_path(
            self.repository_root,
            f"config/state/webui-control/results/{key}.json",
        )

    def _ensure_directories(self) -> None:
        _safe_plane_path(
            self.repository_root, "config/state/webui-control/requests"
        ).mkdir(parents=True, exist_ok=True)
        _safe_plane_path(
            self.repository_root, "config/state/webui-control/results"
        ).mkdir(parents=True, exist_ok=True)

    def call(
        self,
        operation: RuntimeControlOperation,
        profile: str,
        *,
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RuntimeControlResult:
        if not isinstance(operation, RuntimeControlOperation):
            raise RuntimeControlError("RUNTIME_OPERATION_INVALID", "Операция runtime control не входит в typed catalog")
        profile = _profile(profile)
        if session_id is not None:
            session_id = _token(session_id, field="session_id")
        key = _token(idempotency_key or str(uuid.uuid4()), field="idempotency_key")
        self._ensure_directories()
        owner = self._ensure_owner()
        result_path = self._result_path(key)
        existing = self._read_result(result_path)
        if existing is not None:
            self._validate_result(existing, operation, profile, key, owner=owner)
            return existing

        request_path = self._request_path(key)
        with application_host_lock(self.lock_path):
            existing = self._read_result(result_path)
            if existing is not None:
                self._validate_result(existing, operation, profile, key, owner=owner)
                return existing
            request = _read_json(request_path, _MAX_REQUEST_BYTES)
            if request is not None:
                request_id = _validate_request(request, operation=operation, profile=profile, session_id=session_id, idempotency_key=key)
            else:
                request_id = str(uuid.uuid4())
                payload = {
                    "schema_version": _SCHEMA_VERSION,
                    "request_id": request_id,
                    "idempotency_key": key,
                    "operation": operation.value,
                    "profile": profile,
                    "session_id": session_id,
                    "expected_owner": owner.as_dict(),
                    "created_at": _timestamp(),
                    "expires_at": _expires_at(self.timeout),
                }
                _write_json(request_path, payload, _MAX_REQUEST_BYTES)

        deadline = time.monotonic() + self.timeout
        while True:
            result = self._read_result(result_path)
            if result is not None:
                self._validate_result(result, operation, profile, key, owner=owner)
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeControlError(
                    "RUNTIME_CONTROL_TIMEOUT",
                    "Операция управления WebUI не завершилась в ограниченный срок",
                )
            time.sleep(min(self.poll_interval, remaining))

    def ensure_owner(self) -> RuntimeOwnerIdentity:
        """Подтвердить текущего owner или выполнить canonical bootstrap."""

        return self._ensure_owner()

    def _ensure_owner(self) -> RuntimeOwnerIdentity:
        raw = self.owner_reader()
        if self.bootstrapper is not None:
            if raw is None:
                self.bootstrapper.ensure()
            else:
                candidate = RuntimeOwnerIdentity.from_value(raw)
                try:
                    matches = self.owner_matches(candidate)
                except Exception as exc:
                    raise RuntimeControlError(
                        "RUNTIME_OWNER_UNKNOWN",
                        "Идентичность общего WebUI owner невозможно проверить",
                    ) from exc
                if matches is not True:
                    # Canonical gui.py сам атомарно перепроверит старую запись
                    # и не сможет перезаписать живого owner или orphan worker.
                    self.bootstrapper.ensure()
            raw = self.owner_reader()
        if raw is None:
            raise RuntimeControlError("RUNTIME_OWNER_UNAVAILABLE", "Общий WebUI owner не найден")
        owner = RuntimeOwnerIdentity.from_value(raw)
        try:
            matches = self.owner_matches(owner)
        except Exception as exc:
            raise RuntimeControlError("RUNTIME_OWNER_UNKNOWN", "Идентичность общего WebUI owner невозможно проверить") from exc
        if matches is not True:
            raise RuntimeControlError("RUNTIME_OWNER_STALE", "Идентичность общего WebUI owner устарела")
        return owner

    def _read_result(self, path: Path) -> RuntimeControlResult | None:
        if path.suffix != ".json" or re.fullmatch(_SAFE_TOKEN, path.stem) is None:
            raise RuntimeControlError("RUNTIME_RESULT_INVALID", "Путь результата control operation имеет небезопасное имя")
        path = self._result_path(path.stem)
        payload = _read_json(path, _MAX_RESULT_BYTES)
        if payload is None:
            return None
        return RuntimeControlResult.from_dict(payload)

    @staticmethod
    def _validate_result(
        result: RuntimeControlResult,
        operation: RuntimeControlOperation,
        profile: str,
        idempotency_key: str,
        *,
        owner: RuntimeOwnerIdentity,
    ) -> None:
        if (
            result.operation is not operation
            or result.profile != profile
            or result.idempotency_key != idempotency_key
        ):
            raise RuntimeControlError(
                "RUNTIME_RESULT_CONFLICT",
                "Результат control operation связан с другой operation",
            )
        if result.ok and (
            result.owner is None or not _owner_equal(result.owner, owner)
        ):
            raise RuntimeControlError(
                "RUNTIME_RESULT_CONFLICT",
                "Результат успешной control operation связан с другим owner",
            )


def _validate_request(
    payload: object,
    *,
    operation: RuntimeControlOperation,
    profile: str,
    session_id: str | None,
    idempotency_key: str,
) -> str:
    if not isinstance(payload, Mapping):
        raise RuntimeControlError("RUNTIME_REQUEST_CONFLICT", "Повторный idempotency key имеет неверный request")
    required = {
        "schema_version",
        "request_id",
        "idempotency_key",
        "operation",
        "profile",
        "session_id",
        "expected_owner",
        "created_at",
        "expires_at",
    }
    if set(payload) != required or payload.get("schema_version") != _SCHEMA_VERSION:
        raise RuntimeControlError("RUNTIME_REQUEST_CONFLICT", "Повторный runtime request имеет неизвестные поля")
    request_id = _token(payload.get("request_id"), field="request_id")
    if payload.get("idempotency_key") != idempotency_key or payload.get("operation") != operation.value or payload.get("profile") != profile or payload.get("session_id") != session_id:
        raise RuntimeControlError("RUNTIME_REQUEST_CONFLICT", "Idempotency key уже связан с другой operation")
    RuntimeOwnerIdentity.from_value(payload.get("expected_owner"))
    created_at = _parse_utc_timestamp(payload.get("created_at"), field="created_at")
    expires_at = _parse_utc_timestamp(payload.get("expires_at"), field="expires_at")
    if expires_at <= created_at:
        raise RuntimeControlError("RUNTIME_REQUEST_CONFLICT", "Срок действия runtime request должен быть позже создания")
    return request_id


class WebUIControlServer:
    """Исполнитель фиксированного control catalog внутри WebUI owner."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        owner_reader: Callable[[], object | None],
        owner_matches: Callable[[RuntimeOwnerIdentity], bool],
        executor: RuntimeControlExecutor,
        poll_interval: float = 0.05,
    ) -> None:
        if type(poll_interval) not in (int, float) or not 0 < float(poll_interval) <= 1:
            raise ValueError("poll_interval control server должен быть в диапазоне (0, 1]")
        self.repository_root = Path(repository_root).resolve()
        self.root = _safe_plane_path(self.repository_root, "config/state/webui-control")
        self.requests = _safe_plane_path(self.repository_root, "config/state/webui-control/requests")
        self.results = _safe_plane_path(self.repository_root, "config/state/webui-control/results")
        self.lock_path = _safe_plane_path(self.repository_root, "config/state/webui-control/plane.lock")
        self.owner_reader = owner_reader
        self.owner_matches = owner_matches
        self.executor = executor
        self.poll_interval = float(poll_interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._operation_lock = threading.Lock()

    def _request_path(self, key: str) -> Path:
        return _safe_plane_path(
            self.repository_root,
            f"config/state/webui-control/requests/{key}.json",
        )

    def _result_path(self, key: str) -> Path:
        return _safe_plane_path(
            self.repository_root,
            f"config/state/webui-control/results/{key}.json",
        )

    def _ensure_directories(self) -> None:
        _safe_plane_path(
            self.repository_root, "config/state/webui-control/requests"
        ).mkdir(parents=True, exist_ok=True)
        _safe_plane_path(
            self.repository_root, "config/state/webui-control/results"
        ).mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ensure_directories()
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="webui-runtime-control", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
        self._thread = None

    def serve_once(self) -> int:
        self._ensure_directories()
        processed = 0
        try:
            candidates = sorted(self.requests.glob("*.json"), key=lambda path: path.name)
        except OSError:
            return 0
        paths: list[Path] = []
        expired_requests: list[tuple[Path, object]] = []
        now = datetime.now(UTC)
        for candidate in candidates:
            if candidate.suffix != ".json" or re.fullmatch(_SAFE_TOKEN, candidate.stem) is None:
                continue
            try:
                request_path = self._request_path(candidate.stem)
            except RuntimeControlError:
                continue
            try:
                payload = _read_json(request_path, _MAX_REQUEST_BYTES)
            except RuntimeControlError:
                paths.append(request_path)
                continue
            if payload is not None and _request_is_expired(payload, now=now):
                if len(expired_requests) < _MAX_REQUEST_FILES:
                    expired_requests.append((request_path, payload))
                continue
            paths.append(request_path)

        for request_path, payload in expired_requests:
            if self._stop.is_set():
                break
            if isinstance(payload, Mapping):
                try:
                    key = _token(payload.get("idempotency_key"), field="idempotency_key")
                    if self._result_path(key).exists():
                        self._remove_request(request_path)
                        continue
                except (RuntimeControlError, OSError):
                    pass
            written = self._write_error_result(
                payload,
                code="RUNTIME_CONTROL_EXPIRED",
                message="Срок действия runtime control request истёк до выполнения operation",
            )
            self._remove_request(request_path)
            if written:
                processed += 1

        paths = paths[:_MAX_REQUEST_FILES]
        for request_path in paths:
            if self._stop.is_set():
                break
            if request_path.suffix != ".json" or re.fullmatch(_SAFE_TOKEN, request_path.stem) is None:
                continue
            try:
                request_path = self._request_path(request_path.stem)
            except RuntimeControlError:
                continue
            payload: object | None = None
            try:
                payload = _read_json(request_path, _MAX_REQUEST_BYTES)
                if payload is None:
                    continue
                request = _parse_request(payload)
                result_path = self._result_path(str(request["idempotency_key"]))
                if result_path.exists():
                    self._remove_request(request_path)
                    continue
                result = self._execute(request)
                _write_json(result_path, result.as_dict(), _MAX_RESULT_BYTES)
                self._remove_request(request_path)
                processed += 1
            except RuntimeControlError as exc:
                written = self._write_error_result(
                    payload,
                    code=exc.code if exc.code.startswith("RUNTIME_") else "RUNTIME_REQUEST_INVALID",
                    message=str(exc),
                )
                self._remove_request(request_path)
                if written:
                    processed += 1
            except Exception:  # noqa: BLE001 - сервер должен продолжать обслуживать остальные запросы.
                written = self._write_error_result(
                    payload,
                    code="RUNTIME_EXECUTION_FAILED",
                    message="Операция владельца runtime завершилась ошибкой",
                )
                self._remove_request(request_path)
                if written:
                    processed += 1
        self._prune_results()
        return processed

    def _serve(self) -> None:
        while not self._stop.is_set():
            self.serve_once()
            self._stop.wait(self.poll_interval)

    def _execute(self, request: Mapping[str, object]) -> RuntimeControlResult:
        operation = RuntimeControlOperation(str(request["operation"]))
        profile = _profile(request["profile"])
        request_id = _token(request["request_id"], field="request_id")
        key = _token(request["idempotency_key"], field="idempotency_key")
        session_id = request.get("session_id")
        if session_id is not None:
            session_id = _token(session_id, field="session_id")
        expected_owner = RuntimeOwnerIdentity.from_value(request["expected_owner"])
        expires_at = _parse_utc_timestamp(request["expires_at"], field="expires_at")
        raw_owner = self.owner_reader()
        if raw_owner is None:
            return RuntimeControlResult(False, "RUNTIME_OWNER_UNAVAILABLE", "Общий WebUI owner завершил работу", operation, profile, request_id, key)
        owner = RuntimeOwnerIdentity.from_value(raw_owner)
        if not _owner_equal(owner, expected_owner):
            return RuntimeControlResult(False, "RUNTIME_OWNER_CHANGED", "Владелец WebUI изменился до выполнения operation", operation, profile, request_id, key, owner=owner)
        if datetime.now(UTC) >= expires_at:
            return RuntimeControlResult(
                False,
                "RUNTIME_CONTROL_EXPIRED",
                "Срок действия runtime control request истёк до выполнения operation",
                operation,
                profile,
                request_id,
                key,
                owner=owner,
            )
        try:
            valid = self.owner_matches(owner)
        except Exception:  # noqa: BLE001 - граница owner работает fail-closed.
            valid = False
        if valid is not True:
            return RuntimeControlResult(False, "RUNTIME_OWNER_STALE", "Идентичность WebUI owner больше не подтверждается", operation, profile, request_id, key, owner=owner)
        with self._operation_lock:
            if datetime.now(UTC) >= expires_at:
                return RuntimeControlResult(
                    False,
                    "RUNTIME_CONTROL_EXPIRED",
                    "Срок действия runtime control request истёк до захвата operation lock",
                    operation,
                    profile,
                    request_id,
                    key,
                    owner=owner,
                )
            result = self.executor(
                operation,
                profile,
                request_id=request_id,
                idempotency_key=key,
                session_id=session_id,
                expires_at=expires_at.isoformat(),
            )
        if datetime.now(UTC) >= expires_at:
            return RuntimeControlResult(
                False,
                "RUNTIME_CONTROL_EXPIRED",
                "Срок действия runtime control request истёк после выполнения operation",
                operation,
                profile,
                request_id,
                key,
                owner=owner,
            )
        if isinstance(result, RuntimeControlResult):
            if result.request_id != request_id or result.idempotency_key != key:
                raise RuntimeControlError("RUNTIME_EXECUTION_INVALID", "Executor владельца вернул чужую identity")
            validated = RuntimeControlResult.from_dict(result.as_dict())
            if validated.operation is not operation or validated.profile != profile:
                raise RuntimeControlError(
                    "RUNTIME_EXECUTION_INVALID",
                    "Executor владельца вернул чужую operation",
                )
            if validated.owner is None or not _owner_equal(validated.owner, owner):
                raise RuntimeControlError(
                    "RUNTIME_EXECUTION_INVALID",
                    "Executor владельца вернул чужую identity owner",
                )
            return validated
        if isinstance(result, Mapping):
            validated = RuntimeControlResult.from_dict(result)
            if validated.operation is not operation or validated.profile != profile:
                raise RuntimeControlError(
                    "RUNTIME_EXECUTION_INVALID",
                    "Executor владельца вернул чужую operation",
                )
            if validated.owner is None or not _owner_equal(validated.owner, owner):
                raise RuntimeControlError(
                    "RUNTIME_EXECUTION_INVALID",
                    "Executor владельца вернул чужую identity owner",
                )
            return validated
        raise RuntimeControlError("RUNTIME_EXECUTION_INVALID", "Executor владельца вернул неподдерживаемый результат")

    def _write_error_result(self, payload: object, *, code: str, message: str) -> bool:
        if not isinstance(payload, Mapping):
            return False
        try:
            request_id = _token(payload.get("request_id"), field="request_id")
            key = _token(payload.get("idempotency_key"), field="idempotency_key")
            profile = _profile(payload.get("profile"))
        except RuntimeControlError:
            return False
        try:
            operation = RuntimeControlOperation(str(payload.get("operation")))
        except ValueError:
            operation = RuntimeControlOperation.START_PROFILE
        try:
            safe_code = _text(code, maximum=100, pattern=_SAFE_CODE, field="code")
        except RuntimeControlError:
            safe_code = "RUNTIME_EXECUTION_FAILED"
        try:
            safe_message = _text(message, maximum=_MAX_TEXT, field="message")
        except RuntimeControlError:
            safe_message = "Операция владельца runtime завершилась ошибкой"
        owner = None
        try:
            raw_owner = self.owner_reader()
            if raw_owner is not None:
                candidate = RuntimeOwnerIdentity.from_value(raw_owner)
                if self.owner_matches(candidate) is True:
                    owner = candidate
        except Exception:  # noqa: BLE001 - результат ошибки записывается по возможности.
            owner = None
        result = RuntimeControlResult(
            ok=False,
            code=safe_code,
            message=safe_message,
            operation=operation,
            profile=profile,
            request_id=request_id,
            idempotency_key=key,
            owner=owner,
        )
        try:
            _write_json(self._result_path(key), result.as_dict(), _MAX_RESULT_BYTES)
        except RuntimeControlError:
            return False
        return True

    @staticmethod
    def _remove_request(path: Path) -> None:
        try:
            path.unlink()
        except (FileNotFoundError, IsADirectoryError, OSError):
            pass

    def _prune_results(self) -> None:
        try:
            paths = sorted(
                (
                    self._result_path(path.stem)
                    for path in self.results.glob("*.json")
                    if re.fullmatch(_SAFE_TOKEN, path.stem)
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except (OSError, RuntimeControlError):
            return
        retention_cutoff = time.time() - _MAX_CONTROL_TIMEOUT_SECONDS
        for path in paths[_MAX_RESULT_FILES:]:
            try:
                if path.stat().st_mtime > retention_cutoff:
                    continue
                path.unlink()
            except OSError:
                pass


def _parse_request(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise RuntimeControlError("RUNTIME_REQUEST_INVALID", "Runtime request должен быть объектом")
    required = {
        "schema_version",
        "request_id",
        "idempotency_key",
        "operation",
        "profile",
        "session_id",
        "expected_owner",
        "created_at",
        "expires_at",
    }
    if set(payload) != required or payload.get("schema_version") != _SCHEMA_VERSION:
        raise RuntimeControlError("RUNTIME_REQUEST_INVALID", "Runtime request имеет неизвестные поля")
    request_id = _token(payload.get("request_id"), field="request_id")
    key = _token(payload.get("idempotency_key"), field="idempotency_key")
    try:
        operation = RuntimeControlOperation(str(payload.get("operation")))
    except ValueError as exc:
        raise RuntimeControlError("RUNTIME_OPERATION_INVALID", "Операция отсутствует в фиксированном типизированном каталоге") from exc
    profile = _profile(payload.get("profile"))
    session_id = payload.get("session_id")
    if session_id is not None:
        session_id = _token(session_id, field="session_id")
    RuntimeOwnerIdentity.from_value(payload.get("expected_owner"))
    created_at = _parse_utc_timestamp(payload.get("created_at"), field="created_at")
    expires_at = _parse_utc_timestamp(payload.get("expires_at"), field="expires_at")
    if expires_at <= created_at:
        raise RuntimeControlError("RUNTIME_REQUEST_INVALID", "Срок действия runtime request должен быть позже создания")
    return {
        "request_id": request_id,
        "idempotency_key": key,
        "operation": operation.value,
        "profile": profile,
        "session_id": session_id,
        "expected_owner": payload["expected_owner"],
        "expires_at": expires_at.isoformat(),
    }


class SharedWebUIBootstrapper:
    """Безопасно поднять ровно один canonical ``gui.py`` owner при его отсутствии."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        owner_reader: Callable[[], object | None],
        owner_matches: Callable[[RuntimeOwnerIdentity], bool],
        python_executable: Path | str | None = None,
        timeout: float = 30.0,
        poll_interval: float = 0.1,
    ) -> None:
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= _MAX_CONTROL_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout bootstrap должен быть в диапазоне (0, 120] секунд")
        self.repository_root = Path(repository_root).resolve()
        self.gui_path = _scoped_path(self.repository_root, "gui.py")
        default_python = self.repository_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.python_executable = Path(python_executable or default_python).resolve()
        self.lock_path = _scoped_path(self.repository_root, "config/state/webui-bootstrap.lock")
        self.owner_reader = owner_reader
        self.owner_matches = owner_matches
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._process: subprocess.Popen[bytes] | None = None

    def ensure(self) -> RuntimeOwnerIdentity:
        existing = self._read_valid_owner()
        if existing is not None:
            return existing
        if not self.gui_path.is_file() or not self.python_executable.is_file():
            raise RuntimeControlError("RUNTIME_BOOTSTRAP_UNAVAILABLE", "Canonical gui.py или project Python отсутствует")
        try:
            with application_host_lock(self.lock_path, timeout=min(30.0, self.timeout)):
                existing = self._read_valid_owner()
                if existing is not None:
                    return existing
                try:
                    self._process = subprocess.Popen(
                        [str(self.python_executable), str(self.gui_path)],
                        cwd=str(self.repository_root),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        start_new_session=os.name != "nt",
                    )
                except OSError as exc:
                    raise RuntimeControlError("RUNTIME_BOOTSTRAP_FAILED", "Не удалось запустить общий WebUI") from exc
                deadline = time.monotonic() + self.timeout
                while True:
                    owner = self._read_valid_owner()
                    if owner is not None:
                        return owner
                    if self._process.poll() is not None:
                        raise RuntimeControlError("RUNTIME_BOOTSTRAP_FAILED", "Общий WebUI завершился до регистрации owner")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._stop_owned_process()
                        raise RuntimeControlError("RUNTIME_BOOTSTRAP_TIMEOUT", "Общий WebUI не зарегистрировал owner в ограниченный срок")
                    time.sleep(min(self.poll_interval, remaining))
        except TimeoutError as exc:
            raise RuntimeControlError(
                "RUNTIME_BOOTSTRAP_TIMEOUT",
                "Не удалось получить bootstrap lock общего WebUI в ограниченный срок",
            ) from exc

    def _read_valid_owner(self) -> RuntimeOwnerIdentity | None:
        raw = self.owner_reader()
        if raw is None:
            return None
        owner = RuntimeOwnerIdentity.from_value(raw)
        try:
            if self.owner_matches(owner) is True:
                return owner
        except Exception:  # noqa: BLE001 - устаревший owner считается отсутствующим.
            return None
        return None

    def _stop_owned_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            # При ошибке bootstrap не используется taskkill: registry owner
            # остаётся источником истины для последующего восстановления.
            pass


__all__ = [
    "RuntimeControlError",
    "RuntimeControlExecutor",
    "RuntimeControlOperation",
    "RuntimeControlResult",
    "RuntimeOwnerIdentity",
    "SharedWebUIBootstrapper",
    "WebUIControlClient",
    "WebUIControlServer",
]
