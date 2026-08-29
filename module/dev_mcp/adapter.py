"""Тонкая безопасная граница между MCP tools и Dev Runtime.

Этот модуль намеренно не создаёт ``DevSessionManager`` при импорте. Единственная
точка, где выбирается runtime, находится в локальном ``_default_manager`` и
использует уже существующий ``DevEnvironment.current()`` внутри менеджера.
"""

from __future__ import annotations

import importlib
import logging
import math
import re
import sys
import threading
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

DEV_MCP_TOOL_NAMES = (
    "dev_preflight",
    "dev_doctor",
    "dev_list_tasks",
    "dev_plan_session",
    "dev_start_session",
    "dev_status",
    "dev_stop_session",
    "dev_cleanup",
    "dev_recover",
)

_NO_ARGUMENT_TOOLS = frozenset(
    {
        "dev_preflight",
        "dev_doctor",
        "dev_list_tasks",
        "dev_status",
        "dev_cleanup",
        "dev_recover",
    }
)

_MAX_RESULT_TEXT = 4096
_MAX_RESULT_KEY = 128
_MAX_RESULT_DEPTH = 8
_MAX_RESULT_ITEMS = 256
_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_:/])(?:[A-Za-z]:[\\/]|\\\\|/(?!/))[^\s,;)\]}]+"
)
_URL_USERINFO = re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE)
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:access[_-]?token|api[_-]?key|token|password|passwd|secret)=)[^&#\s]+"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|access[_-]?token|api[_-]?key|token|password|passwd|secret)"
    r"\s*([:=])\s*(?:bearer\s+)?[^\s,;]+"
)
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "command_line",
        "config",
        "config_path",
        "cookie",
        "credentials",
        "cwd",
        "env",
        "environment",
        "executable",
        "headers",
        "passfile",
        "password",
        "policy_file",
        "raw",
        "repository_root",
        "root_path",
        "state_file",
        "token",
        "traceback",
        "worker_registry",
    }
)


class DevRuntimeManager(Protocol):
    """Минимальный protocol, нужный adapter-у и его unit tests."""

    def preflight(self) -> object: ...

    def doctor(self) -> object: ...

    def list_tasks(self) -> object: ...

    def plan(self, *, root_tasks: list[str], excluded_tasks: list[str]) -> object: ...

    def start(self, *, root_tasks: list[str], excluded_tasks: list[str]) -> object: ...

    def status(self) -> object: ...

    def stop(self, *, preserve_task_state: bool = False) -> object: ...

    def cleanup(self) -> object: ...

    def recover(self) -> object: ...


def _default_manager() -> DevRuntimeManager:
    """Лениво импортировать composition root существующего Dev Runtime."""

    from module.dev_runtime import DevSessionManager

    return DevSessionManager()


_LEGACY_LOGGER_LOCK = threading.Lock()


def _ensure_legacy_logger_stderr() -> None:
    """Изолировать legacy Rich logger от stdio MCP при необходимости."""

    with _LEGACY_LOGGER_LOCK:
        legacy_logger = sys.modules.get("module.logger")
        # Legacy ``module.logger`` emits a startup banner during import and
        # binds RichHandler to the current stdout. Import the legacy logging
        # modules only on an explicit runtime call, redirecting that output to
        # stderr before diagnostics can import WebUI packages.
        with redirect_stdout(sys.stderr):
            if legacy_logger is None:
                legacy_logger = importlib.import_module("module.logger")
            if "deploy.logger" not in sys.modules:
                importlib.import_module("deploy.logger")

        for handler in legacy_logger.logger.handlers:
            console = getattr(handler, "console", None)
            if console is not None:
                try:
                    console.file = sys.stderr
                except (AttributeError, TypeError):
                    continue



class _EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _TaskArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    root_tasks: list[str] = Field(min_length=1)
    excluded_tasks: list[str] = Field(default_factory=list)


class _StopArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    preserve_task_state: bool = False


def _field(result: object, name: str, default: object = None) -> object:
    if isinstance(result, Mapping):
        return result.get(name, default)
    try:
        return getattr(result, name)
    except (AttributeError, TypeError):
        return default


def _redact_text(value: str) -> str:
    value = _URL_USERINFO.sub(r"\g<scheme>***@", value)
    value = _SENSITIVE_QUERY.sub(r"\1***", value)
    value = _SENSITIVE_ASSIGNMENT.sub(r"\1\2***", value)
    value = _ABSOLUTE_PATH.sub("[путь скрыт]", value)
    if len(value) > _MAX_RESULT_TEXT:
        return value[:_MAX_RESULT_TEXT] + "…"
    return value


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_path")
        or any(
            marker in normalized
            for marker in ("secret", "token", "password", "credential", "cookie")
        )
    )


def _safe_value(value: object, *, depth: int = 0) -> object:
    if depth > _MAX_RESULT_DEPTH:
        return "[вложенность скрыта]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 10**12 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        safe: dict[str, object] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= _MAX_RESULT_ITEMS:
                break
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > _MAX_RESULT_KEY:
                continue
            if _sensitive_key(raw_key):
                continue
            safe[raw_key] = _safe_value(raw_value, depth=depth + 1)
        return safe
    if isinstance(value, (list, tuple)):
        return [
            _safe_value(item, depth=depth + 1)
            for item in value[:_MAX_RESULT_ITEMS]
        ]
    return None


def serialize_dev_result(result: object) -> dict[str, object]:
    """Сериализовать только публичные поля DevResult через allowlist."""

    raw_ok = _field(result, "ok", False)
    raw_code = _field(result, "code", "DEV_MCP_INVALID_RESULT")
    raw_message = _field(result, "message", "Результат Dev Runtime имеет некорректную форму")
    raw_state = _field(result, "state", "failed")
    raw_session_id = _field(result, "session_id")
    raw_details = _field(result, "details", {})

    ok = raw_ok if isinstance(raw_ok, bool) else False
    code = _redact_text(raw_code) if isinstance(raw_code, str) else "DEV_MCP_INVALID_RESULT"
    message = (
        _redact_text(raw_message)
        if isinstance(raw_message, str)
        else "Результат Dev Runtime имеет некорректную форму"
    )
    state = _redact_text(raw_state) if isinstance(raw_state, str) else "failed"
    session_id = (
        _redact_text(raw_session_id)
        if isinstance(raw_session_id, str) and len(raw_session_id) <= 128
        else None
    )
    details = _safe_value(raw_details)
    if not isinstance(details, dict):
        details = {}
    return {
        "ok": ok,
        "code": code,
        "message": message,
        "state": state,
        "session_id": session_id,
        "details": details,
    }


def _input_error(tool_name: str) -> dict[str, object]:
    return {
        "ok": False,
        "code": "DEV_MCP_INPUT_INVALID",
        "message": "Входные аргументы Dev MCP не прошли строгую проверку",
        "state": "failed",
        "session_id": None,
        "details": {"tool": tool_name, "validation": "schema"},
    }


def _unknown_tool_error(tool_name: str) -> dict[str, object]:
    return {
        "ok": False,
        "code": "DEV_MCP_UNKNOWN_TOOL",
        "message": "Запрошенный инструмент Dev MCP не существует",
        "state": "failed",
        "session_id": None,
        "details": {"tool": _redact_text(tool_name)},
    }


def _internal_error() -> dict[str, object]:
    return {
        "ok": False,
        "code": "DEV_MCP_INTERNAL_ERROR",
        "message": "Внутренняя ошибка Dev MCP; подробности записаны в stderr",
        "state": "failed",
        "session_id": None,
        "details": {},
    }


class DevMcpAdapter:
    """Dispatch MCP tools к одному лениво создаваемому Dev Runtime manager."""

    def __init__(
        self,
        manager_factory: Callable[[], DevRuntimeManager] | None = None,
    ) -> None:
        self._manager_factory = manager_factory or _default_manager
        self._uses_default_manager = manager_factory is None
        self._manager: DevRuntimeManager | None = None
        self._manager_lock = threading.Lock()

    def _get_manager(self) -> DevRuntimeManager:
        manager = self._manager
        if manager is not None:
            return manager
        with self._manager_lock:
            manager = self._manager
            if manager is None:
                manager = self._manager_factory()
                self._manager = manager
            return manager

    @staticmethod
    def _arguments(arguments: Mapping[str, object] | None) -> dict[str, object]:
        if arguments is None:
            return {}
        if not isinstance(arguments, Mapping):
            raise TypeError("arguments должен быть объектом")
        return dict(arguments)

    def _validated(
        self,
        tool_name: str,
        arguments: Mapping[str, object] | None,
    ) -> _EmptyArguments | _TaskArguments | _StopArguments | None:
        try:
            raw = self._arguments(arguments)
            if tool_name in _NO_ARGUMENT_TOOLS:
                return _EmptyArguments.model_validate(raw, strict=True)
            if tool_name in {"dev_plan_session", "dev_start_session"}:
                return _TaskArguments.model_validate(raw, strict=True)
            if tool_name == "dev_stop_session":
                return _StopArguments.model_validate(raw, strict=True)
        except (TypeError, ValueError, ValidationError):
            return None
        return None

    def call(
        self,
        tool_name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Выполнить один разрешённый tool без dynamic getattr или path input."""

        if tool_name not in DEV_MCP_TOOL_NAMES:
            return _unknown_tool_error(tool_name)
        parsed = self._validated(tool_name, arguments)
        if parsed is None:
            return _input_error(tool_name)

        try:
            if self._uses_default_manager:
                _ensure_legacy_logger_stderr()
            manager = self._get_manager()
            if tool_name == "dev_preflight":
                result = manager.preflight()
            elif tool_name == "dev_doctor":
                result = manager.doctor()
            elif tool_name == "dev_list_tasks":
                result = manager.list_tasks()
            elif tool_name == "dev_plan_session":
                assert isinstance(parsed, _TaskArguments)
                result = manager.plan(
                    root_tasks=parsed.root_tasks,
                    excluded_tasks=parsed.excluded_tasks,
                )
            elif tool_name == "dev_start_session":
                assert isinstance(parsed, _TaskArguments)
                result = manager.start(
                    root_tasks=parsed.root_tasks,
                    excluded_tasks=parsed.excluded_tasks,
                )
            elif tool_name == "dev_status":
                result = manager.status()
            elif tool_name == "dev_stop_session":
                assert isinstance(parsed, _StopArguments)
                result = manager.stop(preserve_task_state=parsed.preserve_task_state)
            elif tool_name == "dev_cleanup":
                result = manager.cleanup()
            else:
                assert tool_name == "dev_recover"
                result = manager.recover()
        except Exception as exc:
            logger.error(
                "[Dev MCP] tool %s завершился неожиданной ошибкой: %s",
                tool_name,
                type(exc).__name__,
            )
            return _internal_error()
        return serialize_dev_result(result)


__all__ = ["DEV_MCP_TOOL_NAMES", "DevMcpAdapter", "serialize_dev_result"]
