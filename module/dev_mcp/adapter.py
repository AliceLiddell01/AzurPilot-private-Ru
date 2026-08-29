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
_FILE_URI_PATH = re.compile(r"(?i)\bfile:///[^\s,;)\]}]+")
_URL_USERINFO = re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE)
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:authorization|access[_-]?token|x[_-]?api[_-]?key|api[_-]?key|"
    r"token|password|passwd|secret)=)[^&#\s]+"
)
_CREDENTIAL_NAME = (
    r"authorization|access[_-]?token|x[_-]?api[_-]?key|api[_-]?key|"
    r"token|password|passwd|secret"
)
_SENSITIVE_QUOTED_ASSIGNMENT = re.compile(
    rf"""(?ix)
    (?P<key_quote>["'])
    (?P<key>{_CREDENTIAL_NAME})
    (?P=key_quote)
    (?P<separator>\s*[:=]\s*)
    (?P<value_quote>["'])
    (?P<bearer>bearer\s+)?
    (?P<value>.*?)
    (?P=value_quote)
    """
)
_SENSITIVE_QUOTED_VALUE = re.compile(
    rf"""(?ix)
    \b(?P<key>{_CREDENTIAL_NAME})
    (?P<separator>\s*[:=]\s*)
    (?P<value_quote>["'])
    (?P<bearer>bearer\s+)?
    (?P<value>.*?)
    (?P=value_quote)
    """
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?i)\b(?P<key>{_CREDENTIAL_NAME})"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<bearer>bearer\s+)?"
    r"(?P<value>[^\s,;}\]]+)"
)

_SAFE_DETAIL_KEYS = frozenset(
    {
        "allowed",
        "allowed_tasks",
        "blockers",
        "catalog",
        "checks",
        "cleanup",
        "cleanup_confirmed",
        "code",
        "command",
        "dependencies",
        "details",
        "enabled",
        "error",
        "excluded_tasks",
        "field",
        "host",
        "items",
        "lifecycle_marked_cleanup_pending",
        "log",
        "message",
        "name",
        "new_dependency",
        "next_run",
        "observed_code",
        "plan",
        "policy_marked",
        "policy_marked_cleanup_pending",
        "policy_removed",
        "policy_state",
        "port",
        "preflight",
        "preserve_task_state",
        "preserved_task_state",
        "present",
        "profile",
        "policy_expected",
        "read_only",
        "reason",
        "relative_log",
        "required_by",
        "root",
        "root_tasks",
        "safe",
        "section",
        "sequence",
        "session_id",
        "state",
        "status",
        "steps",
        "task",
        "task_cleanup",
        "task_lifecycle",
        "task_policy",
        "tasks",
        "tasks_reset",
        "timestamp",
        "tool",
        "type",
        "valid",
        "validation",
        "value",
        "mode",
        "phase",
        "cleanup_required",
    }
)

_SAFE_RESULT_KEYS = frozenset(
    {"ok", "code", "message", "state", "session_id", "details"}
)
_SAFE_PREFLIGHT_CHECK_KEYS = frozenset({"name", "ok", "code", "message"})
_SAFE_TASK_LIFECYCLE_KEYS = frozenset(
    {"mode", "phase", "cleanup_required", "policy_expected"}
)
_SAFE_TASK_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "present",
        "valid",
        "code",
        "state",
        "session_id",
        "profile",
        "root_tasks",
        "excluded_tasks",
        "allowed_tasks",
        "catalog",
        "dependencies",
        "created_at",
        "updated_at",
    }
)
_SAFE_TASK_PROVENANCE_KEYS = frozenset(
    {"task", "required_by", "root", "reason", "sequence", "timestamp"}
)
_SAFE_TASK_DESCRIPTOR_KEYS = frozenset(
    {"section", "command", "enabled", "next_run"}
)
_SAFE_TASK_CATALOG_KEYS = frozenset({"profile", "tasks"})
_SAFE_TASK_PLAN_KEYS = frozenset(
    {"profile", "root_tasks", "excluded_tasks", "catalog"}
)
_SAFE_ERROR_KEYS = frozenset({"type", "code", "message", "field", "tasks"})

_SCHEMA_KEYS = {
    "details": _SAFE_DETAIL_KEYS,
    "result": _SAFE_RESULT_KEYS,
    "preflight_check": _SAFE_PREFLIGHT_CHECK_KEYS,
    "task_lifecycle": _SAFE_TASK_LIFECYCLE_KEYS,
    "task_policy": _SAFE_TASK_POLICY_KEYS,
    "task_provenance": _SAFE_TASK_PROVENANCE_KEYS,
    "task_descriptor": _SAFE_TASK_DESCRIPTOR_KEYS,
    "task_catalog": _SAFE_TASK_CATALOG_KEYS,
    "task_plan": _SAFE_TASK_PLAN_KEYS,
    "error": _SAFE_ERROR_KEYS,
}

_DETAIL_CHILD_SCHEMAS: dict[str, str | None] = {
    "allowed": "bool",
    "allowed_tasks": "string_list",
    "blockers": "string_list",
    "catalog": "catalog",
    "checks": "preflight_checks",
    "cleanup": "result",
    "cleanup_confirmed": "bool",
    "code": "string",
    "command": "string",
    "dependencies": "task_provenance_list",
    "details": "details",
    "enabled": "bool",
    "error": "error",
    "excluded_tasks": "string_list",
    "field": "string",
    "host": "string",
    "items": "generic_list",
    "lifecycle_marked_cleanup_pending": "bool",
    "log": "string",
    "message": "string",
    "name": "string",
    "new_dependency": "bool",
    "next_run": "string",
    "observed_code": "string",
    "plan": "task_plan",
    "policy_expected": "bool",
    "policy_marked": "bool",
    "policy_marked_cleanup_pending": "bool",
    "policy_removed": "bool",
    "policy_state": "string",
    "port": "int",
    "preflight": "result",
    "preserve_task_state": "bool",
    "preserved_task_state": "bool",
    "present": "bool",
    "profile": "string",
    "read_only": "bool",
    "reason": "string",
    "relative_log": "string",
    "required_by": "string",
    "root": "string",
    "root_tasks": "string_list",
    "safe": None,
    "section": "string",
    "sequence": "int",
    "session_id": "string",
    "state": "string",
    "status": "result",
    "steps": "result_list",
    "task": "string",
    "task_cleanup": "result",
    "task_lifecycle": "task_lifecycle",
    "task_policy": "task_policy",
    "tasks": "task_descriptor_list",
    "tasks_reset": "int",
    "timestamp": "string",
    "tool": "string",
    "type": "string",
    "valid": "bool",
    "validation": "string",
}

_RESULT_CHILD_SCHEMAS: dict[str, str | None] = {
    "ok": "bool",
    "code": "string",
    "message": "string",
    "state": "string",
    "session_id": "string",
    "details": "details",
}

_TASK_POLICY_CHILD_SCHEMAS: dict[str, str | None] = {
    "schema_version": "int",
    "present": "bool",
    "valid": "bool",
    "code": "string",
    "state": "string",
    "session_id": "string",
    "profile": "string",
    "root_tasks": "string_list",
    "excluded_tasks": "string_list",
    "allowed_tasks": "string_list",
    "catalog": "string_list",
    "dependencies": "task_provenance_list",
    "created_at": "string",
    "updated_at": "string",
}

_TASK_PROVENANCE_CHILD_SCHEMAS: dict[str, str | None] = {
    "task": "string",
    "required_by": "string",
    "root": "string",
    "reason": "string",
    "sequence": "int",
    "timestamp": "string",
}

_TASK_DESCRIPTOR_CHILD_SCHEMAS: dict[str, str | None] = {
    "section": "string",
    "command": "string",
    "enabled": "bool",
    "next_run": "string",
}

_TASK_CATALOG_CHILD_SCHEMAS: dict[str, str | None] = {
    "profile": "string",
    "tasks": "task_descriptor_list",
}

_TASK_PLAN_CHILD_SCHEMAS: dict[str, str | None] = {
    "profile": "string",
    "root_tasks": "string_list",
    "excluded_tasks": "string_list",
    "catalog": "string_list",
}

_ERROR_CHILD_SCHEMAS: dict[str, str | None] = {
    "type": "string",
    "code": "string",
    "message": "string",
    "field": "string",
    "tasks": "string_list",
}

_SCHEMA_CHILD_SCHEMAS = {
    "details": _DETAIL_CHILD_SCHEMAS,
    "result": _RESULT_CHILD_SCHEMAS,
    "task_lifecycle": {
        "mode": "string",
        "phase": "string",
        "cleanup_required": "bool",
        "policy_expected": "bool",
    },
    "task_policy": _TASK_POLICY_CHILD_SCHEMAS,
    "task_provenance": _TASK_PROVENANCE_CHILD_SCHEMAS,
    "task_descriptor": _TASK_DESCRIPTOR_CHILD_SCHEMAS,
    "task_catalog": _TASK_CATALOG_CHILD_SCHEMAS,
    "task_plan": _TASK_PLAN_CHILD_SCHEMAS,
    "error": _ERROR_CHILD_SCHEMAS,
}


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


def _redact_quoted_assignment(match: re.Match[str]) -> str:
    return (
        f"{match.group('key_quote')}{match.group('key')}{match.group('key_quote')}"
        f"{match.group('separator')}{match.group('value_quote')}"
        f"{match.group('bearer') or ''}***{match.group('value_quote')}"
    )


def _redact_quoted_value(match: re.Match[str]) -> str:
    return (
        f"{match.group('key')}{match.group('separator')}"
        f"{match.group('value_quote')}{match.group('bearer') or ''}***"
        f"{match.group('value_quote')}"
    )


def _redact_assignment(match: re.Match[str]) -> str:
    return (
        f"{match.group('key')}{match.group('separator')}"
        f"{match.group('bearer') or ''}***"
    )


def _redact_text(value: str) -> str:
    value = _URL_USERINFO.sub(r"\g<scheme>***@", value)
    value = _SENSITIVE_QUERY.sub(r"\1***", value)
    value = _SENSITIVE_QUOTED_ASSIGNMENT.sub(_redact_quoted_assignment, value)
    value = _SENSITIVE_QUOTED_VALUE.sub(_redact_quoted_value, value)
    value = _SENSITIVE_ASSIGNMENT.sub(_redact_assignment, value)
    value = _FILE_URI_PATH.sub("file:///[путь скрыт]", value)
    value = _ABSOLUTE_PATH.sub("[путь скрыт]", value)
    if len(value) > _MAX_RESULT_TEXT:
        return value[:_MAX_RESULT_TEXT] + "…"
    return value


def _safe_schema_key(key: object, allowed_keys: frozenset[str]) -> str | None:
    if not isinstance(key, str) or not key or len(key) > _MAX_RESULT_KEY:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized if normalized in allowed_keys else None


def _safe_mapping(
    value: Mapping[object, object],
    *,
    schema: str,
    depth: int,
) -> dict[str, object]:
    allowed_keys = _SCHEMA_KEYS.get(schema)
    if allowed_keys is None:
        return {}

    child_schemas = _SCHEMA_CHILD_SCHEMAS.get(schema, {})
    safe: dict[str, object] = {}
    for index, (raw_key, raw_value) in enumerate(value.items()):
        if index >= _MAX_RESULT_ITEMS:
            break
        key = _safe_schema_key(raw_key, allowed_keys)
        if key is None:
            continue
        safe[key] = _safe_value(
            raw_value,
            schema=child_schemas.get(key),
            depth=depth + 1,
        )
    return safe


def _safe_sequence(
    value: list[object] | tuple[object, ...],
    *,
    item_schema: str | None,
    depth: int,
) -> list[object]:
    safe: list[object] = []
    for item in value[:_MAX_RESULT_ITEMS]:
        if item_schema == "string" and not isinstance(item, str):
            continue
        if item_schema == "bool" and not isinstance(item, bool):
            continue
        if item_schema == "int" and (not isinstance(item, int) or isinstance(item, bool)):
            continue
        safe.append(_safe_value(item, schema=item_schema, depth=depth + 1))
    return safe


def _safe_value(
    value: object,
    *,
    schema: str | None = None,
    depth: int = 0,
) -> object:
    if depth > _MAX_RESULT_DEPTH:
        return "[вложенность скрыта]"
    if schema == "string":
        return _redact_text(value) if isinstance(value, str) else None
    if schema == "bool":
        return value if isinstance(value, bool) else None
    if schema == "int":
        return (
            value
            if isinstance(value, int)
            and not isinstance(value, bool)
            and abs(value) <= 10**12
            else None
        )

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 10**12 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        if schema == "catalog":
            return _safe_mapping(value, schema="task_catalog", depth=depth)
        if schema is None:
            return {}
        return _safe_mapping(value, schema=schema, depth=depth)
    if isinstance(value, (list, tuple)):
        if schema == "catalog":
            return _safe_sequence(value, item_schema="string", depth=depth)
        item_schema = {
            "generic_list": None,
            "preflight_checks": "preflight_check",
            "result_list": "result",
            "string_list": "string",
            "task_descriptor_list": "task_descriptor",
            "task_provenance_list": "task_provenance",
        }.get(schema)
        if schema not in {
            "generic_list",
            "preflight_checks",
            "result_list",
            "string_list",
            "task_descriptor_list",
            "task_provenance_list",
        }:
            return []
        return _safe_sequence(value, item_schema=item_schema, depth=depth)
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
    details = _safe_value(raw_details, schema="details")
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
        except Exception as exc:  # noqa: BLE001 - boundary must sanitize runtime failures
            logger.error(
                "[Dev MCP] tool %s завершился неожиданной ошибкой: %s",
                tool_name,
                type(exc).__name__,
            )
            return _internal_error()
        return serialize_dev_result(result)


__all__ = ["DEV_MCP_TOOL_NAMES", "DevMcpAdapter", "serialize_dev_result"]
