"""Строгий transport adapter standalone Game MCP read/control plane."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from io import BytesIO
from pathlib import Path
from threading import Condition, RLock
from uuid import UUID

from module.application.errors import (
    ApplicationError,
    ConfigurationValidationError,
    IncompatibleSchemaError,
    InstanceNotRunningError,
    InvalidRequestError,
    OperationFailedError,
    OwnershipAmbiguousError,
    PostconditionFailedError,
    PreconditionFailedError,
    ResourceBusyError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    StorageAuthenticationError,
    StorageConfigurationError,
    StorageError,
    StorageUnavailableError,
)
from module.application.fleet_state import FleetStateObservation, FleetStateResult
from module.application.game_control_lock import profile_mutation_lock
from module.application.game_models import (
    AdbRestartResult,
    ConfigSnapshot,
    ConfigUpdateRequest,
    ConfigUpdateResult,
    CurrentTaskSnapshot,
    DashboardResources,
    EmulatorRestartResult,
    LifecycleOutcome,
    LifecycleResult,
    MediaFrame,
    RuntimeLogTail,
    SchedulerEntry,
    SchedulerQueueClearResult,
    SchedulerQueueSnapshot,
    ScheduleTaskRequest,
    ScheduleTaskResult,
    thaw_payload,
)
from module.application.game_validation import (
    INVALID_NAME_CHARS,
    MAX_NAME_LENGTH,
    UNKNOWN_TASK,
    validate_json_value,
)
from module.application.models import (
    InstanceReference,
    InstanceStatus,
    RuntimeState,
    TaskArgumentMetadata,
    TaskGroupMetadata,
    TaskMetadata,
    TaskOption,
    TaskSummary,
)
from module.application.morale import (
    MoraleFleetState,
    MoraleKnowledge,
    MoraleRecoveryProfile,
    MoraleSelectionState,
    MoraleSlotState,
)
from module.formation.model import (
    SUPPORTED_SURFACE_FLEET_INDICES,
    FleetSelection,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.game_mcp.contract import (
    GAME_MCP_CONTROL_SCOPE,
    GAME_MCP_NO_ARGUMENT_TOOLS,
    GAME_MCP_READ_SCOPE,
    contract_result,
)

logger = logging.getLogger(__name__)

GAME_MCP_READ_TOOL_NAMES = (
    "game_get_contract",
    "game_list_profiles",
    "game_get_profile_status",
    "game_get_resources",
    "game_get_current_task",
    "game_get_scheduler_queue",
    "game_list_tasks",
    "game_get_task_help",
    "game_get_fleet_state",
    "game_get_morale",
    "game_get_config",
    "game_get_recent_logs",
    "game_get_screenshot",
)
GAME_MCP_CONTROL_TOOL_NAMES = (
    "game_start_profile",
    "game_stop_profile",
    "game_trigger_task",
    "game_clear_scheduler_queue",
    "game_update_config",
    "game_restart_emulator",
    "game_restart_adb",
)
GAME_MCP_TOOL_NAMES = GAME_MCP_READ_TOOL_NAMES + GAME_MCP_CONTROL_TOOL_NAMES
GAME_MCP_TOOL_REQUIRED_SCOPES = {
    **{name: GAME_MCP_READ_SCOPE for name in GAME_MCP_READ_TOOL_NAMES},
    **{name: GAME_MCP_CONTROL_SCOPE for name in GAME_MCP_CONTROL_TOOL_NAMES},
}

_MAX_PROFILE_COUNT = 256
_MAX_TASK_COUNT = 512
_MAX_SELECTION_SIZE = len(SUPPORTED_SURFACE_FLEET_INDICES)
_MAX_PUBLIC_LOG_LINES = 200
_MAX_PUBLIC_LOG_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 256 * 1024
_MAX_RESULT_DEPTH = 8
_MAX_RESULT_ITEMS = 256
_MAX_RESULT_SEQUENCE_ITEMS = 512
_MAX_RESULT_STRING = 4096
_MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
_MAX_SCREENSHOT_WIDTH = 8192
_MAX_SCREENSHOT_HEIGHT = 8192
_MAX_SCREENSHOT_PIXELS = 16_777_216
_ALLOWED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg"})
_MUTATION_LOCK_TIMEOUT_SECONDS = 30.0
_SECRET_KEY_PARTS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "secret",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "privatekey",
        "signingkey",
        "cookie",
        "passfile",
        "credential",
        "credentials",
        "authorization",
        "dsn",
        "sessionid",
        "oauth",
    }
)
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:/])(?:"
    r"\\\\[^\\/\s'\"<>]+[\\/][^\s'\"<>]+"
    r"|[A-Za-z]:[\\/][^\s'\"<>]+[\\/][^\s'\"<>]+"
    r"|/(?:[^/\s'\"<>]+/)+[^/\s'\"<>]+(?:[\\/][^\s'\"<>]+)*"
    r")"
)
_TRACEBACK_RE = re.compile(
    r"(?:traceback \(most recent call last\)|\bfile\s+[\"'])", re.IGNORECASE
)
_BEARER_RE = re.compile(r"(\bbearer\s+)[^\s,;]+", re.IGNORECASE)
_SECRET_NAME_RE = (
    r"(?:[A-Za-z][A-Za-z0-9_-]*?)?"
    r"(?:password|passwd|token|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|private[_-]?key|signing[_-]?key|"
    r"cookie|passfile|credential|credentials|authorization|dsn|session[_-]?id|oauth)"
    r"[A-Za-z0-9_-]*"
)
_SECRET_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_-])(?P<key>"
    + _SECRET_NAME_RE
    + r")[\"']?\s*[:=]\s*[\"']?)(?P<value>[^\"'\s,;}\]]+)(?P<quote>[\"']?)",
    re.IGNORECASE,
)


class _UnknownTaskError(ResourceNotFoundError):
    """Внутренняя метка для различения unknown task и unknown profile."""


class _UnknownConfigError(ResourceNotFoundError):
    """Внутренняя метка для различения unknown config и unknown profile."""


class _ResultLimitExceeded(ValueError):
    """Внутренняя метка для явного отказа без потери элементов ответа."""


@dataclass(frozen=True, slots=True)
class GameMcpResponse:
    """Безопасный структурированный ответ с необязательным MCP image content."""

    structured: dict[str, object]
    image: bytes | None = None
    mime_type: str | None = None


def _default_backend() -> object:
    from module.game_mcp.composition import GameMcpBackend

    return GameMcpBackend()


def _enum_value(value: object) -> str | None:
    return (
        value.value
        if isinstance(value, Enum) and isinstance(value.value, str)
        else None
    )


def _safe_text(value: str, *, maximum: int = _MAX_RESULT_STRING) -> str:
    value = _ANSI_RE.sub("", value)
    value = "".join(
        char
        for char in value
        if char in {"\n", "\r", "\t"} or (ord(char) >= 32 and ord(char) != 127)
    )
    value = _BEARER_RE.sub(r"\1<скрыто>", value)
    value = _SECRET_VALUE_RE.sub(r"\g<prefix><скрыто>\g<quote>", value)
    value = _PATH_RE.sub("<путь скрыт>", value)
    return value[:maximum]


def _secret_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    compact = normalized.replace("_", "")
    return any(part in compact for part in _SECRET_KEY_PARTS)


def _safe_value(value: object, *, key: str | None = None, depth: int = 0) -> object:
    if key is not None and _secret_key(key):
        return "<скрыто>"
    if depth > _MAX_RESULT_DEPTH:
        return "<вложенность скрыта>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 10**12 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, Mapping):
        if len(value) > _MAX_RESULT_ITEMS:
            raise _ResultLimitExceeded("Ответ содержит слишком много полей.")
        result: dict[str, object] = {}
        for index, (raw_key, raw_item) in enumerate(value.items()):
            if index >= _MAX_RESULT_ITEMS:
                raise _ResultLimitExceeded("Ответ содержит слишком много полей.")
            if not isinstance(raw_key, str):
                raise TypeError("Ключ ответа должен быть строкой.")
            safe_key = _safe_text(raw_key, maximum=128)
            if not safe_key:
                continue
            result[safe_key] = _safe_value(raw_item, key=raw_key, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_RESULT_SEQUENCE_ITEMS:
            raise _ResultLimitExceeded("Ответ содержит слишком много элементов.")
        return [
            _safe_value(item, depth=depth + 1) for item in value
        ]
    return None


def _safe_uuid(value: UUID) -> str:
    if not isinstance(value, UUID):
        raise ServiceUnavailableError("Источник вернул некорректный идентификатор.")
    return str(value)


def _safe_datetime(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ServiceUnavailableError("Источник вернул некорректное время.")
    return value.isoformat()


def _result(
    *,
    ok: bool,
    code: str,
    message: str,
    state: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    try:
        safe_details = _safe_value(details or {})
    except _ResultLimitExceeded:
        return {
            "ok": False,
            "code": "GAME_RESULT_LIMIT_EXCEEDED",
            "message": "Ответ Game MCP превышает ограничение элементов",
            "state": "failed",
            "details": {},
        }
    except (TypeError, ValueError):
        return {
            "ok": False,
            "code": "GAME_MCP_INTERNAL_ERROR",
            "message": "Game MCP не смог сформировать безопасный ответ",
            "state": "failed",
            "details": {},
        }
    if not isinstance(safe_details, dict):
        safe_details = {}
    response: dict[str, object] = {
        "ok": ok,
        "code": code,
        "message": message,
        "state": state,
        "details": safe_details,
    }
    try:
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return {
            "ok": False,
            "code": "GAME_MCP_INTERNAL_ERROR",
            "message": "Game MCP не смог сформировать безопасный ответ",
            "state": "failed",
            "details": {},
        }
    if len(encoded.encode("utf-8")) > _MAX_RESULT_BYTES:
        return {
            "ok": False,
            "code": "GAME_RESPONSE_TOO_LARGE",
            "message": "Ответ Game MCP превышает безопасный размер",
            "state": "failed",
            "details": {},
        }
    return response


def _ok(
    code: str, message: str, state: str, details: Mapping[str, object]
) -> dict[str, object]:
    return _result(ok=True, code=code, message=message, state=state, details=details)


def _error(code: str, message: str, *, tool: str | None = None) -> dict[str, object]:
    details = {"tool": tool} if tool is not None else {}
    return _result(
        ok=False, code=code, message=message, state="failed", details=details
    )


def _invalid(tool: str) -> dict[str, object]:
    return _error(
        "GAME_MCP_INVALID_REQUEST",
        "Аргументы Game MCP не прошли строгую проверку",
        tool=tool,
    )


def _unknown_tool(tool: object) -> dict[str, object]:
    return _error(
        "GAME_MCP_UNKNOWN_TOOL",
        "Запрошенный инструмент Game MCP не существует",
        tool=(
            _safe_text(tool, maximum=MAX_NAME_LENGTH)
            if isinstance(tool, str)
            else None
        ),
    )


def _arguments(arguments: Mapping[str, object] | None) -> dict[str, object]:
    if arguments is None:
        return {}
    if not isinstance(arguments, Mapping):
        raise InvalidRequestError("Аргументы должны быть JSON-объектом.")
    if any(not isinstance(key, str) for key in arguments):
        raise InvalidRequestError("Имена аргументов должны быть строками.")
    return dict(arguments)


def _check_keys(
    arguments: dict[str, object],
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> None:
    if set(arguments) - allowed or required - set(arguments):
        raise InvalidRequestError(
            "Набор аргументов не соответствует схеме инструмента."
        )


def _public_name(value: object, *, resource: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidRequestError(f"Имя {resource} должно быть канонической строкой.")
    if len(value) > MAX_NAME_LENGTH or value in {".", ".."}:
        raise InvalidRequestError(f"Имя {resource} содержит недопустимое значение.")
    if any(
        char in INVALID_NAME_CHARS or ord(char) < 32 or ord(char) == 127
        for char in value
    ):
        raise InvalidRequestError(f"Имя {resource} содержит недопустимое значение.")
    return value


def _profile_arguments(arguments: dict[str, object]) -> str:
    _check_keys(
        arguments, allowed=frozenset({"profile"}), required=frozenset({"profile"})
    )
    return _public_name(arguments["profile"], resource="профиля")


def _task_arguments(arguments: dict[str, object]) -> str:
    _check_keys(arguments, allowed=frozenset({"task"}), required=frozenset({"task"}))
    return _public_name(arguments["task"], resource="задачи")


def _selection_arguments(arguments: dict[str, object]) -> tuple[str, tuple[int, ...]]:
    _check_keys(
        arguments,
        allowed=frozenset({"profile", "fleet_indices"}),
        required=frozenset({"profile", "fleet_indices"}),
    )
    profile = _profile_arguments({"profile": arguments["profile"]})
    raw_indices = arguments["fleet_indices"]
    if (
        isinstance(raw_indices, (str, bytes))
        or not isinstance(raw_indices, Sequence)
        or not raw_indices
        or len(raw_indices) > _MAX_SELECTION_SIZE
        or any(type(index) is not int for index in raw_indices)
    ):
        raise InvalidRequestError(
            "fleet_indices должен быть ограниченным массивом индексов."
        )
    indices = tuple(raw_indices)
    if len(indices) != len(set(indices)) or any(
        index not in SUPPORTED_SURFACE_FLEET_INDICES for index in indices
    ):
        raise InvalidRequestError(
            "fleet_indices содержит недопустимый или повторный индекс."
        )
    return profile, indices


def _validate_arguments(
    tool: str, arguments: Mapping[str, object] | None
) -> tuple[dict[str, object], tuple[str, tuple[int, ...]] | None]:
    raw = _arguments(arguments)
    selection: tuple[str, tuple[int, ...]] | None = None
    if tool in GAME_MCP_NO_ARGUMENT_TOOLS:
        _check_keys(raw, allowed=frozenset())
    elif tool in {
        "game_get_profile_status",
        "game_get_resources",
        "game_get_current_task",
        "game_get_scheduler_queue",
        "game_get_screenshot",
    }:
        _check_keys(
            raw, allowed=frozenset({"profile"}), required=frozenset({"profile"})
        )
        _profile_arguments(raw)
    elif tool == "game_get_task_help":
        _task_arguments(raw)
    elif tool == "game_get_fleet_state" or tool == "game_get_morale":
        selection = _selection_arguments(raw)
    elif tool == "game_get_config":
        _check_keys(
            raw, allowed=frozenset({"profile", "task"}), required=frozenset({"profile"})
        )
        _profile_arguments({"profile": raw["profile"]})
        if "task" in raw:
            _public_name(raw["task"], resource="задачи")
    elif tool == "game_get_recent_logs":
        _check_keys(
            raw,
            allowed=frozenset({"profile", "lines"}),
            required=frozenset({"profile"}),
        )
        _profile_arguments({"profile": raw["profile"]})
        lines = raw.get("lines", 50)
        if type(lines) is not int or not 0 <= lines <= _MAX_PUBLIC_LOG_LINES:
            raise InvalidRequestError(
                f"lines должен быть целым числом от 0 до {_MAX_PUBLIC_LOG_LINES}."
            )
    elif tool in {
        "game_start_profile",
        "game_stop_profile",
        "game_clear_scheduler_queue",
        "game_restart_emulator",
        "game_restart_adb",
    }:
        _profile_arguments(raw)
    elif tool == "game_trigger_task":
        _check_keys(
            raw,
            allowed=frozenset({"profile", "task"}),
            required=frozenset({"profile", "task"}),
        )
        _profile_arguments({"profile": raw["profile"]})
        _task_arguments({"task": raw["task"]})
    elif tool == "game_update_config":
        _check_keys(
            raw,
            allowed=frozenset({"profile", "task", "group", "argument", "value"}),
            required=frozenset(
                {"profile", "task", "group", "argument", "value"}
            ),
        )
        _profile_arguments({"profile": raw["profile"]})
        for key, resource in (
            ("task", "задачи"),
            ("group", "группы"),
            ("argument", "аргумента"),
        ):
            _public_name(raw[key], resource=resource)
        try:
            validate_json_value(raw["value"])
        except (TypeError, ValueError):
            raise InvalidRequestError(
                "Значение конфигурации не прошло bounded JSON-проверку."
            ) from None
    else:
        raise InvalidRequestError("Для инструмента отсутствует строгая схема.")
    return raw, selection


def _resource_payload(resources: DashboardResources) -> list[dict[str, object]]:
    if (
        not isinstance(resources, DashboardResources)
        or len(resources.items) > _MAX_RESULT_ITEMS
    ):
        raise ServiceUnavailableError("Источник вернул некорректные ресурсы.")
    payload = []
    for resource in resources.items:
        payload.append(
            {
                "key": resource.key,
                "label": resource.label,
                "value": thaw_payload(resource.value),
                **(
                    {"limit": thaw_payload(resource.limit)}
                    if resource.limit is not None
                    else {}
                ),
                **(
                    {"total": thaw_payload(resource.total)}
                    if resource.total is not None
                    else {}
                ),
                **(
                    {"last_update": thaw_payload(resource.last_update)}
                    if resource.last_update is not None
                    else {}
                ),
            }
        )
    return payload


def _task_summary_payload(tasks: Sequence[TaskSummary]) -> list[dict[str, object]]:
    if (
        isinstance(tasks, (str, bytes))
        or not isinstance(tasks, Sequence)
        or len(tasks) > _MAX_TASK_COUNT
        or any(not isinstance(task, TaskSummary) for task in tasks)
    ):
        raise ServiceUnavailableError("Источник вернул некорректный каталог задач.")
    return [
        {"name": task.name, "display_name": task.display_name, "help": task.help}
        for task in tasks
    ]


def _task_metadata_payload(task: TaskMetadata) -> dict[str, object]:
    if not isinstance(task, TaskMetadata):
        raise ServiceUnavailableError("Источник вернул некорректную metadata задачи.")
    if len(task.groups) > _MAX_RESULT_ITEMS:
        raise ServiceUnavailableError("Metadata задачи превышает безопасный размер.")
    groups = []
    for group in task.groups:
        if (
            not isinstance(group, TaskGroupMetadata)
            or len(group.arguments) > _MAX_RESULT_ITEMS
        ):
            raise ServiceUnavailableError("Источник вернул некорректную группу задачи.")
        arguments = []
        for argument in group.arguments:
            if (
                not isinstance(argument, TaskArgumentMetadata)
                or len(argument.options) > _MAX_RESULT_ITEMS
            ):
                raise ServiceUnavailableError(
                    "Источник вернул некорректный аргумент задачи."
                )
            options = []
            for option in argument.options:
                if not isinstance(option, TaskOption):
                    raise ServiceUnavailableError(
                        "Источник вернул некорректный option задачи."
                    )
                options.append(
                    {
                        "value": _safe_value(option.value, key=argument.name),
                        "display_name": _safe_text(option.display_name),
                    }
                )
            arguments.append(
                {
                    "name": argument.name,
                    "display_name": argument.display_name,
                    "help": argument.help,
                    "input_type": argument.input_type,
                    "default": _safe_value(
                        thaw_payload(argument.default), key=argument.name
                    ),
                    "options": options,
                }
            )
        groups.append(
            {
                "name": group.name,
                "display_name": group.display_name,
                "help": group.help,
                "arguments": arguments,
            }
        )
    return {
        "name": task.name,
        "display_name": task.display_name,
        "help": task.help,
        "groups": groups,
    }


def _log_payload(logs: RuntimeLogTail) -> tuple[list[str], bool]:
    if not isinstance(logs, RuntimeLogTail):
        raise ServiceUnavailableError("Источник вернул некорректный журнал.")
    sanitized: list[str] = []
    bytes_used = 0
    bounded = logs.lines[-_MAX_PUBLIC_LOG_LINES:]
    truncated = len(bounded) != len(logs.lines)
    for raw_line in reversed(bounded):
        if _TRACEBACK_RE.search(raw_line):
            line = "[трассировка скрыта]\n"
        else:
            line = _safe_text(raw_line, maximum=8192)
        encoded_size = len(line.encode("utf-8"))
        if bytes_used + encoded_size > _MAX_PUBLIC_LOG_BYTES:
            truncated = True
            break
        sanitized.append(line)
        bytes_used += encoded_size
    if len(sanitized) != len(bounded):
        truncated = True
    sanitized.reverse()
    return sanitized, truncated


def _slot_payload(slot: FormationFleetSlotObservation) -> dict[str, object]:
    if not isinstance(slot, FormationFleetSlotObservation):
        raise ServiceUnavailableError("Источник вернул некорректный fleet slot.")
    payload: dict[str, object] = {
        "side": _enum_value(slot.side),
        "position": slot.position,
        "occupied": slot.occupied,
        "identity_status": _enum_value(slot.identity_status),
    }
    for key, value in (
        ("raw_name_ocr", slot.raw_name_ocr),
        ("displayed_name", slot.displayed_name),
        ("canonical_name", slot.canonical_name),
    ):
        if value is not None:
            payload[key] = _safe_text(value)
    if slot.canonical_identity is not None:
        payload["canonical_identity"] = _safe_text(
            slot.canonical_identity.key, maximum=128
        )
    if slot.ship_form is not None:
        payload["ship_form"] = _enum_value(slot.ship_form)
    return payload


def _snapshot_payload(snapshot: FormationFleetSnapshot) -> dict[str, object]:
    if not isinstance(snapshot, FormationFleetSnapshot):
        raise ServiceUnavailableError(
            "Источник вернул некорректный formation snapshot."
        )
    return {
        "fleet_index": snapshot.fleet_index,
        "complete": snapshot.complete,
        "occupied_count": snapshot.occupied_count,
        "catalog_fingerprint": snapshot.catalog_fingerprint,
        "slots": [_slot_payload(slot) for slot in snapshot.slots],
    }


def _observation_payload(observation: FleetStateObservation) -> dict[str, object]:
    if not isinstance(observation, FleetStateObservation):
        raise ServiceUnavailableError(
            "Источник вернул некорректное Fleet State observation."
        )
    return {
        "id": _safe_uuid(observation.id),
        "run_id": _safe_uuid(observation.run_id),
        "idempotency_key": _safe_text(observation.idempotency_key, maximum=128),
        "fleet_index": observation.fleet_index,
        "observed_at": _safe_datetime(observation.observed_at),
        "snapshot": _snapshot_payload(observation.snapshot),
    }


def _fleet_state_payload(
    result: FleetStateResult,
) -> tuple[dict[str, object], str, str]:
    if not isinstance(result, FleetStateResult):
        raise ServiceUnavailableError("Источник вернул некорректный Fleet State.")
    observations = [_observation_payload(item) for item in result.observations]
    missing = list(result.missing_fleet_indices)
    if not observations:
        code, state = "GAME_DATA_UNKNOWN", "unknown"
    elif missing:
        code, state = "GAME_FLEET_STATE_PARTIAL", "partial"
    else:
        code, state = "GAME_FLEET_STATE_READY", "ready"
    details = {
        "selection": list(result.request.selection.fleet_indices),
        "observations": observations,
        "missing_fleet_indices": missing,
        "coverage_complete": not missing,
        "snapshots_complete": bool(observations)
        and all(item["snapshot"]["complete"] for item in observations),
    }
    return details, code, state


def _recovery_payload(
    recovery: MoraleRecoveryProfile | None,
) -> dict[str, object] | None:
    if recovery is None:
        return None
    if not isinstance(recovery, MoraleRecoveryProfile):
        raise ServiceUnavailableError("Источник вернул некорректный morale recovery.")
    return {
        "recovery_per_hour": recovery.recovery_per_hour,
        "recovery_ceiling": recovery.recovery_ceiling,
        "source": recovery.source,
    }


def _morale_slot_payload(slot: MoraleSlotState) -> dict[str, object]:
    if not isinstance(slot, MoraleSlotState):
        raise ServiceUnavailableError("Источник вернул некорректный morale slot.")
    payload: dict[str, object] = {
        "fleet_index": slot.fleet_index,
        "side": _enum_value(slot.side),
        "position": slot.position,
        "occupied": slot.occupied,
        "identity_status": _enum_value(slot.identity_status),
        "knowledge": _enum_value(slot.knowledge),
        "baseline": slot.baseline,
        "current": slot.current,
        "recovery": _recovery_payload(slot.recovery),
        "observed_at": (
            _safe_datetime(slot.observed_at) if slot.observed_at is not None else None
        ),
        "source": _safe_text(slot.source) if slot.source is not None else None,
        "morale_observation_id": (
            _safe_uuid(slot.morale_observation_id)
            if slot.morale_observation_id is not None
            else None
        ),
        "location": _enum_value(slot.location),
        "dorm_scan_id": (
            _safe_uuid(slot.dorm_scan_id) if slot.dorm_scan_id is not None else None
        ),
    }
    if slot.canonical_identity is not None:
        payload["canonical_identity"] = _safe_text(
            slot.canonical_identity.key, maximum=128
        )
    if slot.canonical_name is not None:
        payload["canonical_name"] = _safe_text(slot.canonical_name)
    if slot.ship_form is not None:
        payload["ship_form"] = _enum_value(slot.ship_form)
    return payload


def _morale_fleet_payload(fleet: MoraleFleetState) -> dict[str, object]:
    if not isinstance(fleet, MoraleFleetState):
        raise ServiceUnavailableError(
            "Источник вернул некорректное morale состояние флота."
        )
    return {
        "fleet_index": fleet.fleet_index,
        "formation_observation_id": (
            _safe_uuid(fleet.formation_observation_id)
            if fleet.formation_observation_id is not None
            else None
        ),
        "formation_observed_at": (
            _safe_datetime(fleet.formation_observed_at)
            if fleet.formation_observed_at is not None
            else None
        ),
        "slots": [_morale_slot_payload(slot) for slot in fleet.slots],
    }


def _morale_payload(result: MoraleSelectionState) -> tuple[dict[str, object], str, str]:
    if not isinstance(result, MoraleSelectionState):
        raise ServiceUnavailableError("Источник вернул некорректное morale состояние.")
    fleets = [_morale_fleet_payload(fleet) for fleet in result.fleets]
    known_slots = [
        slot
        for fleet in result.fleets
        for slot in fleet.slots
        if slot.knowledge is not MoraleKnowledge.UNKNOWN
    ]
    has_formation = any(
        fleet.formation_observation_id is not None for fleet in result.fleets
    )
    if not known_slots and not has_formation:
        code, state = "GAME_DATA_UNKNOWN", "unknown"
    elif all(
        slot.knowledge is not MoraleKnowledge.UNKNOWN
        for fleet in result.fleets
        for slot in fleet.slots
        if slot.occupied is not False
    ):
        code, state = "GAME_MORALE_READY", "ready"
    else:
        code, state = "GAME_MORALE_PARTIAL", "partial"
    return (
        {
            "selection": list(result.selection.fleet_indices),
            "projected_at": _safe_datetime(result.projected_at),
            "fleets": fleets,
        },
        code,
        state,
    )


def _validate_media(frame: MediaFrame) -> tuple[bytes, str, int, int]:
    if not isinstance(frame, MediaFrame):
        raise ServiceUnavailableError("Источник вернул некорректный кадр.")
    media_type = frame.media_type.casefold()
    if (
        media_type not in _ALLOWED_IMAGE_TYPES
        or len(frame.data) > _MAX_SCREENSHOT_BYTES
    ):
        raise ServiceUnavailableError(
            "Кадр не соответствует безопасному media contract."
        )
    try:
        from PIL import Image

        with Image.open(BytesIO(frame.data)) as image:
            width, height = image.size
            image.verify()
            image_format = image.format
    except Exception:  # noqa: BLE001 - media boundary hides decoder details.
        raise ServiceUnavailableError("Кадр не прошёл проверку формата.") from None
    expected_format = {"image/png": "PNG", "image/jpeg": "JPEG"}[media_type]
    if image_format != expected_format or not (
        1 <= width <= _MAX_SCREENSHOT_WIDTH
        and 1 <= height <= _MAX_SCREENSHOT_HEIGHT
        and width * height <= _MAX_SCREENSHOT_PIXELS
    ):
        raise ServiceUnavailableError(
            "Кадр превышает безопасные размеры или MIME contract."
        )
    return frame.data, media_type, width, height


def _control_service(backend: object) -> object:
    control = getattr(backend, "control", None)
    if control is None:
        raise ServiceUnavailableError("Game control capability недоступна.")
    return control


def _control_lifecycle_result(
    tool: str,
    profile: str,
    control: object,
) -> dict[str, object]:
    method_name = "start_instance" if tool == "game_start_profile" else "stop_instance"
    method = getattr(control, method_name, None)
    if not callable(method):
        raise ServiceUnavailableError("Game lifecycle capability недоступна.")
    result = method(profile)
    if not isinstance(result, LifecycleResult) or result.instance != profile:
        raise ServiceUnavailableError("Lifecycle owner вернул некорректный результат.")
    expected = (
        {LifecycleOutcome.STARTED, LifecycleOutcome.ALREADY_RUNNING}
        if tool == "game_start_profile"
        else {LifecycleOutcome.STOPPED, LifecycleOutcome.ALREADY_STOPPED}
    )
    if result.outcome not in expected:
        raise ServiceUnavailableError("Lifecycle owner вернул некорректный результат.")
    if result.outcome is LifecycleOutcome.STARTED:
        code = "GAME_PROFILE_STARTED"
    elif result.outcome is LifecycleOutcome.ALREADY_RUNNING:
        code = "GAME_PROFILE_ALREADY_RUNNING"
    elif result.outcome is LifecycleOutcome.STOPPED:
        code = "GAME_PROFILE_STOPPED"
    else:
        code = "GAME_PROFILE_ALREADY_STOPPED"
    state = "running" if result.outcome in {
        LifecycleOutcome.STARTED,
        LifecycleOutcome.ALREADY_RUNNING,
    } else "stopped"
    return _ok(
        code,
        "Профиль запущен" if state == "running" else "Профиль остановлен",
        state,
        {"profile": profile, "outcome": result.outcome.value},
    )


def _control_schedule_result(profile: str, control: object, arguments: dict[str, object]) -> dict[str, object]:
    method = getattr(control, "trigger_task", None)
    if not callable(method):
        raise ServiceUnavailableError("Game scheduler capability недоступна.")
    task = _public_name(arguments["task"], resource="задачи")
    try:
        result = method(ScheduleTaskRequest(profile, task))
    except ResourceNotFoundError:
        raise _UnknownTaskError("Задача не найдена.") from None
    if (
        not isinstance(result, ScheduleTaskResult)
        or result.request.instance != profile
        or result.request.task != task
        or result.verified is not True
    ):
        raise PostconditionFailedError("Планирование задачи не подтверждено.")
    scheduled_at = _safe_datetime(result.scheduled_at)
    return _ok(
        "GAME_TASK_SCHEDULED",
        "Задача поставлена в scheduler",
        "scheduled",
        {
            "profile": profile,
            "task": task,
            "scheduled_at": scheduled_at,
            "verified": True,
        },
    )


def _control_clear_result(profile: str, control: object) -> dict[str, object]:
    method = getattr(control, "clear_scheduler_queue", None)
    if not callable(method):
        raise ServiceUnavailableError("Game scheduler capability недоступна.")
    result = method(profile)
    if (
        not isinstance(result, SchedulerQueueClearResult)
        or result.instance != profile
        or result.verified is not True
    ):
        raise PostconditionFailedError("Очистка очереди scheduler не подтверждена.")
    cleared = [_public_name(task, resource="задачи") for task in result.cleared_tasks]
    return _ok(
        "GAME_SCHEDULER_QUEUE_CLEARED",
        "Очередь scheduler очищена",
        "ready",
        {
            "profile": profile,
            "cleared_tasks": cleared,
            "cleared_count": len(cleared),
            "verified": True,
        },
    )


def _control_config_result(
    profile: str,
    control: object,
    arguments: dict[str, object],
) -> dict[str, object]:
    method = getattr(control, "update_config", None)
    if not callable(method):
        raise ServiceUnavailableError("Game configuration capability недоступна.")
    task = _public_name(arguments["task"], resource="задачи")
    group = _public_name(arguments["group"], resource="группы")
    argument = _public_name(arguments["argument"], resource="аргумента")
    try:
        result = method(
            ConfigUpdateRequest(
                instance=profile,
                task=task,
                group=group,
                argument=argument,
                value=arguments["value"],
            )
        )
    except ResourceNotFoundError:
        raise _UnknownConfigError("Параметр конфигурации не найден.") from None
    if (
        not isinstance(result, ConfigUpdateResult)
        or result.request.instance != profile
        or result.request.task != task
        or result.request.group != group
        or result.request.argument != argument
        or result.verified is not True
    ):
        raise PostconditionFailedError("Изменение конфигурации не подтверждено.")
    return _ok(
        "GAME_CONFIG_UPDATED",
        "Конфигурация обновлена",
        "ready",
        {
            "profile": profile,
            "task": task,
            "group": group,
            "argument": argument,
            "verified": True,
        },
    )


def _control_restart_result(
    tool: str,
    profile: str,
    control: object,
) -> dict[str, object]:
    method_name = (
        "restart_emulator" if tool == "game_restart_emulator" else "restart_adb"
    )
    method = getattr(control, method_name, None)
    if not callable(method):
        raise ServiceUnavailableError("Game restart capability недоступна.")
    result = method(profile)
    result_type = (
        EmulatorRestartResult
        if tool == "game_restart_emulator"
        else AdbRestartResult
    )
    if not isinstance(result, result_type) or result.instance != profile:
        raise ServiceUnavailableError("Restart owner вернул некорректный результат.")
    return _ok(
        "GAME_EMULATOR_RESTARTED"
        if tool == "game_restart_emulator"
        else "GAME_ADB_RESTARTED",
        "Эмулятор перезапущен"
        if tool == "game_restart_emulator"
        else "ADB перезапущен",
        "ready",
        {"profile": profile, "verified": True},
    )


def _current_request_scopes() -> tuple[str, ...] | None:
    """Вернуть scopes remote principal; None означает local stdio authority."""

    from module.mcp_shared.auth import current_access_token

    access_token = current_access_token()
    if access_token is None:
        return None
    scopes = getattr(access_token, "scopes", None)
    if isinstance(scopes, (str, bytes)) or not isinstance(scopes, Collection):
        return ()
    if any(not isinstance(scope, str) for scope in scopes):
        return ()
    return tuple(scopes)


def _authorized(
    tool: str,
    explicit_scopes: Collection[str] | None,
) -> bool:
    scopes = (
        tuple(explicit_scopes)
        if explicit_scopes is not None
        else _current_request_scopes()
    )
    if scopes is None:
        return True
    required_scope = GAME_MCP_TOOL_REQUIRED_SCOPES[tool]
    return required_scope in scopes


class GameMcpAdapter:
    """Маршрутизировать stateless read/control Game MCP tools."""

    def __init__(
        self,
        backend_factory: Callable[[], object] | object | None = None,
        *,
        mutation_lock_root: Path | str | None = None,
    ) -> None:
        if backend_factory is None:
            self._backend_factory: Callable[[], object] = _default_backend
        elif callable(backend_factory):
            self._backend_factory = backend_factory
        else:
            self._backend_factory = lambda: backend_factory
        self._backend: object | None = None
        self._backend_lock = RLock()
        self._backend_condition = Condition(self._backend_lock)
        self._active_calls = 0
        self._closing = False
        self._closed = False
        self._mutation_lock_root = mutation_lock_root

    def close(self) -> None:
        """Освободить ленивый persistence context, если он был создан."""

        with self._backend_condition:
            if self._closed:
                return
            self._closing = True
            self._closed = True
            try:
                while self._active_calls:
                    self._backend_condition.wait()
                backend = self._backend
                self._backend = None
                if backend is None:
                    return
                dispose = getattr(backend, "dispose", None)
                if callable(dispose):
                    dispose()
            finally:
                self._closing = False
                self._backend_condition.notify_all()

    def _get_backend(self) -> object:
        with self._backend_lock:
            if self._backend is None:
                backend = self._backend_factory()
                if backend is None:
                    raise ServiceUnavailableError("Game MCP backend недоступен.")
                self._backend = backend
            return self._backend

    def _is_closed(self) -> bool:
        with self._backend_lock:
            return self._closed

    def _acquire_backend(self) -> object:
        with self._backend_condition:
            while self._closing:
                self._backend_condition.wait()
            if self._closed:
                raise ServiceUnavailableError("Game MCP adapter закрыт.")
            backend = self._get_backend()
            self._active_calls += 1
            return backend

    def _release_backend(self) -> None:
        with self._backend_condition:
            self._active_calls -= 1
            if self._active_calls == 0:
                self._backend_condition.notify_all()

    def _acquire_mutation_lock(
        self,
        profile: str,
        backend: object,
    ) -> AbstractContextManager[None]:
        root = getattr(backend, "mutation_lock_root", None)
        if root is None:
            root = self._mutation_lock_root
        return profile_mutation_lock(
            profile,
            repository_root=root,
            timeout=_MUTATION_LOCK_TIMEOUT_SECONDS,
        )

    @staticmethod
    def _known_profile(backend: object, profile: str) -> str:
        instances = getattr(backend, "instances", None)
        list_instances = getattr(instances, "list_instances", None)
        if not callable(list_instances):
            raise ServiceUnavailableError("Каталог профилей недоступен.")
        values = list_instances()
        if (
            isinstance(values, (str, bytes))
            or not isinstance(values, Sequence)
            or len(values) > _MAX_PROFILE_COUNT
        ):
            raise ServiceUnavailableError("Каталог профилей имеет некорректный формат.")
        names = []
        for item in values:
            if not isinstance(item, InstanceReference):
                raise ServiceUnavailableError(
                    "Каталог профилей имеет некорректный формат."
                )
            names.append(_public_name(item.name, resource="профиля"))
        if profile not in names:
            raise ResourceNotFoundError("Профиль не найден.")
        return profile

    @staticmethod
    def _profile_from(arguments: dict[str, object]) -> str:
        return _public_name(arguments["profile"], resource="профиля")

    def _dispatch(
        self,
        tool: str,
        arguments: dict[str, object],
        backend: object,
        selection: tuple[str, tuple[int, ...]] | None = None,
        *,
        profile: str | None = None,
    ) -> GameMcpResponse | dict[str, object]:
        instances = getattr(backend, "instances", None)
        tasks = getattr(backend, "tasks", None)
        read = getattr(backend, "read", None)
        if tool == "game_list_profiles":
            values = instances.list_instances()
            if (
                isinstance(values, (str, bytes))
                or not isinstance(values, Sequence)
                or len(values) > _MAX_PROFILE_COUNT
                or any(not isinstance(item, InstanceReference) for item in values)
            ):
                raise ServiceUnavailableError(
                    "Каталог профилей имеет некорректный формат."
                )
            profiles = [
                {"profile": _public_name(item.name, resource="профиля")}
                for item in values
            ]
            return _ok(
                "GAME_PROFILES_READY",
                "Каталог профилей готов",
                "ready",
                {"profiles": profiles},
            )
        if tool == "game_list_tasks":
            return _ok(
                "GAME_TASKS_READY",
                "Каталог задач готов",
                "ready",
                {"tasks": _task_summary_payload(tasks.list_tasks())},
            )
        if tool == "game_get_task_help":
            task_name = _task_arguments(arguments)
            try:
                task = tasks.get_task_metadata(task_name)
            except ResourceNotFoundError:
                raise _UnknownTaskError("Задача не найдена.") from None
            if not isinstance(task, TaskMetadata):
                raise _UnknownTaskError("Задача не найдена.")
            return _ok(
                "GAME_TASK_HELP_READY",
                "Справка задачи готова",
                "ready",
                {"task": _task_metadata_payload(task)},
            )

        if profile is None:
            profile = self._profile_from(arguments)
        self._known_profile(backend, profile)
        if tool in GAME_MCP_CONTROL_TOOL_NAMES:
            control = _control_service(backend)
            if tool in {"game_start_profile", "game_stop_profile"}:
                return _control_lifecycle_result(tool, profile, control)
            if tool == "game_trigger_task":
                return _control_schedule_result(profile, control, arguments)
            if tool == "game_clear_scheduler_queue":
                return _control_clear_result(profile, control)
            if tool == "game_update_config":
                return _control_config_result(profile, control, arguments)
            if tool in {"game_restart_emulator", "game_restart_adb"}:
                return _control_restart_result(tool, profile, control)
            raise InvalidRequestError("Для control-инструмента отсутствует обработчик.")
        if tool == "game_get_profile_status":
            status = instances.get_status(profile)
            if not isinstance(status, InstanceStatus):
                raise ServiceUnavailableError(
                    "Источник вернул некорректный статус профиля."
                )
            state_names = {
                RuntimeState.RUNNING: "running",
                RuntimeState.STOPPED: "stopped",
                RuntimeState.WARNING: "warning",
                RuntimeState.UPDATING: "updating",
            }
            return _ok(
                "GAME_PROFILE_STATUS_READY",
                "Статус профиля готов",
                state_names.get(status.state, "unknown"),
                {
                    "profile": profile,
                    "running": status.running,
                    "state": state_names.get(status.state, "unknown"),
                },
            )
        if tool == "game_get_resources":
            resources = read.get_resources(profile)
            return _ok(
                "GAME_RESOURCES_READY",
                "Ресурсы профиля готовы",
                "ready",
                {"profile": profile, "resources": _resource_payload(resources)},
            )
        if tool == "game_get_current_task":
            result = read.get_current_running_task(profile)
            if not isinstance(result, CurrentTaskSnapshot):
                raise ServiceUnavailableError(
                    "Источник вернул некорректную текущую задачу."
                )
            task_unknown = result.task == UNKNOWN_TASK
            return _ok(
                "GAME_DATA_UNKNOWN" if task_unknown else "GAME_CURRENT_TASK_READY",
                "Текущая задача неизвестна."
                if task_unknown
                else "Текущая задача определена",
                "unknown" if task_unknown else "running",
                {"profile": profile, "task": result.task},
            )
        if tool == "game_get_scheduler_queue":
            result = read.get_scheduler_queue(profile)
            if not isinstance(result, SchedulerQueueSnapshot):
                raise ServiceUnavailableError(
                    "Источник вернул некорректную очередь scheduler."
                )
            if len(result.entries) > _MAX_RESULT_SEQUENCE_ITEMS or any(
                not isinstance(entry, SchedulerEntry) for entry in result.entries
            ):
                raise ServiceUnavailableError(
                    "Источник вернул некорректные элементы очереди scheduler."
                )
            entries = [
                {"task": entry.task, "next_run": thaw_payload(entry.next_run)}
                for entry in result.entries
            ]
            return _ok(
                "GAME_SCHEDULER_QUEUE_READY",
                "Очередь scheduler готова",
                "ready",
                {"profile": profile, "entries": entries},
            )
        if tool == "game_get_config":
            task_name = arguments.get("task")
            if task_name is not None:
                task_name = _public_name(task_name, resource="задачи")
                try:
                    task_metadata = tasks.get_task_metadata(task_name)
                except ResourceNotFoundError:
                    raise _UnknownTaskError("Задача не найдена.") from None
                if not isinstance(task_metadata, TaskMetadata):
                    raise _UnknownTaskError("Задача не найдена.")
            result = read.get_config(profile, task_name)
            if not isinstance(result, ConfigSnapshot):
                raise ServiceUnavailableError(
                    "Источник вернул некорректную конфигурацию."
                )
            return _ok(
                "GAME_CONFIG_READY",
                "Конфигурация профиля готова",
                "ready",
                {
                    "profile": profile,
                    "task": task_name,
                    "config": thaw_payload(result.data),
                },
            )
        if tool == "game_get_recent_logs":
            lines = arguments.get("lines", 50)
            result = read.get_recent_logs(profile, lines)
            values, truncated = _log_payload(result)
            return _ok(
                "GAME_LOGS_READY",
                "Журнал профиля готов",
                "ready",
                {"profile": profile, "lines": values, "truncated": truncated},
            )
        if tool == "game_get_screenshot":
            frame = read.get_screenshot(profile)
            data, media_type, width, height = _validate_media(frame)
            structured = _ok(
                "GAME_SCREENSHOT_READY",
                "Снимок экрана профиля готов",
                "ready",
                {
                    "profile": profile,
                    "screenshot": {
                        "mime": media_type,
                        "width": width,
                        "height": height,
                        "byte_size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    },
                },
            )
            return GameMcpResponse(
                structured=structured, image=data, mime_type=media_type
            )
        if tool == "game_get_fleet_state":
            if selection is None:
                raise InvalidRequestError("Для Fleet State отсутствует selection.")
            profile, indices = selection
            service = getattr(backend, "fleet_state", None)
            if service is None:
                raise ServiceUnavailableError("Fleet State capability недоступна.")
            result = service.state_read_only(profile, _make_selection(indices))
            details, code, state = _fleet_state_payload(result)
            details["profile"] = profile
            return _ok(code, "Fleet State профиля готов", state, details)
        if tool == "game_get_morale":
            if selection is None:
                raise InvalidRequestError("Для morale отсутствует selection.")
            profile, indices = selection
            service = getattr(backend, "morale", None)
            if service is None:
                raise ServiceUnavailableError("Morale capability недоступна.")
            result = service.state_read_only(profile, _make_selection(indices))
            details, code, state = _morale_payload(result)
            details["profile"] = profile
            return _ok(code, "Morale состояние профиля готово", state, details)
        raise InvalidRequestError("Для инструмента отсутствует обработчик.")

    def call(
        self,
        tool_name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        scopes: Collection[str] | None = None,
    ) -> dict[str, object] | GameMcpResponse:
        """Выполнить один self-contained запрос без выбранного профиля."""

        if tool_name not in GAME_MCP_TOOL_NAMES:
            return _unknown_tool(tool_name)
        if self._is_closed():
            return _error(
                "GAME_SERVICE_UNAVAILABLE",
                "Game MCP adapter закрыт.",
                tool=tool_name,
            )
        try:
            authorized = _authorized(tool_name, scopes)
        except (KeyError, TypeError, ValueError):
            authorized = False
        if not authorized:
            return _error(
                "GAME_MCP_UNAUTHORIZED",
                "Недостаточно полномочий для этого инструмента Game MCP.",
                tool=tool_name,
            )
        try:
            parsed, selection = _validate_arguments(tool_name, arguments)
        except (InvalidRequestError, TypeError, ValueError):
            return _invalid(tool_name)
        if tool_name == "game_get_contract":
            return contract_result()
        try:
            backend = self._acquire_backend()
        except Exception as exc:  # noqa: BLE001 - public boundary must hide adapter details.
            logger.error(
                "Источник Game MCP недоступен для %s: %s",
                tool_name,
                type(exc).__name__,
            )
            return _error(
                "GAME_SERVICE_UNAVAILABLE",
                "Источник Game данных сейчас недоступен.",
                tool=tool_name,
            )
        try:
            try:
                if tool_name in GAME_MCP_CONTROL_TOOL_NAMES:
                    profile = self._profile_from(parsed)
                    self._known_profile(backend, profile)
                    with self._acquire_mutation_lock(profile, backend):
                        # Повторная проверка после lock закрывает TOCTOU-окно.
                        result = self._dispatch(
                            tool_name,
                            parsed,
                            backend,
                            selection,
                            profile=profile,
                        )
                else:
                    result = self._dispatch(
                        tool_name, parsed, backend, selection
                    )
            except InstanceNotRunningError:
                return _error(
                    "GAME_PROFILE_NOT_RUNNING", "Профиль не запущен.", tool=tool_name
                )
            except InvalidRequestError:
                return _invalid(tool_name)
            except ConfigurationValidationError:
                return _error(
                    "GAME_CONFIG_INVALID",
                    "Значение конфигурации не прошло проверку.",
                    tool=tool_name,
                )
            except _UnknownTaskError:
                return _error("GAME_UNKNOWN_TASK", "Задача не найдена.", tool=tool_name)
            except _UnknownConfigError:
                return _error(
                    "GAME_UNKNOWN_CONFIG",
                    "Параметр конфигурации не найден.",
                    tool=tool_name,
                )
            except ResourceNotFoundError:
                return _error(
                    "GAME_UNKNOWN_PROFILE", "Профиль не найден.", tool=tool_name
                )
            except PostconditionFailedError:
                return _error(
                    "GAME_POSTCONDITION_FAILED",
                    "Изменение не подтверждено ожидаемым состоянием.",
                    tool=tool_name,
                )
            except ResourceBusyError:
                return _error(
                    "GAME_RESOURCE_BUSY",
                    "Профиль занят другой control-операцией.",
                    tool=tool_name,
                )
            except OwnershipAmbiguousError:
                return _error(
                    "GAME_OWNERSHIP_AMBIGUOUS",
                    "Ownership целевого Game ресурса не подтвержден.",
                    tool=tool_name,
                )
            except PreconditionFailedError:
                return _error(
                    "GAME_PRECONDITION_FAILED",
                    "Безопасное условие Game операции не выполнено.",
                    tool=tool_name,
                )
            except OperationFailedError:
                return _error(
                    "GAME_OPERATION_FAILED",
                    "Операция Game MCP не подтверждена.",
                    tool=tool_name,
                )
            except (
                StorageConfigurationError,
                StorageAuthenticationError,
                StorageUnavailableError,
                IncompatibleSchemaError,
            ):
                return _error(
                    "GAME_CAPABILITY_UNAVAILABLE",
                    "Запрошенная Game capability сейчас недоступна.",
                    tool=tool_name,
                )
            except StorageError:
                return _error(
                    "GAME_SERVICE_UNAVAILABLE",
                    "Источник Game данных сейчас недоступен.",
                    tool=tool_name,
                )
            except ServiceUnavailableError:
                return _error(
                    "GAME_CAPABILITY_UNAVAILABLE"
                    if tool_name in GAME_MCP_CONTROL_TOOL_NAMES
                    else "GAME_SERVICE_UNAVAILABLE",
                    "Запрошенная Game capability сейчас недоступна."
                    if tool_name in GAME_MCP_CONTROL_TOOL_NAMES
                    else "Источник Game данных сейчас недоступен.",
                    tool=tool_name,
                )
            except ApplicationError:
                return _error(
                    "GAME_SERVICE_UNAVAILABLE",
                    "Источник Game данных сейчас недоступен.",
                    tool=tool_name,
                )
            except Exception as exc:  # noqa: BLE001 - public boundary must hide adapter details.
                logger.error(
                    "Инструмент Game MCP %s завершился ошибкой %s",
                    tool_name,
                    type(exc).__name__,
                )
                return _error(
                    "GAME_SERVICE_UNAVAILABLE",
                    "Источник Game данных сейчас недоступен.",
                    tool=tool_name,
                )
            return result
        finally:
            self._release_backend()


def _make_selection(indices: tuple[int, ...]) -> FleetSelection:
    return FleetSelection(indices)


__all__ = (
    "GAME_MCP_CONTROL_TOOL_NAMES",
    "GAME_MCP_READ_TOOL_NAMES",
    "GAME_MCP_TOOL_NAMES",
    "GAME_MCP_TOOL_REQUIRED_SCOPES",
    "GameMcpAdapter",
    "GameMcpResponse",
)
