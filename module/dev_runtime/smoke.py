"""Универсальный декларативный Smoke Harness для Dev Runtime.

Smoke Harness остаётся отдельным слоем над существующим Dev Runtime. Он не
знает о конкретных обработчиках игрового процесса и не исполняет команды из ``SmokeSpec``.
Спецификация описывает только наблюдаемые условия, а supervisor использует уже существующие
DevSession, Task Sandbox и Evidence API.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

import psutil
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from module.dev_runtime.bounded_io import BoundedReadTooLarge, read_bounded_bytes
from module.dev_runtime.contracts import (
    DevEnvironment,
    DevResult,
    DevSessionState,
    DevStatusKind,
)
from module.dev_runtime.coordination import (
    RuntimeCoordinationError,
    runtime_coordination_lock,
)
from module.dev_runtime.evidence import (
    EVIDENCE_EVENT_TYPES,
    EVIDENCE_HEALTH_COMPLETE,
    EVIDENCE_HEALTH_CORRUPT,
    EVIDENCE_HEALTH_DEGRADED,
    EVIDENCE_HEALTH_UNAVAILABLE,
    EvidenceScreenshot,
    GitSnapshot,
    capture_git_snapshot,
)
from module.dev_runtime.game_bridge import (
    GAME_OBSERVATION_MAX_PARAMETER_VALUE,
    GameObservationError,
    GameObservationSnapshot,
    GameObservationStore,
)
from module.dev_runtime.target import DevTarget, target_identity
from module.dev_runtime.task_sandbox import (
    SCHEDULER_RESET_TIME,
    TaskCatalog,
    TaskPolicyStore,
    TaskSandboxError,
    _atomic_json_write,
    _ensure_scoped_path,
    _exclusive_policy_lock,
    _is_reparse_point,
    _read_session,
    read_profile_payload,
    scheduler_state,
    write_profile_payload,
)

SMOKE_SCHEMA_VERSION = 1
SMOKE_STATE_SCHEMA_VERSION = 1
SMOKE_MAX_NAME = 128
SMOKE_MAX_OBJECTIVE = 1024
SMOKE_MAX_RUBRIC = 4096
SMOKE_MAX_ASSERTIONS = 64
SMOKE_MAX_OVERRIDES = 32
SMOKE_MAX_TASKS = 64
SMOKE_MAX_PROGRESS_TEXT = 256
SMOKE_MAX_RESULT_TEXT = 1024
SMOKE_MAX_RUN_BYTES = 512 * 1024
SMOKE_MAX_SPEC_BYTES = 256 * 1024
SMOKE_MAX_RUNS = 32
SMOKE_MAX_RUN_AGE_SECONDS = 30 * 24 * 60 * 60
SMOKE_POLL_SECONDS = 0.25
SMOKE_MIN_TIMEOUT_SECONDS = 1.0
SMOKE_MAX_TIMEOUT_SECONDS = 24 * 60 * 60
SMOKE_MAX_OBSERVATION_SECONDS = 24 * 60 * 60
SMOKE_MAX_CONFIG_PATH = 256
SMOKE_MAX_LITERAL = 512
SMOKE_MAX_EVIDENCE_REFS = 16
SMOKE_MAX_LOG_BYTES = 128 * 1024
SMOKE_MAX_TIMELINE_EVENTS = 2048
SMOKE_LOCK_TIMEOUT = 10.0
SMOKE_LOCK_RETRY_SECONDS = 0.05
SMOKE_MAX_GAME_OBSERVATIONS = 8
SMOKE_MAX_GAME_CHECKPOINTS = 8
SMOKE_MAX_GAME_PARAMETER_DEPTH = 4
SMOKE_MAX_GAME_PARAMETER_ITEMS = 16

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_CONFIG_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}(?:\.[A-Za-z][A-Za-z0-9_-]{0,63}){2,4}$")
_SAFE_EVENT = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SAFE_SHA = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_DEFENSE_BLOCK_TOKENS = frozenset(
    {
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "key",
        "llm",
        "private_key",
        "privatekey",
        "public_key",
        "publickey",
        "remote",
        "scheduler",
        "state",
        "storage",
        "secret",
        "secrets",
        "password",
        "token",
        "cookie",
        "passfile",
        "executable",
        "path",
        "command",
        "serial",
        "ssh",
        "runtime",
    }
)


type ScalarValue = StrictBool | StrictInt | StrictFloat | StrictStr | None
type DurationValue = StrictInt | StrictFloat


class SmokeState(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    EVALUATING = "evaluating"
    CLEANING_UP = "cleaning_up"
    AWAITING_EXTERNAL_EVALUATION = "awaiting_external_evaluation"
    FINISHED = "finished"


class SmokeOutcome(StrEnum):
    PASS = "PASS"
    PRODUCT_FAILED = "PRODUCT_FAILED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    HARNESS_FAILED = "HARNESS_FAILED"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    TIMEOUT = "TIMEOUT"
    INVALIDATED = "INVALIDATED"
    CANCELLED = "CANCELLED"


class SmokeAssertionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PENDING = "PENDING"
    UNAVAILABLE = "UNAVAILABLE"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_default=True,
        validate_assignment=True,
    )


def _text(value: str, *, field_name: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} должен быть строкой")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} не должен быть пустым")
    if len(value) > maximum or value != value.strip():
        raise ValueError(f"{field_name} имеет недопустимую длину или пробелы по краям")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field_name} содержит управляющий символ")
    return value


def _identifier(value: str, *, field_name: str) -> str:
    value = _text(value, field_name=field_name, maximum=128)
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{field_name} имеет небезопасный формат")
    return value


def _duration(value: DurationValue, *, field_name: str, minimum: float = 0.0) -> DurationValue:
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < minimum or numeric > SMOKE_MAX_OBSERVATION_SECONDS:
        raise ValueError(f"{field_name} выходит за допустимые границы")
    return value


def _safe_task(value: str, *, field_name: str) -> str:
    value = _text(value, field_name=field_name, maximum=128)
    if (
        not value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "/" in value
        or "\\" in value
        or ".." in value
    ):
        raise ValueError(f"{field_name} содержит небезопасный выбор задачи")
    return value


def _safe_event(value: str, *, field_name: str = "event_type") -> str:
    value = _text(value, field_name=field_name, maximum=64)
    if value not in EVIDENCE_EVENT_TYPES or not _SAFE_EVENT.fullmatch(value):
        raise ValueError(f"{field_name} отсутствует в каноническом реестре событий Evidence API")
    return value


def _safe_scalar(value: ScalarValue, *, field_name: str) -> ScalarValue:
    if isinstance(value, str):
        return _text(value, field_name=field_name, maximum=SMOKE_MAX_LITERAL, allow_empty=True)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(float(value)):
        raise ValueError(f"{field_name} должен быть конечным числом")
    return value


class SmokeSessionSpec(_StrictModel):
    root_tasks: list[str] = Field(min_length=1, max_length=SMOKE_MAX_TASKS)
    excluded_tasks: list[str] = Field(default_factory=list, max_length=SMOKE_MAX_TASKS)

    @field_validator("root_tasks", "excluded_tasks")
    @classmethod
    def validate_tasks(cls, values: list[str], info: object) -> list[str]:
        field_name = getattr(info, "field_name", "tasks")
        normalized = [_safe_task(value, field_name=field_name) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{field_name} содержит дубликаты")
        return sorted(normalized)

    @model_validator(mode="after")
    def validate_overlap(self) -> SmokeSessionSpec:
        overlap = sorted(set(self.root_tasks) & set(self.excluded_tasks))
        if overlap:
            raise ValueError("root_tasks и excluded_tasks не должны пересекаться")
        return self


class SmokeConfigOverride(_StrictModel):
    path: str = Field(min_length=5, max_length=SMOKE_MAX_CONFIG_PATH)
    value: ScalarValue

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        value = _text(value, field_name="config_overrides.path", maximum=SMOKE_MAX_CONFIG_PATH)
        if not _SAFE_CONFIG_PATH.fullmatch(value):
            raise ValueError("config_overrides.path должен быть каноническим точечным путём конфигурации")
        return value

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: ScalarValue) -> ScalarValue:
        return _safe_scalar(value, field_name="config_overrides.value")


class _AssertionBase(_StrictModel):
    assertion_id: str = Field(min_length=1, max_length=128)
    required: StrictBool = True

    @field_validator("assertion_id")
    @classmethod
    def validate_assertion_id(cls, value: str) -> str:
        return _identifier(value, field_name="assertion_id")


class EventOccurredAssertion(_AssertionBase):
    capability_id: Literal["event_occurred"]
    event_type: str

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        return _safe_event(value)


class EventNotOccurredAssertion(_AssertionBase):
    capability_id: Literal["event_not_occurred"]
    event_type: str
    observation_window_seconds: DurationValue = 1.0

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        return _safe_event(value)

    @field_validator("observation_window_seconds")
    @classmethod
    def validate_window(cls, value: DurationValue) -> DurationValue:
        return _duration(value, field_name="observation_window_seconds", minimum=0.1)


class TaskStartedAssertion(_AssertionBase):
    capability_id: Literal["task_started"]
    task: str

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        return _safe_task(value, field_name="task")


class TaskNotStartedAssertion(_AssertionBase):
    capability_id: Literal["task_not_started"]
    task: str
    observation_window_seconds: DurationValue = 1.0

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        return _safe_task(value, field_name="task")

    @field_validator("observation_window_seconds")
    @classmethod
    def validate_window(cls, value: DurationValue) -> DurationValue:
        return _duration(value, field_name="observation_window_seconds", minimum=0.1)


class DependencyOccurredAssertion(_AssertionBase):
    capability_id: Literal["dependency_occurred"]
    task: str
    required_by: str

    @field_validator("task", "required_by")
    @classmethod
    def validate_task(cls, value: str, info: object) -> str:
        return _safe_task(value, field_name=getattr(info, "field_name", "task"))


class NoRuntimeErrorAssertion(_AssertionBase):
    capability_id: Literal["no_runtime_error"]
    observation_window_seconds: DurationValue = 1.0

    @field_validator("observation_window_seconds")
    @classmethod
    def validate_window(cls, value: DurationValue) -> DurationValue:
        return _duration(value, field_name="observation_window_seconds", minimum=0.1)


class ExpectedSafeErrorAssertion(_AssertionBase):
    capability_id: Literal["expected_safe_error"]
    error_type: str | None = None
    error_code: str | None = None

    @field_validator("error_type", "error_code")
    @classmethod
    def validate_error_selector(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _text(value, field_name=getattr(info, "field_name", "error"), maximum=128)

    @model_validator(mode="after")
    def require_selector(self) -> ExpectedSafeErrorAssertion:
        if self.error_type is None and self.error_code is None:
            raise ValueError("expected_safe_error требует error_type или error_code")
        return self


class EvidenceHealthAssertion(_AssertionBase):
    capability_id: Literal["evidence_health"]
    expected_status: Literal["complete", "degraded", "corrupt", "unavailable"] = "complete"


class RuntimeStateAssertion(_AssertionBase):
    capability_id: Literal["runtime_state"]
    expected_state: Literal["running", "stopped", "starting", "failed"]


class DevPortStateAssertion(_AssertionBase):
    capability_id: Literal["dev_port_state"]
    expected_state: Literal["listening", "free"]


class ConfigValueAssertion(_AssertionBase):
    capability_id: Literal["config_value"]
    path: str = Field(min_length=5, max_length=SMOKE_MAX_CONFIG_PATH)
    expected_value: ScalarValue

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return SmokeConfigOverride(path=value, value=None).path

    @field_validator("expected_value")
    @classmethod
    def validate_expected_value(cls, value: ScalarValue) -> ScalarValue:
        return _safe_scalar(value, field_name="expected_value")


class ConfigRestoredAssertion(_AssertionBase):
    capability_id: Literal["config_restored"]
    path: str = Field(min_length=5, max_length=SMOKE_MAX_CONFIG_PATH)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return SmokeConfigOverride(path=value, value=None).path


class DurationWithinBoundAssertion(_AssertionBase):
    capability_id: Literal["duration_within_bound"]
    maximum_seconds: DurationValue
    minimum_seconds: DurationValue = 0.0

    @field_validator("maximum_seconds", "minimum_seconds")
    @classmethod
    def validate_bound(cls, value: DurationValue, info: object) -> DurationValue:
        return _duration(value, field_name=getattr(info, "field_name", "duration"), minimum=0.0)

    @model_validator(mode="after")
    def validate_order(self) -> DurationWithinBoundAssertion:
        if float(self.minimum_seconds) > float(self.maximum_seconds):
            raise ValueError("minimum_seconds не может быть больше maximum_seconds")
        return self


class SessionLogContainsAssertion(_AssertionBase):
    capability_id: Literal["session_log_contains_literal"]
    literal: str = Field(min_length=1, max_length=SMOKE_MAX_LITERAL)

    @field_validator("literal")
    @classmethod
    def validate_literal(cls, value: str) -> str:
        return _text(value, field_name="literal", maximum=SMOKE_MAX_LITERAL)


class SessionLogNotContainsAssertion(_AssertionBase):
    capability_id: Literal["session_log_does_not_contain_literal"]
    literal: str = Field(min_length=1, max_length=SMOKE_MAX_LITERAL)
    observation_window_seconds: DurationValue = 1.0

    @field_validator("literal")
    @classmethod
    def validate_literal(cls, value: str) -> str:
        return _text(value, field_name="literal", maximum=SMOKE_MAX_LITERAL)

    @field_validator("observation_window_seconds")
    @classmethod
    def validate_window(cls, value: DurationValue) -> DurationValue:
        return _duration(value, field_name="observation_window_seconds", minimum=0.1)


type SmokeAssertion = Annotated[
    EventOccurredAssertion
    | EventNotOccurredAssertion
    | TaskStartedAssertion
    | TaskNotStartedAssertion
    | DependencyOccurredAssertion
    | NoRuntimeErrorAssertion
    | ExpectedSafeErrorAssertion
    | EvidenceHealthAssertion
    | RuntimeStateAssertion
    | DevPortStateAssertion
    | ConfigValueAssertion
    | ConfigRestoredAssertion
    | DurationWithinBoundAssertion
    | SessionLogContainsAssertion
    | SessionLogNotContainsAssertion,
    Field(discriminator="capability_id"),
]


class SmokeSetupSpec(_StrictModel):
    config_overrides: list[SmokeConfigOverride] = Field(default_factory=list, max_length=SMOKE_MAX_OVERRIDES)

    @model_validator(mode="after")
    def validate_override_ids(self) -> SmokeSetupSpec:
        paths = [item.path for item in self.config_overrides]
        if len(paths) != len(set(paths)):
            raise ValueError("config_overrides.path должен быть уникальным")
        object.__setattr__(self, "config_overrides", sorted(self.config_overrides, key=lambda item: item.path))
        return self


class VisualCaptureCondition(_StrictModel):
    kind: Literal["event", "task_started", "task_finished"]
    event_type: str | None = None
    task: str | None = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str | None) -> str | None:
        return None if value is None else _safe_event(value)

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str | None) -> str | None:
        return None if value is None else _safe_task(value, field_name="capture_condition.task")

    @model_validator(mode="after")
    def validate_condition(self) -> VisualCaptureCondition:
        if self.kind == "event":
            if self.event_type is None or self.task is not None:
                raise ValueError("условие захвата события требует только event_type")
        elif self.task is None or self.event_type is not None:
            raise ValueError("условие захвата задачи требует только task")
        return self


class SmokeVisualAssertion(_AssertionBase):
    capability_id: Literal["external_visual"]
    rubric: str = Field(min_length=1, max_length=SMOKE_MAX_RUBRIC)
    capture_condition: VisualCaptureCondition

    @field_validator("rubric")
    @classmethod
    def validate_rubric(cls, value: str) -> str:
        return _text(value, field_name="rubric", maximum=SMOKE_MAX_RUBRIC)


def _game_parameter_value(value: object, *, field_name: str, depth: int = 0) -> object:
    if depth > SMOKE_MAX_GAME_PARAMETER_DEPTH:
        raise ValueError(f"{field_name} имеет слишком глубокую структуру")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > GAME_OBSERVATION_MAX_PARAMETER_VALUE:
            raise ValueError(f"{field_name} выходит за bounded range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} должен быть конечным числом")
        if abs(value) > GAME_OBSERVATION_MAX_PARAMETER_VALUE:
            raise ValueError(f"{field_name} выходит за bounded range")
        return value
    if isinstance(value, str):
        return _text(value, field_name=field_name, maximum=SMOKE_MAX_LITERAL, allow_empty=True)
    if isinstance(value, Mapping):
        if len(value) > SMOKE_MAX_GAME_PARAMETER_ITEMS:
            raise ValueError(f"{field_name} содержит слишком много полей")
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} содержит нестроковый ключ")
            safe_key = _text(key, field_name=f"{field_name}.key", maximum=128)
            normalized[safe_key] = _game_parameter_value(
                item,
                field_name=f"{field_name}.{safe_key}",
                depth=depth + 1,
            )
        return normalized
    if isinstance(value, (list, tuple)):
        if len(value) > SMOKE_MAX_GAME_PARAMETER_ITEMS:
            raise ValueError(f"{field_name} превышает ограничение размера")
        return [
            _game_parameter_value(item, field_name=f"{field_name}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{field_name} содержит неподдерживаемый тип")


class SmokeGameObservationRequest(_StrictModel):
    capability_id: str = Field(min_length=1, max_length=128)
    parameters: dict[str, object] = Field(
        default_factory=dict,
        max_length=SMOKE_MAX_GAME_PARAMETER_ITEMS,
    )

    @field_validator("capability_id")
    @classmethod
    def validate_capability_id(cls, value: str) -> str:
        return _identifier(value, field_name="game_observations.capability_id")

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, object]) -> dict[str, object]:
        normalized = _game_parameter_value(
            value,
            field_name="game_observations.parameters",
        )
        if not isinstance(normalized, dict):
            raise TypeError("game_observations.parameters должен быть объектом")
        return normalized


class SmokeGameCheckpoint(_StrictModel):
    checkpoint_id: str = Field(min_length=1, max_length=128)
    observations: list[SmokeGameObservationRequest] = Field(
        min_length=1,
        max_length=SMOKE_MAX_GAME_OBSERVATIONS,
    )

    @field_validator("checkpoint_id")
    @classmethod
    def validate_checkpoint_id(cls, value: str) -> str:
        value = _identifier(value, field_name="game_observations.checkpoint_id")
        if value in {"before", "final"}:
            raise ValueError("game checkpoint_id before/final зарезервирован")
        return value

    @model_validator(mode="after")
    def validate_observation_ids(self) -> SmokeGameCheckpoint:
        capability_ids = [item.capability_id for item in self.observations]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("game checkpoint не должен содержать duplicate capability_id")
        return self


class SmokeGameObservationSpec(_StrictModel):
    observations: list[SmokeGameObservationRequest] = Field(
        min_length=1,
        max_length=SMOKE_MAX_GAME_OBSERVATIONS,
    )
    checkpoints: list[SmokeGameCheckpoint] = Field(
        default_factory=list,
        max_length=SMOKE_MAX_GAME_CHECKPOINTS,
    )
    duplicate_policy: Literal["reject", "keep_first"] = "reject"

    @model_validator(mode="after")
    def validate_checkpoints(self) -> SmokeGameObservationSpec:
        capability_ids = [item.capability_id for item in self.observations]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("game observations не должны содержать duplicate capability_id")
        checkpoint_ids = [item.checkpoint_id for item in self.checkpoints]
        if len(checkpoint_ids) != len(set(checkpoint_ids)):
            raise ValueError("game checkpoint_id должен быть уникальным")
        object.__setattr__(
            self,
            "checkpoints",
            sorted(self.checkpoints, key=lambda item: item.checkpoint_id),
        )
        return self


class SmokeSpec(_StrictModel):
    schema_version: Literal[SMOKE_SCHEMA_VERSION] = SMOKE_SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=SMOKE_MAX_NAME)
    objective: str = Field(min_length=1, max_length=SMOKE_MAX_OBJECTIVE)
    timeout_seconds: DurationValue = 180.0
    session: SmokeSessionSpec
    setup: SmokeSetupSpec = Field(default_factory=SmokeSetupSpec)
    assertions: list[SmokeAssertion] = Field(default_factory=list, max_length=SMOKE_MAX_ASSERTIONS)
    visual_assertions: list[SmokeVisualAssertion] = Field(default_factory=list, max_length=SMOKE_MAX_ASSERTIONS)
    game_observations: SmokeGameObservationSpec | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _identifier(value, field_name="name")

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        return _text(value, field_name="objective", maximum=SMOKE_MAX_OBJECTIVE)

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: DurationValue) -> DurationValue:
        numeric = float(value)
        if not math.isfinite(numeric) or not SMOKE_MIN_TIMEOUT_SECONDS <= numeric <= SMOKE_MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds выходит за допустимые границы")
        return value

    @model_validator(mode="after")
    def validate_ids(self) -> SmokeSpec:
        object.__setattr__(self, "assertions", sorted(self.assertions, key=lambda item: item.assertion_id))
        object.__setattr__(self, "visual_assertions", sorted(self.visual_assertions, key=lambda item: item.assertion_id))
        identifiers = [item.assertion_id for item in self.assertions]
        identifiers.extend(item.assertion_id for item in self.visual_assertions)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("assertion_id должен быть уникальным во всей области приёмки")
        return self

    def canonical_dict(self) -> dict[str, object]:
        payload = self.model_dump(mode="json", exclude_none=True)
        assert isinstance(payload, dict)
        return payload

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def spec_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class SmokeEvidenceRef(_StrictModel):
    source: Literal[
        "canonical_timeline",
        "runtime_state",
        "task_policy",
        "structured_error",
        "config",
        "session_log",
        "external_visual",
        "game_observation",
    ]
    reference: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=SMOKE_MAX_RESULT_TEXT)

    @field_validator("reference", "description")
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        return _text(value, field_name=getattr(info, "field_name", "evidence"), maximum=SMOKE_MAX_RESULT_TEXT)


class SmokeAssertionResult(_StrictModel):
    assertion_id: str
    capability_id: str
    required: StrictBool
    status: SmokeAssertionStatus
    evidence_source: str
    evidence_refs: list[SmokeEvidenceRef] = Field(default_factory=list, max_length=SMOKE_MAX_EVIDENCE_REFS)
    message: str = Field(max_length=SMOKE_MAX_RESULT_TEXT)

    @field_validator("assertion_id", "capability_id")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, field_name=getattr(info, "field_name", "id"))

    @field_validator("evidence_source", "message")
    @classmethod
    def validate_result_text(cls, value: str, info: object) -> str:
        return _text(value, field_name=getattr(info, "field_name", "result"), maximum=SMOKE_MAX_RESULT_TEXT, allow_empty=True)


class SmokeSourceSnapshot(_StrictModel):
    head: str | None
    branch: str | None
    detached: StrictBool | None
    dirty: StrictBool | None
    changed_paths: list[str] = Field(default_factory=list, max_length=256)
    available: StrictBool
    fingerprint: str

    @field_validator("head")
    @classmethod
    def validate_head(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _text(value, field_name="source.head", maximum=128)
        if not re.fullmatch(r"[0-9a-fA-F]{7,128}", value):
            raise ValueError("source.head имеет неверный формат")
        return value

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str | None) -> str | None:
        return None if value is None else _text(value, field_name="source.branch", maximum=256)

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            value = _text(value, field_name="source.changed_paths", maximum=260)
            if (
                value.startswith(("/", "\\", "../", "..\\"))
                or re.match(r"^[A-Za-z]:[/\\]", value)
                or value == ".."
                or "/../" in value
                or "\\..\\" in value
            ):
                raise ValueError("source.changed_paths содержит внешний путь")
            normalized.append(value.replace("\\", "/"))
        return normalized

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not _SAFE_SHA.fullmatch(value):
            raise ValueError("source.fingerprint имеет неверный SHA-256")
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> SmokeSourceSnapshot:
        if self.available:
            if self.head is None or self.branch is None or self.detached is None or self.dirty is None:
                raise ValueError("доступный снимок source содержит неполные поля")
            if self.dirty is not bool(self.changed_paths):
                raise ValueError("доступный снимок source имеет несогласованное состояние дерева")
        elif any(item is not None for item in (self.head, self.branch, self.detached, self.dirty)) or self.changed_paths:
            raise ValueError("недоступный снимок source содержит лишние поля")
        raw = {
            "head": self.head,
            "branch": self.branch,
            "detached": self.detached,
            "dirty": self.dirty,
            "changed_paths": list(self.changed_paths),
            "available": self.available,
        }
        expected_fingerprint = hashlib.sha256(
            json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.fingerprint != expected_fingerprint:
            raise ValueError("source.fingerprint не соответствует содержимому снимка")
        return self


class SmokeFailure(_StrictModel):
    code: str
    message: str
    assertion_id: str | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return _identifier(value, field_name="failure.code")

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _text(value, field_name="failure.message", maximum=SMOKE_MAX_RESULT_TEXT)

    @field_validator("assertion_id")
    @classmethod
    def validate_assertion_id(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, field_name="failure.assertion_id")


class SmokeCleanup(_StrictModel):
    attempted: StrictBool = False
    session_stopped: StrictBool = False
    task_cleanup_confirmed: StrictBool = False
    scheduler_clean: StrictBool = False
    overrides_restored: StrictBool = False
    source_unchanged: StrictBool = False
    no_owned_orphan: StrictBool = False
    port_free: StrictBool = False
    confirmed: StrictBool = False
    failure_code: str | None = None

    @field_validator("failure_code")
    @classmethod
    def validate_failure_code(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, field_name="cleanup.failure_code")

    @model_validator(mode="after")
    def validate_confirmation(self) -> SmokeCleanup:
        if self.confirmed and (
            not self.session_stopped
            or not self.task_cleanup_confirmed
            or not self.scheduler_clean
            or not self.overrides_restored
            or not self.source_unchanged
            or not self.no_owned_orphan
            or not self.port_free
            or self.failure_code is not None
        ):
            raise ValueError("подтверждённая очистка содержит неподтверждённые проверки")
        return self


class SmokeOverrideSnapshot(_StrictModel):
    path: str
    original_value: ScalarValue
    applied_value: ScalarValue

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return SmokeConfigOverride(path=value, value=None).path

    @field_validator("original_value", "applied_value")
    @classmethod
    def validate_values(cls, value: ScalarValue, info: object) -> ScalarValue:
        return _safe_scalar(value, field_name=getattr(info, "field_name", "override"))


class SmokeOverrideState(_StrictModel):
    snapshots: list[SmokeOverrideSnapshot] = Field(default_factory=list, max_length=SMOKE_MAX_OVERRIDES)
    persisted: StrictBool = False
    applied: StrictBool = False
    restored: StrictBool = False
    verified: StrictBool = False
    baseline_digest: str | None = None

    @field_validator("baseline_digest")
    @classmethod
    def validate_baseline_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SAFE_SHA.fullmatch(value):
            raise ValueError("baseline_digest имеет неверный SHA-256")
        return value


class SmokeProgress(_StrictModel):
    passed: StrictInt = 0
    failed: StrictInt = 0
    pending: StrictInt = 0
    unavailable: StrictInt = 0
    elapsed_seconds: StrictFloat = 0.0
    current_task: str | None = None
    evidence_health: str = EVIDENCE_HEALTH_UNAVAILABLE

    @field_validator("passed", "failed", "pending", "unavailable")
    @classmethod
    def validate_count(cls, value: int) -> int:
        if value < 0 or value > SMOKE_MAX_ASSERTIONS * 2:
            raise ValueError("progress count выходит за допустимые границы")
        return value

    @field_validator("elapsed_seconds")
    @classmethod
    def validate_elapsed(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0 or value > SMOKE_MAX_TIMEOUT_SECONDS * 2:
            raise ValueError("progress elapsed_seconds имеет неверное значение")
        return value

    @field_validator("current_task", "evidence_health")
    @classmethod
    def validate_progress_text(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _text(value, field_name=getattr(info, "field_name", "progress"), maximum=SMOKE_MAX_PROGRESS_TEXT)


class SmokePendingEvaluation(_StrictModel):
    assertion_id: str
    screenshot_id: str
    screenshot_sha256: str
    rubric: str
    rubric_hash: str
    spec_hash: str
    session_id: str
    submitted: StrictBool = False

    @field_validator("assertion_id", "screenshot_id", "session_id")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, field_name=getattr(info, "field_name", "evaluation"))

    @field_validator("screenshot_sha256", "rubric_hash", "spec_hash")
    @classmethod
    def validate_hash(cls, value: str, info: object) -> str:
        if not _SAFE_SHA.fullmatch(value):
            raise ValueError(f"{getattr(info, 'field_name', 'hash')} имеет неверный SHA-256")
        return value

    @field_validator("rubric")
    @classmethod
    def validate_rubric(cls, value: str) -> str:
        return _text(value, field_name="evaluation.rubric", maximum=SMOKE_MAX_RUBRIC)


class SmokeExternalVerdict(_StrictModel):
    source: Literal["mcp_client"] = "mcp_client"
    assertion_id: str
    screenshot_id: str
    screenshot_sha256: str
    spec_hash: str
    rubric_hash: str
    verdict: Literal["pass", "fail"]
    rationale: str
    submitted_at: str

    @field_validator("assertion_id", "screenshot_id")
    @classmethod
    def validate_identity(cls, value: str, info: object) -> str:
        return _identifier(value, field_name=getattr(info, "field_name", "verdict"))

    @field_validator("screenshot_sha256", "spec_hash", "rubric_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SAFE_SHA.fullmatch(value):
            raise ValueError("verdict hash имеет неверный SHA-256")
        return value

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return _text(value, field_name="rationale", maximum=SMOKE_MAX_RESULT_TEXT)

    @field_validator("submitted_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        _timestamp(value, field_name="submitted_at")
        return value


class SmokeSupervisorIdentity(_StrictModel):
    pid: StrictInt
    created_at: StrictFloat
    executable: str
    command_line: list[str] = Field(min_length=4, max_length=8)
    cwd: str

    @field_validator("pid")
    @classmethod
    def validate_pid(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("supervisor pid должен быть положительным")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("supervisor created_at имеет неверное значение")
        return value

    @field_validator("executable", "cwd")
    @classmethod
    def validate_process_text(cls, value: str, info: object) -> str:
        return _text(value, field_name=getattr(info, "field_name", "process"), maximum=1024)

    @field_validator("command_line")
    @classmethod
    def validate_command_line(cls, values: list[str]) -> list[str]:
        if any("\x00" in item or not item for item in values):
            raise ValueError("командная строка supervisor содержит недопустимое значение")
        return values


class SmokeRunRecord(_StrictModel):
    schema_version: Literal[SMOKE_STATE_SCHEMA_VERSION] = SMOKE_STATE_SCHEMA_VERSION
    smoke_id: str
    state: SmokeState
    outcome: SmokeOutcome | None = None
    spec_hash: str
    source: SmokeSourceSnapshot
    created_at: str
    started_at: str | None = None
    deadline_at: str
    finished_at: str | None = None
    session_id: str | None = None
    target_profile: str | None = None
    target_identity: str | None = None
    supervisor: SmokeSupervisorIdentity | None = None
    progress: SmokeProgress = Field(default_factory=SmokeProgress)
    cleanup: SmokeCleanup = Field(default_factory=SmokeCleanup)
    overrides: SmokeOverrideState = Field(default_factory=SmokeOverrideState)
    assertions: list[SmokeAssertionResult] = Field(default_factory=list, max_length=SMOKE_MAX_ASSERTIONS * 2)
    primary_failure: SmokeFailure | None = None
    harness_failure: SmokeFailure | None = None
    pending_evaluation: SmokePendingEvaluation | None = None
    external_verdict: SmokeExternalVerdict | None = None

    @field_validator("smoke_id")
    @classmethod
    def validate_smoke_id(cls, value: str) -> str:
        return _identifier(value, field_name="smoke_id")

    @field_validator("spec_hash")
    @classmethod
    def validate_spec_hash(cls, value: str) -> str:
        if not _SAFE_SHA.fullmatch(value):
            raise ValueError("spec_hash имеет неверный SHA-256")
        return value

    @field_validator("created_at", "started_at", "deadline_at", "finished_at")
    @classmethod
    def validate_timestamps(cls, value: str | None, info: object) -> str | None:
        if value is not None:
            _timestamp(value, field_name=getattr(info, "field_name", "timestamp"))
        return value

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, field_name="session_id")

    @field_validator("target_profile")
    @classmethod
    def validate_target_profile(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, field_name="target_profile")

    @field_validator("target_identity")
    @classmethod
    def validate_target_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SAFE_SHA.fullmatch(value):
            raise ValueError("target_identity имеет неверный SHA-256")
        return value

    @model_validator(mode="after")
    def validate_result_state(self) -> SmokeRunRecord:
        if self.state is SmokeState.FINISHED and self.outcome is None:
            raise ValueError("завершённый SmokeRun должен иметь outcome")
        if self.state is SmokeState.AWAITING_EXTERNAL_EVALUATION and (self.pending_evaluation is None or self.outcome is not None):
            raise ValueError("ожидающий внешней оценки SmokeRun должен иметь pending_evaluation")
        if self.external_verdict is not None and self.state is not SmokeState.FINISHED:
            raise ValueError("external_verdict разрешён только для завершённого SmokeRun")
        if (self.target_profile is None) != (self.target_identity is None):
            raise ValueError("target_profile и target_identity должны быть заданы вместе")
        if self.target_profile is not None:
            try:
                expected = target_identity(DevTarget(self.target_profile))
            except ValueError as exc:
                raise ValueError("target_profile имеет недопустимое назначение") from exc
            if self.target_identity != expected:
                raise ValueError("target_identity не соответствует target_profile")
        return self


class SmokeResult(_StrictModel):
    schema_version: Literal[SMOKE_STATE_SCHEMA_VERSION] = SMOKE_STATE_SCHEMA_VERSION
    smoke_id: str
    spec_hash: str
    outcome: SmokeOutcome
    code: str
    message: str
    source: SmokeSourceSnapshot
    session_id: str | None = None
    target_profile: str | None = None
    target_identity: str | None = None
    assertions: list[SmokeAssertionResult] = Field(default_factory=list, max_length=SMOKE_MAX_ASSERTIONS * 2)
    cleanup: SmokeCleanup
    primary_failure: SmokeFailure | None = None
    harness_failure: SmokeFailure | None = None
    external_verdict: SmokeExternalVerdict | None = None
    finished_at: str

    @field_validator("smoke_id")
    @classmethod
    def validate_smoke_id(cls, value: str) -> str:
        return _identifier(value, field_name="result.smoke_id")

    @field_validator("spec_hash")
    @classmethod
    def validate_spec_hash(cls, value: str) -> str:
        if not _SAFE_SHA.fullmatch(value):
            raise ValueError("result.spec_hash имеет неверный SHA-256")
        return value

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return _identifier(value, field_name="result.code")

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _text(value, field_name="result.message", maximum=SMOKE_MAX_RESULT_TEXT)

    @field_validator("finished_at")
    @classmethod
    def validate_finished_at(cls, value: str) -> str:
        _timestamp(value, field_name="finished_at")
        return value

    @field_validator("target_profile")
    @classmethod
    def validate_target_profile(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, field_name="result.target_profile")

    @field_validator("target_identity")
    @classmethod
    def validate_target_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SAFE_SHA.fullmatch(value):
            raise ValueError("result.target_identity имеет неверный SHA-256")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> SmokeResult:
        if (self.target_profile is None) != (self.target_identity is None):
            raise ValueError("result target_profile и target_identity должны быть заданы вместе")
        if self.target_profile is not None:
            try:
                expected = target_identity(DevTarget(self.target_profile))
            except ValueError as exc:
                raise ValueError("result.target_profile имеет недопустимое назначение") from exc
            if self.target_identity != expected:
                raise ValueError("result.target_identity не соответствует target_profile")
        return self


class SmokeControl(_StrictModel):
    cancel_requested: StrictBool = False
    requested_at: str | None = None

    @field_validator("requested_at")
    @classmethod
    def validate_requested_at(cls, value: str | None) -> str | None:
        if value is not None:
            _timestamp(value, field_name="requested_at")
        return value


def _timestamp(value: str, *, field_name: str) -> str:
    value = _text(value, field_name=field_name, maximum=80)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} не является timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} должен содержать timezone")
    return value


def _timestamp_now(now: Callable[[], datetime]) -> str:
    value = now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _add_seconds(timestamp: str, seconds: float) -> str:
    value = datetime.fromisoformat(timestamp)
    return (value + timedelta(seconds=seconds)).astimezone(UTC).isoformat()


def _safe_model_json(model: BaseModel) -> dict[str, object]:
    payload = model.model_dump(mode="json")
    if not isinstance(payload, dict):
        raise TypeError("ожидался JSON-объект")
    return payload


def _validate_json_model(model_type: type[BaseModel], payload: object) -> BaseModel:
    """Строго проверить JSON-представление, сохранив смысл перечислений из файла."""

    return model_type.model_validate_json(json.dumps(payload, ensure_ascii=True), strict=True)


def _source_snapshot(git: GitSnapshot) -> SmokeSourceSnapshot:
    raw = {
        "head": git.head,
        "branch": git.branch,
        "detached": git.detached,
        "dirty": git.dirty,
        "changed_paths": list(git.changed_paths),
        "available": git.available,
    }
    fingerprint = hashlib.sha256(
        json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SmokeSourceSnapshot(fingerprint=fingerprint, **raw)


def _same_source(expected: SmokeSourceSnapshot, current: SmokeSourceSnapshot) -> bool:
    return (
        expected.available
        and current.available
        and expected.head == current.head
        and expected.branch == current.branch
        and expected.detached == current.detached
        and expected.dirty is False
        and current.dirty is False
        and expected.changed_paths == current.changed_paths == []
        and expected.fingerprint == current.fingerprint
    )


def _result_details(result: object) -> Mapping[str, object]:
    details = result.details if isinstance(result, DevResult) else getattr(result, "details", {})
    return details if isinstance(details, Mapping) else {}


def _result_ok(result: object) -> bool:
    value = result.ok if isinstance(result, DevResult) else getattr(result, "ok", False)
    return value is True


def _result_session_id(result: object) -> str | None:
    value = result.session_id if isinstance(result, DevResult) else getattr(result, "session_id", None)
    return value if isinstance(value, str) else None


def _result_state(result: object) -> str | None:
    value = result.state if isinstance(result, DevResult) else getattr(result, "state", None)
    return value if isinstance(value, str) else None


class SmokeStoreError(RuntimeError):
    """Ошибки собственного ограниченного постоянного состояния SmokeRun."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConfigRegistry:
    """Безопасный реестр обычных пользовательских параметров текущей схемы."""

    _ALLOWED_TYPES = frozenset({"checkbox", "select", "input", "textarea", "datetime"})
    _CAPABILITY_KEY = "smoke_override"

    def __init__(self, environment: DevEnvironment):
        self.environment = environment
        self.path = _ensure_scoped_path(
            environment.repository_root / "module" / "config" / "argument" / "args.json",
            environment.repository_root,
            label="реестр аргументов конфигурации",
        )
        self._leaves = self._load()

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            raw = read_bounded_bytes(self.path, max_bytes=2 * 1024 * 1024)
            payload = json.loads(raw.decode("utf-8"))
        except (FileNotFoundError, OSError, BoundedReadTooLarge, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeStoreError("DEV_SMOKE_CONFIG_SCHEMA_UNAVAILABLE", "Реестр конфигурации недоступен") from exc
        if not isinstance(payload, Mapping):
            raise SmokeStoreError("DEV_SMOKE_CONFIG_SCHEMA_INVALID", "Реестр конфигурации имеет неверную структуру")
        leaves: dict[str, dict[str, object]] = {}

        def visit(value: object, parts: tuple[str, ...]) -> None:
            if not isinstance(value, Mapping):
                return
            if len(parts) >= 3 and isinstance(value.get("type"), str) and "value" in value:
                path = ".".join(parts)
                if value["type"] in self._ALLOWED_TYPES and value.get("display") not in {"hide", "disabled"}:
                    leaves[path] = dict(value)
                return
            if len(parts) >= 5:
                return
            for key, child in value.items():
                if isinstance(key, str) and _SAFE_ID.fullmatch(key):
                    visit(child, (*parts, key))

        visit(payload, ())
        if not leaves:
            raise SmokeStoreError("DEV_SMOKE_CONFIG_SCHEMA_INVALID", "В реестре нет допустимых листовых параметров конфигурации")
        return leaves

    def _known_leaf(self, path: str) -> dict[str, object]:
        try:
            return self._leaves[path]
        except KeyError as exc:
            raise SmokeStoreError("DEV_SMOKE_CONFIG_PATH_UNSUPPORTED", "путь конфигурации отсутствует в текущем реестре") from exc

    @staticmethod
    def _defense_blocked(path: str) -> bool:
        parts = [part.casefold().replace("_", "") for part in path.split(".")]
        tokens = {token.casefold().replace("_", "") for token in _CONFIG_DEFENSE_BLOCK_TOKENS}
        return any(token in part for part in parts for token in tokens)

    def leaf(self, path: str) -> dict[str, object]:
        metadata = self._known_leaf(path)
        if self._defense_blocked(path):
            raise SmokeStoreError("DEV_SMOKE_CONFIG_PATH_UNSUPPORTED", "путь конфигурации находится в защищённой зоне")
        if metadata.get(self._CAPABILITY_KEY) is not True:
            raise SmokeStoreError("DEV_SMOKE_CONFIG_CAPABILITY_REQUIRED", "путь конфигурации не разрешён для Smoke override")
        return metadata

    @staticmethod
    def _same_scalar_type(value: object, expected: object) -> bool:
        if expected is None:
            return value is None or isinstance(value, (bool, int, float, str))
        if isinstance(expected, bool):
            return isinstance(value, bool)
        if isinstance(expected, int) and not isinstance(expected, bool):
            return isinstance(value, int) and not isinstance(value, bool)
        if isinstance(expected, float):
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(expected, str):
            return isinstance(value, str)
        return False

    def validate_value(self, path: str, value: ScalarValue) -> None:
        metadata = self.leaf(path)
        options = metadata.get("option")
        if isinstance(options, list) and not any(self._same_scalar_type(value, item) and value == item for item in options):
            raise SmokeStoreError("DEV_SMOKE_CONFIG_VALUE_INVALID", "переопределение конфигурации не входит в допустимые варианты")
        if metadata.get("type") == "checkbox" and not isinstance(value, bool):
            raise SmokeStoreError("DEV_SMOKE_CONFIG_VALUE_INVALID", "переопределение флажка должно быть логическим значением")
        if not self._same_scalar_type(value, metadata.get("value")):
            raise SmokeStoreError("DEV_SMOKE_CONFIG_VALUE_INVALID", "переопределение конфигурации имеет неверный тип")

    def validate_overrides(self, overrides: Sequence[SmokeConfigOverride], payload: object) -> None:
        if not isinstance(payload, Mapping):
            raise SmokeStoreError("DEV_SMOKE_PROFILE_INVALID", "Профиль development target должен быть JSON-объектом")
        for override in overrides:
            self.validate_value(override.path, override.value)
            marker = object()
            current = _deep_get(payload, override.path, marker)
            if current is marker:
                raise SmokeStoreError("DEV_SMOKE_CONFIG_PATH_MISSING", "путь конфигурации отсутствует в development target")
            if not self._same_scalar_type(override.value, current):
                raise SmokeStoreError("DEV_SMOKE_CONFIG_VALUE_INVALID", "переопределение конфигурации не соответствует типу текущего значения")

    def allowed_paths(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                path
                for path, metadata in self._leaves.items()
                if metadata.get(self._CAPABILITY_KEY) is True and not self._defense_blocked(path)
            )
        )


def _deep_get(payload: object, path: str, default: object = None) -> object:
    current = payload
    try:
        for part in path.split("."):
            if not isinstance(current, Mapping):
                return default
            current = current[part]
        return current
    except (KeyError, TypeError):
        return default


def _deep_set(payload: object, path: str, value: ScalarValue) -> None:
    parts = path.split(".")
    current = payload
    if not isinstance(current, dict):
        raise SmokeStoreError("DEV_SMOKE_PROFILE_INVALID", "профиль нельзя изменить")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            raise SmokeStoreError("DEV_SMOKE_CONFIG_PATH_MISSING", "путь конфигурации больше не является объектом")
        current = child
    current[parts[-1]] = value


def _semantic_profile_digest(payload: object, registry: ConfigRegistry) -> str:
    if not isinstance(payload, Mapping):
        raise SmokeStoreError("DEV_SMOKE_PROFILE_INVALID", "профиль нельзя наблюдать")
    values: dict[str, object] = {}
    missing = object()
    for path in registry.allowed_paths():
        value = _deep_get(payload, path, missing)
        if value is missing:
            # Обычный загрузчик профиля материализует отсутствующие листы их
            # текущими значениями по умолчанию при первом сохранении ap.json.
            value = registry.leaf(path).get("value")
        values[path] = value
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


class SmokeOverrideTransaction:
    """Транзакция только для объявленных листовых параметров без полной копии ap.json."""

    def __init__(
        self,
        environment: DevEnvironment,
        registry: ConfigRegistry,
        overrides: Sequence[SmokeConfigOverride],
        *,
        save_state: Callable[[SmokeOverrideState], None],
    ) -> None:
        self.environment = environment
        self.registry = registry
        self.overrides = tuple(overrides)
        self.save_state = save_state
        self.snapshots: list[SmokeOverrideSnapshot] = []
        self.baseline_digest: str | None = None
        self.restored = False

    def apply(self) -> None:
        payload = read_profile_payload(self.environment.profile_file, repository_root=self.environment.repository_root)
        self.registry.validate_overrides(self.overrides, payload)
        self.baseline_digest = _semantic_profile_digest(payload, self.registry)
        snapshots: list[SmokeOverrideSnapshot] = []
        for override in self.overrides:
            marker = object()
            original = _deep_get(payload, override.path, marker)
            if original is marker:
                raise SmokeStoreError("DEV_SMOKE_CONFIG_PATH_MISSING", "невозможно сохранить исходное переопределяемое значение")
            snapshots.append(
                SmokeOverrideSnapshot(
                    path=override.path,
                    original_value=original if original is not None else None,
                    applied_value=override.value,
                )
            )
        self.snapshots = snapshots
        self.save_state(
            SmokeOverrideState(
                snapshots=snapshots,
                persisted=True,
                applied=False,
                restored=False,
                verified=False,
                baseline_digest=self.baseline_digest,
            )
        )
        if not snapshots:
            self.restored = True
            self.save_state(
                SmokeOverrideState(
                    snapshots=[],
                    persisted=True,
                    applied=True,
                    restored=True,
                    verified=True,
                    baseline_digest=self.baseline_digest,
                )
            )
            return
        changed = copy.deepcopy(dict(payload))
        for snapshot in snapshots:
            _deep_set(changed, snapshot.path, snapshot.applied_value)
        try:
            write_profile_payload(
                self.environment.profile_file,
                changed,
                repository_root=self.environment.repository_root,
            )
            readback = read_profile_payload(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
            )
            for snapshot in snapshots:
                if _deep_get(readback, snapshot.path, object()) != snapshot.applied_value:
                    raise SmokeStoreError("DEV_SMOKE_CONFIG_APPLY_VERIFY_FAILED", "повторное чтение переопределения конфигурации не совпало")
        except Exception:
            try:
                self.restore()
            except Exception:  # noqa: BLE001, S110 — сохраняем исходную ошибку применения
                pass
            raise
        self.restored = False
        self.save_state(
            SmokeOverrideState(
                snapshots=snapshots,
                persisted=True,
                applied=True,
                restored=False,
                verified=True,
                baseline_digest=self.baseline_digest,
            )
        )

    def restore(self) -> bool:
        state = SmokeOverrideState(
            snapshots=self.snapshots,
            persisted=bool(self.snapshots) or True,
            applied=bool(self.snapshots),
            restored=False,
            verified=False,
            baseline_digest=self.baseline_digest,
        )
        if not self.snapshots:
            self.restored = True
            self.save_state(
                SmokeOverrideState(
                    snapshots=[],
                    persisted=True,
                    applied=True,
                    restored=True,
                    verified=True,
                    baseline_digest=self.baseline_digest,
                )
            )
            return True
        payload = read_profile_payload(self.environment.profile_file, repository_root=self.environment.repository_root)
        changed = copy.deepcopy(dict(payload))
        for snapshot in self.snapshots:
            current = _deep_get(payload, snapshot.path, object())
            if current != snapshot.applied_value and current != snapshot.original_value:
                self.restored = False
                self.save_state(state)
                return False
            _deep_set(changed, snapshot.path, snapshot.original_value)
        write_profile_payload(
            self.environment.profile_file,
            changed,
            repository_root=self.environment.repository_root,
        )
        readback = read_profile_payload(self.environment.profile_file, repository_root=self.environment.repository_root)
        verified = all(_deep_get(readback, item.path, object()) == item.original_value for item in self.snapshots)
        self.save_state(
            SmokeOverrideState(
                snapshots=self.snapshots,
                persisted=True,
                applied=True,
                restored=verified,
                verified=verified,
                baseline_digest=self.baseline_digest,
            )
        )
        self.restored = verified
        return verified

    def mutation_guard_ok(self) -> bool:
        if self.baseline_digest is None:
            return True
        payload = read_profile_payload(self.environment.profile_file, repository_root=self.environment.repository_root)
        current = _semantic_profile_digest(payload, self.registry)
        if current == self.baseline_digest:
            return True
        allowed_payload = copy.deepcopy(dict(payload))
        for snapshot in self.snapshots:
            _deep_set(allowed_payload, snapshot.path, snapshot.original_value)
        return _semantic_profile_digest(allowed_payload, self.registry) == self.baseline_digest

    @classmethod
    def from_state(
        cls,
        environment: DevEnvironment,
        registry: ConfigRegistry,
        state: SmokeOverrideState,
        *,
        save_state: Callable[[SmokeOverrideState], None],
    ) -> SmokeOverrideTransaction:
        transaction = cls(environment, registry, (), save_state=save_state)
        transaction.snapshots = list(state.snapshots)
        transaction.baseline_digest = state.baseline_digest
        transaction.restored = state.restored
        return transaction


@dataclass(frozen=True, slots=True)
class TimelineObservation:
    sequence: int
    event_type: str
    fields: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class StructuredErrorObservation:
    exception_type: str | None
    code: str | None
    message: str
    sequence: int | None


@dataclass(frozen=True, slots=True)
class SmokeObservationContext:
    """Ограниченные данные только для чтения, передаваемые оценщикам capabilities."""

    timeline: tuple[TimelineObservation, ...]
    logs: tuple[str, ...]
    evidence_health: str
    runtime_state: str
    task_policy_state: str | None
    current_task: str | None
    config_values: Mapping[str, ScalarValue]
    restored_paths: frozenset[str]
    port_listening: bool | None
    elapsed_seconds: float
    completed: bool
    session_id: str | None
    structured_errors: tuple[StructuredErrorObservation, ...]
    screenshot_metadata: tuple[Mapping[str, object], ...]
    log_available: bool
    log_truncated: bool


@dataclass(frozen=True, slots=True)
class CapabilityEvaluation:
    status: SmokeAssertionStatus
    source: str
    message: str
    references: tuple[SmokeEvidenceRef, ...] = ()


@dataclass(frozen=True, slots=True)
class _CapabilityDefinition:
    descriptor: SmokeCapabilityDescriptor
    evaluator: Callable[[object, SmokeObservationContext], CapabilityEvaluation]


class SmokeFieldSchema(_StrictModel):
    name: str
    value_type: str
    required: StrictBool
    minimum: StrictFloat | None = None
    maximum: StrictFloat | None = None
    enum_values: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("name", "value_type")
    @classmethod
    def validate_field_text(cls, value: str, info: object) -> str:
        return _text(value, field_name=getattr(info, "field_name", "field"), maximum=128)


class SmokeCapabilitySchema(_StrictModel):
    fields: list[SmokeFieldSchema] = Field(default_factory=list, max_length=16)


class SmokeCapabilityDescriptor(_StrictModel):
    capability_id: str
    kind: Literal["assertion", "observation", "setup", "external_evaluation"]
    config_schema: SmokeCapabilitySchema
    evidence_source: Literal[
        "canonical_timeline",
        "runtime_state",
        "task_policy",
        "structured_error",
        "config",
        "session_log",
        "external_visual",
    ]
    deterministic: StrictBool
    external: StrictBool
    available: StrictBool
    description: str

    @field_validator("capability_id")
    @classmethod
    def validate_capability_id(cls, value: str) -> str:
        return _identifier(value, field_name="capability_id")

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _text(value, field_name="description", maximum=SMOKE_MAX_RESULT_TEXT)


def _ref(source: str, reference: str, description: str) -> SmokeEvidenceRef:
    return SmokeEvidenceRef(source=source, reference=reference, description=description)


def _event_matches(ctx: SmokeObservationContext, event_type: str) -> TimelineObservation | None:
    return next((event for event in ctx.timeline if event.event_type == event_type), None)


def _task_event(ctx: SmokeObservationContext, event_type: str, task: str) -> TimelineObservation | None:
    for event in ctx.timeline:
        if event.event_type != event_type:
            continue
        if event.fields.get("task") == task or event.fields.get("current_task") == task:
            return event
    return None


def _eval_event_occurred(assertion: EventOccurredAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    event = _event_matches(ctx, assertion.event_type)
    if event is None:
        return CapabilityEvaluation(
            SmokeAssertionStatus.PENDING,
            "canonical_timeline",
            f"Событие {assertion.event_type} ещё не наблюдалось",
            (_ref("canonical_timeline", "event-observation", "Текущая ограниченная timeline Evidence API"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PASS,
        "canonical_timeline",
        f"Событие {assertion.event_type} наблюдалось",
        (_ref("canonical_timeline", f"sequence:{event.sequence}", "Каноническое событие Evidence API"),),
    )


def _eval_event_not_occurred(assertion: EventNotOccurredAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    event = _event_matches(ctx, assertion.event_type)
    if event is not None:
        return CapabilityEvaluation(
            SmokeAssertionStatus.FAIL,
            "canonical_timeline",
            f"Негативное условие нарушено событием {assertion.event_type}",
            (_ref("canonical_timeline", f"sequence:{event.sequence}", "Нежелательное каноническое событие"),),
        )
    if ctx.elapsed_seconds < float(assertion.observation_window_seconds):
        return CapabilityEvaluation(
            SmokeAssertionStatus.PENDING,
            "canonical_timeline",
            "Окно отрицательного наблюдения ещё не закрыто",
            (_ref("canonical_timeline", "observation-window", "Текущее ограниченное окно timeline Evidence API"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PASS,
        "canonical_timeline",
        "Нежелательное событие отсутствовало после закрытия окна наблюдения",
        (_ref("canonical_timeline", "observation-window", "Закрытое ограниченное окно timeline Evidence API"),),
    )


def _eval_task_started(assertion: TaskStartedAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    event = _task_event(ctx, "task_started", assertion.task)
    if event is None:
        return CapabilityEvaluation(
            SmokeAssertionStatus.PENDING,
            "canonical_timeline",
            "Task ещё не запускалась",
            (_ref("canonical_timeline", "task-observation", "Текущая ограниченная timeline Evidence API"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PASS,
        "canonical_timeline",
        "Task запускалась",
        (_ref("canonical_timeline", f"sequence:{event.sequence}", "Запуск разрешённой task"),),
    )


def _eval_task_not_started(assertion: TaskNotStartedAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    event = _task_event(ctx, "task_started", assertion.task)
    if event is not None:
        return CapabilityEvaluation(
            SmokeAssertionStatus.FAIL,
            "canonical_timeline",
            "Запрещённая task была запущена",
            (_ref("canonical_timeline", f"sequence:{event.sequence}", "Нежелательный запуск task"),),
        )
    if ctx.elapsed_seconds < float(assertion.observation_window_seconds):
        return CapabilityEvaluation(
            SmokeAssertionStatus.PENDING,
            "canonical_timeline",
            "Окно наблюдения ещё не закрыто",
            (_ref("canonical_timeline", "observation-window", "Текущее ограниченное окно timeline Evidence API"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PASS,
        "canonical_timeline",
        "Task не запускалась после закрытия окна наблюдения",
        (_ref("canonical_timeline", "observation-window", "Закрытое ограниченное окно timeline Evidence API"),),
    )


def _eval_dependency(assertion: DependencyOccurredAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    for event in ctx.timeline:
        if event.event_type == "dependency_registered" and event.fields.get("task") == assertion.task and event.fields.get("required_by") == assertion.required_by:
            return CapabilityEvaluation(
                SmokeAssertionStatus.PASS,
                "task_policy",
                "Зависимость с подтверждённым происхождением наблюдалась",
                (_ref("task_policy", f"sequence:{event.sequence}", "Зарегистрированная зависимость с подтверждённым происхождением"),),
            )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PENDING,
        "task_policy",
        "Зависимость с подтверждённым происхождением ещё не наблюдалась",
        (_ref("task_policy", "dependency-observation", "Текущая проверка происхождения зависимости Evidence API"),),
    )


def _eval_no_runtime_error(assertion: NoRuntimeErrorAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    if ctx.structured_errors:
        error = ctx.structured_errors[0]
        reference = f"sequence:{error.sequence}" if error.sequence is not None else "last_error"
        return CapabilityEvaluation(
            SmokeAssertionStatus.FAIL,
            "structured_error",
            "Наблюдалась необъявленная ошибка выполнения",
            (_ref("structured_error", reference, "Структурированная ошибка выполнения"),),
        )
    if ctx.elapsed_seconds < float(assertion.observation_window_seconds):
        return CapabilityEvaluation(
            SmokeAssertionStatus.PENDING,
            "structured_error",
            "Окно проверки ошибок выполнения ещё не закрыто",
            (_ref("structured_error", "observation-window", "Текущее ограниченное окно структурированных ошибок"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PASS,
        "structured_error",
        "Ошибки выполнения не наблюдались после закрытия окна",
        (_ref("structured_error", "observation-window", "Закрытое ограниченное окно структурированных ошибок"),),
    )


def _eval_expected_error(assertion: ExpectedSafeErrorAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    for error in ctx.structured_errors:
        if assertion.error_type is not None and error.exception_type != assertion.error_type:
            continue
        if assertion.error_code is not None and error.code != assertion.error_code:
            continue
        reference = f"sequence:{error.sequence}" if error.sequence is not None else "last_error"
        return CapabilityEvaluation(
            SmokeAssertionStatus.PASS,
            "structured_error",
            "Ожидаемая безопасная ошибка подтверждена",
            (_ref("structured_error", reference, "Заявленная структурированная ошибка"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PENDING,
        "structured_error",
        "Ожидаемая ошибка ещё не наблюдалась",
        (_ref("structured_error", "error-observation", "Текущая проверка структурированной ошибки"),),
    )


def _eval_evidence_health(assertion: EvidenceHealthAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    status = SmokeAssertionStatus.PASS if ctx.evidence_health == assertion.expected_status else SmokeAssertionStatus.FAIL
    return CapabilityEvaluation(
        status,
        "runtime_state",
        f"Состояние evidence: {ctx.evidence_health}",
        (_ref("runtime_state", "evidence_health", "Полнота подтверждающих данных Evidence API"),),
    )


def _eval_runtime_state(assertion: RuntimeStateAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    status = SmokeAssertionStatus.PASS if ctx.runtime_state == assertion.expected_state else SmokeAssertionStatus.FAIL
    return CapabilityEvaluation(
        status,
        "runtime_state",
        f"Состояние среды выполнения: {ctx.runtime_state}",
        (_ref("runtime_state", "status", "Наблюдаемое состояние Dev Runtime"),),
    )


def _eval_port_state(assertion: DevPortStateAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    if ctx.port_listening is None:
        return CapabilityEvaluation(
            SmokeAssertionStatus.UNAVAILABLE,
            "runtime_state",
            "Состояние фиксированного Dev port неизвестно",
            (_ref("runtime_state", "dev-port", "Проверка фиксированного Dev port через Evidence API"),),
        )
    actual = "listening" if ctx.port_listening else "free"
    status = SmokeAssertionStatus.PASS if actual == assertion.expected_state else SmokeAssertionStatus.FAIL
    return CapabilityEvaluation(
        status,
        "runtime_state",
        f"Dev port: {actual}",
        (_ref("runtime_state", "dev-port", "Проверка фиксированного Dev port через Evidence API"),),
    )


def _eval_config_value(assertion: ConfigValueAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    if assertion.path not in ctx.config_values:
        return CapabilityEvaluation(
            SmokeAssertionStatus.UNAVAILABLE,
            "config",
            "Путь конфигурации не представлен безопасным наблюдением",
            (_ref("config", f"path:{assertion.path}", "Наблюдение объявленного параметра конфигурации"),),
        )
    actual = ctx.config_values[assertion.path]
    status = SmokeAssertionStatus.PASS if actual == assertion.expected_value else SmokeAssertionStatus.FAIL
    return CapabilityEvaluation(
        status,
        "config",
        "Значение config совпадает с ожиданием" if status is SmokeAssertionStatus.PASS else "Значение config не совпадает",
        (_ref("config", f"path:{assertion.path}", "Наблюдение объявленного параметра конфигурации"),),
    )


def _eval_config_restored(assertion: ConfigRestoredAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    if assertion.path not in ctx.config_values:
        return CapabilityEvaluation(
            SmokeAssertionStatus.UNAVAILABLE,
            "config",
            "Путь конфигурации не представлен безопасным наблюдением",
            (_ref("config", f"path:{assertion.path}", "Наблюдение восстановления объявленного параметра"),),
        )
    if assertion.path not in ctx.restored_paths:
        return CapabilityEvaluation(
            SmokeAssertionStatus.FAIL,
            "config",
            "Восстановление объявленного пути конфигурации не подтверждено",
            (_ref("config", f"path:{assertion.path}", "Наблюдение восстановления объявленного параметра"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PASS,
        "config",
        "Объявленный путь конфигурации восстановлен",
        (_ref("config", f"path:{assertion.path}", "Наблюдение восстановления объявленного параметра"),),
    )


def _eval_duration(assertion: DurationWithinBoundAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    if ctx.elapsed_seconds > float(assertion.maximum_seconds):
        return CapabilityEvaluation(
            SmokeAssertionStatus.FAIL,
            "runtime_state",
            "Smoke превысил ограничение длительности",
            (_ref("runtime_state", "duration", "Длительность жизненного цикла Dev Runtime"),),
        )
    if not ctx.completed and ctx.elapsed_seconds < float(assertion.minimum_seconds):
        return CapabilityEvaluation(
            SmokeAssertionStatus.PENDING,
            "runtime_state",
            "Минимальная длительность ещё не достигнута",
            (_ref("runtime_state", "duration", "Длительность жизненного цикла Dev Runtime"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PASS,
        "runtime_state",
        "Duration находится в заданных границах",
        (_ref("runtime_state", "duration", "Длительность жизненного цикла Dev Runtime"),),
    )


def _eval_log_contains(assertion: SessionLogContainsAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    if not ctx.log_available:
        return CapabilityEvaluation(
            SmokeAssertionStatus.UNAVAILABLE,
            "session_log",
            "Журнал сессии недоступен",
            (_ref("session_log", "bounded-log", "Ограниченный журнал сессии Evidence API"),),
        )
    if any(assertion.literal in line for line in ctx.logs):
        return CapabilityEvaluation(
            SmokeAssertionStatus.PASS,
            "session_log",
            "Журнал сессии содержит заданный фрагмент",
            (_ref("session_log", "bounded-log", "Ограниченный журнал сессии Evidence API"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PENDING if not ctx.completed else SmokeAssertionStatus.FAIL,
        "session_log",
        "Заданный фрагмент в журнале сессии не найден",
        (_ref("session_log", "bounded-log", "Ограниченный журнал сессии Evidence API"),),
    )


def _eval_log_not_contains(assertion: SessionLogNotContainsAssertion, ctx: SmokeObservationContext) -> CapabilityEvaluation:
    if not ctx.log_available:
        return CapabilityEvaluation(
            SmokeAssertionStatus.UNAVAILABLE,
            "session_log",
            "Журнал сессии недоступен",
            (_ref("session_log", "bounded-log", "Ограниченный журнал сессии Evidence API"),),
        )
    if any(assertion.literal in line for line in ctx.logs):
        return CapabilityEvaluation(
            SmokeAssertionStatus.FAIL,
            "session_log",
            "Запрещённый фрагмент найден в журнале сессии",
            (_ref("session_log", "bounded-log", "Ограниченный журнал сессии Evidence API"),),
        )
    if ctx.elapsed_seconds < float(assertion.observation_window_seconds):
        return CapabilityEvaluation(
            SmokeAssertionStatus.PENDING,
            "session_log",
            "Окно проверки журнала ещё не закрыто",
            (_ref("session_log", "observation-window", "Текущее ограниченное окно журнала сессии Evidence API"),),
        )
    return CapabilityEvaluation(
        SmokeAssertionStatus.PASS,
        "session_log",
        "Запрещённый literal отсутствовал после закрытия окна",
        (_ref("session_log", "observation-window", "Закрытое ограниченное окно журнала сессии Evidence API"),),
    )


def _capability_fields(*fields: SmokeFieldSchema) -> SmokeCapabilitySchema:
    return SmokeCapabilitySchema(fields=list(fields))


def _field(name: str, value_type: str, required: bool, *, minimum: float | None = None, maximum: float | None = None, enum_values: Sequence[str] = ()) -> SmokeFieldSchema:
    return SmokeFieldSchema(
        name=name,
        value_type=value_type,
        required=required,
        minimum=minimum,
        maximum=maximum,
        enum_values=list(enum_values),
    )


class SmokeCapabilityRegistry:
    """Машиночитаемый реестр: описание capability и типизированный оценщик связаны одной записью."""

    def __init__(self) -> None:
        text_field = _field("assertion_id", "safe_identifier", True)
        required_field = _field("required", "boolean", False)
        definitions = [
            ("event_occurred", "assertion", "canonical_timeline", True, False, "Подтвердить наличие канонического события", _eval_event_occurred, _capability_fields(text_field, required_field, _field("event_type", "event_type", True, enum_values=sorted(EVIDENCE_EVENT_TYPES)))),
            ("event_not_occurred", "assertion", "canonical_timeline", True, False, "Подтвердить отсутствие события после окна наблюдения", _eval_event_not_occurred, _capability_fields(text_field, required_field, _field("event_type", "event_type", True), _field("observation_window_seconds", "duration", False, minimum=0.1, maximum=SMOKE_MAX_OBSERVATION_SECONDS))),
            ("task_started", "assertion", "canonical_timeline", True, False, "Подтвердить запуск task через timeline", _eval_task_started, _capability_fields(text_field, required_field, _field("task", "task_selector", True))),
            ("task_not_started", "assertion", "canonical_timeline", True, False, "Подтвердить отсутствие запуска task после окна наблюдения", _eval_task_not_started, _capability_fields(text_field, required_field, _field("task", "task_selector", True), _field("observation_window_seconds", "duration", False, minimum=0.1, maximum=SMOKE_MAX_OBSERVATION_SECONDS))),
            ("dependency_occurred", "assertion", "task_policy", True, False, "Подтвердить зарегистрированную зависимость с происхождением", _eval_dependency, _capability_fields(text_field, required_field, _field("task", "task_selector", True), _field("required_by", "task_selector", True))),
            ("no_runtime_error", "assertion", "structured_error", True, False, "Подтвердить отсутствие необъявленной ошибки выполнения", _eval_no_runtime_error, _capability_fields(text_field, required_field, _field("observation_window_seconds", "duration", False, minimum=0.1, maximum=SMOKE_MAX_OBSERVATION_SECONDS))),
            ("expected_safe_error", "assertion", "structured_error", True, False, "Подтвердить заранее объявленную безопасную ошибку", _eval_expected_error, _capability_fields(text_field, required_field, _field("error_type", "string", False), _field("error_code", "string", False))),
            ("evidence_health", "assertion", "runtime_state", True, False, "Проверить полноту подтверждающих данных Evidence API", _eval_evidence_health, _capability_fields(text_field, required_field, _field("expected_status", "enum", False, enum_values=[EVIDENCE_HEALTH_COMPLETE, EVIDENCE_HEALTH_DEGRADED, EVIDENCE_HEALTH_CORRUPT, EVIDENCE_HEALTH_UNAVAILABLE]))),
            ("runtime_state", "assertion", "runtime_state", True, False, "Проверить наблюдаемое состояние Dev Runtime", _eval_runtime_state, _capability_fields(text_field, required_field, _field("expected_state", "enum", True, enum_values=["running", "stopped", "starting", "failed"]))),
            ("dev_port_state", "assertion", "runtime_state", True, False, "Проверить фиксированный Dev port", _eval_port_state, _capability_fields(text_field, required_field, _field("expected_state", "enum", True, enum_values=["listening", "free"]))),
            ("config_value", "assertion", "config", True, False, "Проверить безопасно наблюдаемое значение config", _eval_config_value, _capability_fields(text_field, required_field, _field("path", "canonical_config_path", True), _field("expected_value", "scalar", True))),
            ("config_restored", "assertion", "config", True, False, "Проверить восстановление объявленного пути конфигурации", _eval_config_restored, _capability_fields(text_field, required_field, _field("path", "canonical_config_path", True))),
            ("duration_within_bound", "assertion", "runtime_state", True, False, "Проверить ограничение длительности smoke", _eval_duration, _capability_fields(text_field, required_field, _field("maximum_seconds", "duration", True, minimum=0.0, maximum=SMOKE_MAX_OBSERVATION_SECONDS), _field("minimum_seconds", "duration", False, minimum=0.0, maximum=SMOKE_MAX_OBSERVATION_SECONDS))),
            ("session_log_contains_literal", "assertion", "session_log", True, False, "Найти ограниченный literal в журнале сессии", _eval_log_contains, _capability_fields(text_field, required_field, _field("literal", "bounded_literal", True))),
            ("session_log_does_not_contain_literal", "assertion", "session_log", True, False, "Подтвердить отсутствие ограниченного literal в журнале", _eval_log_not_contains, _capability_fields(text_field, required_field, _field("literal", "bounded_literal", True), _field("observation_window_seconds", "duration", False, minimum=0.1, maximum=SMOKE_MAX_OBSERVATION_SECONDS))),
        ]
        visual = SmokeCapabilityDescriptor(
            capability_id="external_visual",
            kind="external_evaluation",
            config_schema=_capability_fields(text_field, required_field, _field("rubric", "bounded_rubric", True), _field("capture_condition", "typed_capture_condition", True)),
            evidence_source="external_visual",
            deterministic=False,
            external=True,
            available=True,
            description="Сохранить объявленный screenshot Evidence API для внешней оценки после cleanup",
        )
        self._definitions: dict[str, _CapabilityDefinition] = {}
        for capability_id, kind, source, deterministic, external, description, evaluator, schema in definitions:
            self._definitions[capability_id] = _CapabilityDefinition(
                SmokeCapabilityDescriptor(
                    capability_id=capability_id,
                    kind=kind,
                    config_schema=schema,
                    evidence_source=source,
                    deterministic=deterministic,
                    external=external,
                    available=True,
                    description=description,
                ),
                evaluator,
            )
        self._definitions["external_visual"] = _CapabilityDefinition(
            visual,
            lambda _a, _c: CapabilityEvaluation(
                SmokeAssertionStatus.PENDING,
                "external_visual",
                "Ожидается внешняя оценка",
                (_ref("external_visual", "screenshot-pending", "Ожидание точного screenshot Evidence API"),),
            ),
        )

    def descriptors(self) -> list[SmokeCapabilityDescriptor]:
        return [self._definitions[key].descriptor for key in sorted(self._definitions)]

    def register(
        self,
        descriptor: SmokeCapabilityDescriptor,
        evaluator: Callable[[object, SmokeObservationContext], CapabilityEvaluation],
    ) -> None:
        """Добавить новую типизированную capability до запуска SmokeRun."""

        if descriptor.capability_id in self._definitions:
            raise SmokeStoreError("DEV_SMOKE_CAPABILITY_CONFLICT", "Такая capability Smoke уже зарегистрирована")
        self._definitions[descriptor.capability_id] = _CapabilityDefinition(descriptor, evaluator)

    def definition(self, capability_id: str) -> _CapabilityDefinition:
        try:
            return self._definitions[capability_id]
        except KeyError as exc:
            raise SmokeStoreError("DEV_SMOKE_CAPABILITY_UNAVAILABLE", "Такая capability Smoke отсутствует в реестре") from exc

    def validate_spec(self, spec: SmokeSpec) -> None:
        for assertion in spec.assertions:
            definition = self.definition(assertion.capability_id)
            if not definition.descriptor.available or definition.descriptor.external:
                raise SmokeStoreError("DEV_SMOKE_CAPABILITY_UNAVAILABLE", "Детерминированная capability assertion недоступна")
        for assertion in spec.visual_assertions:
            definition = self.definition(assertion.capability_id)
            if not definition.descriptor.available:
                raise SmokeStoreError("DEV_SMOKE_CAPABILITY_UNAVAILABLE", "Внешняя визуальная capability недоступна")

    def evaluate(self, assertion: object, context: SmokeObservationContext) -> CapabilityEvaluation:
        capability_id = getattr(assertion, "capability_id", None)
        if not isinstance(capability_id, str):
            return CapabilityEvaluation(SmokeAssertionStatus.UNAVAILABLE, "runtime_state", "Capability assertion имеет неверную форму", (_ref("runtime_state", "assertion", "Проверка формы assertion"),))
        try:
            return self.definition(capability_id).evaluator(assertion, context)
        except (AttributeError, TypeError, ValueError):
            return CapabilityEvaluation(SmokeAssertionStatus.UNAVAILABLE, "runtime_state", "Типизированный оценщик не смог обработать assertion", (_ref("runtime_state", "assertion", "Проверка typed assertion"),))


class SmokeSupervisorBackend:
    """Только фиксированный запуск независимого supervisor через Python проекта."""

    def launch(self, environment: DevEnvironment, smoke_id: str) -> SmokeSupervisorIdentity:
        command = [
            str(environment.python_executable),
            "-m",
            "module.dev_runtime.smoke_supervisor",
            "--smoke-id",
            smoke_id,
        ]
        kwargs: dict[str, object] = {
            "cwd": str(environment.repository_root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        try:
            observed = psutil.Process(process.pid)
            created_at = float(observed.create_time())
            executable = observed.exe() or command[0]
            command_line = observed.cmdline() or command
            cwd = observed.cwd() or str(environment.repository_root)
        except (psutil.Error, OSError):
            created_at = time.time()
            executable = command[0]
            command_line = command
            cwd = str(environment.repository_root)
        return SmokeSupervisorIdentity(
            pid=process.pid,
            created_at=created_at,
            executable=executable,
            command_line=list(command_line),
            cwd=cwd,
        )

    def stop(self, environment: DevEnvironment, smoke_id: str, identity: SmokeSupervisorIdentity) -> bool:
        """Остановить только supervisor с совпавшей полной замороженной идентичностью."""

        if self.matches(environment, smoke_id, identity) is not True:
            return False
        try:
            process = psutil.Process(identity.pid)
            process.terminate()
            process.wait(timeout=5.0)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return True
        except psutil.TimeoutExpired:
            if self.matches(environment, smoke_id, identity) is not True:
                return False
            try:
                process.kill()
                process.wait(timeout=5.0)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                return True
            except (psutil.AccessDenied, psutil.Error, OSError):
                return False
        except (psutil.AccessDenied, psutil.Error, OSError):
            return False
        return not process.is_running()

    @staticmethod
    def matches(environment: DevEnvironment, smoke_id: str, identity: SmokeSupervisorIdentity) -> bool | None:
        expected = [
            str(environment.python_executable),
            "-m",
            "module.dev_runtime.smoke_supervisor",
            "--smoke-id",
            smoke_id,
        ]
        try:
            process = psutil.Process(identity.pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
            actual_cmd = process.cmdline()
            actual_cwd = process.cwd()
            actual_executable = process.exe()
            actual_created = float(process.create_time())
        except psutil.NoSuchProcess:
            return None
        except (psutil.AccessDenied, psutil.ZombieProcess):
            return None
        except (psutil.Error, OSError):
            return False
        if abs(actual_created - identity.created_at) > 0.01:
            return False
        if actual_cmd != expected or os.path.normcase(os.path.abspath(actual_cwd)) != os.path.normcase(os.path.abspath(environment.repository_root)):
            return False
        allowed = {os.path.normcase(os.path.abspath(str(environment.python_executable)))}
        if os.name == "nt":
            base = getattr(__import__("sys"), "_base_executable", None)
            if isinstance(base, str) and base:
                allowed.add(os.path.normcase(os.path.abspath(base)))
        return os.path.normcase(os.path.abspath(actual_executable)) in allowed


@dataclass(frozen=True, slots=True)
class _RuntimeObservation:
    context: SmokeObservationContext
    source: SmokeSourceSnapshot
    evidence_ok: bool
    evidence_reason: str | None


class SmokeStateStore:
    """Атомарное состояние в пределах репозитория с отдельными файлами spec/result/control."""

    def __init__(self, environment: DevEnvironment, *, now: Callable[[], datetime] | None = None) -> None:
        self.environment = environment
        self.now = now or (lambda: datetime.now(UTC))
        self.root = _ensure_scoped_path(
            environment.repository_root / "config" / "state" / "dev-runtime-smoke",
            environment.repository_root,
            label="корень SmokeRun state",
        )
        self.lock_path = _ensure_scoped_path(
            environment.repository_root / "config" / "state" / "dev-runtime-smoke.lock",
            environment.repository_root,
            label="блокировка SmokeRun state",
        )
        self._thread_lock = threading.RLock()

    def _run_dir(self, smoke_id: str) -> Path:
        if not _SAFE_ID.fullmatch(smoke_id):
            raise SmokeStoreError("DEV_SMOKE_ID_INVALID", "smoke_id имеет недопустимый формат")
        return _ensure_scoped_path(self.root / smoke_id, self.environment.repository_root, label="каталог SmokeRun")

    def _file(self, smoke_id: str, name: str) -> Path:
        directory = self._run_dir(smoke_id)
        if name not in {"spec.json", "state.json", "result.json", "control.json"}:
            raise SmokeStoreError("DEV_SMOKE_STATE_INVALID", "неизвестный файл состояния")
        return _ensure_scoped_path(directory / name, self.environment.repository_root, label="файл SmokeRun state")

    @contextmanager
    def _locked(self):
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.root) or _is_reparse_point(self.lock_path):
            raise SmokeStoreError("DEV_SMOKE_STATE_UNSAFE_PATH", "состояние SmokeRun не должно быть ссылкой или junction")
        with self._thread_lock:
            try:
                with _exclusive_policy_lock(self.lock_path):
                    yield
            except TaskSandboxError as exc:
                raise SmokeStoreError(exc.code, str(exc)) from exc

    @staticmethod
    def _read_json(path: Path, maximum: int) -> object:
        try:
            if _is_reparse_point(path):
                raise SmokeStoreError("DEV_SMOKE_STATE_UNSAFE_PATH", "файл состояния SmokeRun не должен быть ссылкой")
            data = read_bounded_bytes(path, max_bytes=maximum)
        except FileNotFoundError as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_MISSING", "файл состояния SmokeRun отсутствует") from exc
        except BoundedReadTooLarge as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_TOO_LARGE", "файл состояния SmokeRun превышает ограничение") from exc
        except OSError as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_UNREADABLE", "файл состояния SmokeRun невозможно прочитать") from exc
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_CORRUPT", "файл состояния SmokeRun содержит некорректный JSON") from exc

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, object]) -> None:
        try:
            _atomic_json_write(path, payload)
        except TaskSandboxError as exc:
            raise SmokeStoreError(exc.code, str(exc)) from exc

    def create(self, spec: SmokeSpec, source: SmokeSourceSnapshot, *, created_at: str, deadline_at: str, smoke_id: str | None = None) -> SmokeRunRecord:
        smoke_id = smoke_id or str(uuid.uuid4())
        smoke_id = _identifier(smoke_id, field_name="smoke_id")
        record = SmokeRunRecord(
            smoke_id=smoke_id,
            state=SmokeState.CREATED,
            spec_hash=spec.spec_hash(),
            source=source,
            created_at=created_at,
            deadline_at=deadline_at,
            target_profile=self.environment.profile_name,
            target_identity=target_identity(self.environment.dev_target),
        )
        with self._locked():
            self.root.mkdir(parents=True, exist_ok=True)
            if any(
                record.state
                in {
                    SmokeState.CREATED,
                    SmokeState.PREPARING,
                    SmokeState.RUNNING,
                    SmokeState.EVALUATING,
                    SmokeState.CLEANING_UP,
                    SmokeState.AWAITING_EXTERNAL_EVALUATION,
                }
                for record in self.list_records_unlocked()
            ):
                raise SmokeStoreError("DEV_SMOKE_ACTIVE_CONFLICT", "Активный SmokeRun уже существует")
            run_dir = self._run_dir(smoke_id)
            if os.path.lexists(run_dir):
                raise SmokeStoreError("DEV_SMOKE_ID_CONFLICT", "SmokeRun с таким smoke_id уже существует")
            run_dir.mkdir()
            self._write_json(self._file(smoke_id, "spec.json"), spec.canonical_dict())
            self._write_json(self._file(smoke_id, "state.json"), _safe_model_json(record))
            self._write_json(self._file(smoke_id, "control.json"), _safe_model_json(SmokeControl()))
        return record

    def _load_unlocked(self, smoke_id: str) -> SmokeRunRecord:
        raw = self._read_json(self._file(smoke_id, "state.json"), SMOKE_MAX_RUN_BYTES)
        try:
            return _validate_json_model(SmokeRunRecord, raw)  # type: ignore[return-value]
        except ValidationError as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_CORRUPT", "состояние SmokeRun имеет некорректную схему") from exc

    def load(self, smoke_id: str) -> SmokeRunRecord:
        with self._locked():
            return self._load_unlocked(smoke_id)

    def load_spec(self, smoke_id: str) -> SmokeSpec:
        with self._locked():
            raw = self._read_json(self._file(smoke_id, "spec.json"), SMOKE_MAX_SPEC_BYTES)
            try:
                spec = _validate_json_model(SmokeSpec, raw)
            except ValidationError as exc:
                raise SmokeStoreError("DEV_SMOKE_SPEC_CORRUPT", "SmokeSpec имеет некорректную схему") from exc
            record = self._load_unlocked(smoke_id)
            if spec.spec_hash() != record.spec_hash:
                raise SmokeStoreError("DEV_SMOKE_SPEC_HASH_MISMATCH", "spec_hash не соответствует замороженной спецификации")
            return spec

    @staticmethod
    def _next_state_allowed(current: SmokeState, target: SmokeState) -> bool:
        allowed = {
            SmokeState.CREATED: {SmokeState.PREPARING, SmokeState.FINISHED},
            SmokeState.PREPARING: {SmokeState.RUNNING, SmokeState.EVALUATING, SmokeState.CLEANING_UP, SmokeState.FINISHED},
            SmokeState.RUNNING: {SmokeState.EVALUATING, SmokeState.CLEANING_UP, SmokeState.FINISHED},
            SmokeState.EVALUATING: {SmokeState.CLEANING_UP, SmokeState.FINISHED},
            SmokeState.CLEANING_UP: {SmokeState.AWAITING_EXTERNAL_EVALUATION, SmokeState.FINISHED},
            SmokeState.AWAITING_EXTERNAL_EVALUATION: {SmokeState.FINISHED},
            SmokeState.FINISHED: set(),
        }
        return target == current or target in allowed[current]

    def update(self, smoke_id: str, updates: Mapping[str, object]) -> SmokeRunRecord:
        with self._locked():
            current = self._load_unlocked(smoke_id)
            if current.state is SmokeState.FINISHED and updates:
                raise SmokeStoreError("DEV_SMOKE_STATE_IMMUTABLE", "завершённый SmokeRun нельзя изменять")
            updated = self._updated_unlocked(current, updates)
            self._write_json(self._file(smoke_id, "state.json"), _safe_model_json(updated))
            return updated

    def _updated_unlocked(self, current: SmokeRunRecord, updates: Mapping[str, object]) -> SmokeRunRecord:
        payload = _safe_model_json(current)
        for key, value in updates.items():
            if key not in payload:
                raise SmokeStoreError("DEV_SMOKE_STATE_INVALID", "неизвестное поле состояния SmokeRun")
            if isinstance(value, BaseModel):
                payload[key] = _safe_model_json(value)
            elif isinstance(value, (list, tuple)):
                payload[key] = [_safe_model_json(item) if isinstance(item, BaseModel) else item for item in value]
            else:
                payload[key] = value
        try:
            updated = _validate_json_model(SmokeRunRecord, payload)
        except ValidationError as exc:
            raise SmokeStoreError("DEV_SMOKE_STATE_INVALID", "обновление SmokeRun нарушает строгую схему") from exc
        if not isinstance(updated, SmokeRunRecord):
            raise SmokeStoreError("DEV_SMOKE_STATE_INVALID", "обновление SmokeRun имеет неверный тип")
        if not self._next_state_allowed(current.state, updated.state):
            raise SmokeStoreError("DEV_SMOKE_STATE_TRANSITION_INVALID", "переход SmokeRun state запрещён")
        immutable = (
            "smoke_id",
            "spec_hash",
            "source",
            "created_at",
            "deadline_at",
            "target_profile",
            "target_identity",
        )
        if any(getattr(current, key) != getattr(updated, key) for key in immutable):
            raise SmokeStoreError("DEV_SMOKE_STATE_IMMUTABLE", "замороженные поля SmokeRun нельзя изменить")
        return updated

    @staticmethod
    def _result_matches_record(result: SmokeResult, record: SmokeRunRecord) -> bool:
        return (
            result.smoke_id == record.smoke_id
            and result.spec_hash == record.spec_hash
            and result.source == record.source
            and result.session_id == record.session_id
            and result.target_profile == record.target_profile
            and result.target_identity == record.target_identity
            and result.outcome == record.outcome
            and result.finished_at == record.finished_at
            and result.assertions == record.assertions
            and result.cleanup == record.cleanup
            and result.primary_failure == record.primary_failure
            and result.harness_failure == record.harness_failure
            and result.external_verdict == record.external_verdict
        )

    def finish(self, smoke_id: str, updates: Mapping[str, object], result: SmokeResult) -> SmokeRunRecord:
        """Атомарно подготовить неизменяемый результат и зафиксировать завершённое состояние."""

        with self._locked():
            current = self._load_unlocked(smoke_id)
            if current.state is SmokeState.FINISHED:
                raise SmokeStoreError("DEV_SMOKE_STATE_IMMUTABLE", "завершённый SmokeRun нельзя изменять")
            updated = self._updated_unlocked(current, updates)
            if updated.state is not SmokeState.FINISHED or not self._result_matches_record(result, updated):
                raise SmokeStoreError("DEV_SMOKE_RESULT_MISMATCH", "SmokeResult не соответствует финальному SmokeRun")
            result_path = self._file(smoke_id, "result.json")
            result_payload = _safe_model_json(result)
            if os.path.lexists(result_path):
                existing = self._read_json(result_path, SMOKE_MAX_RUN_BYTES)
                if existing != result_payload:
                    raise SmokeStoreError("DEV_SMOKE_RESULT_IMMUTABLE", "SmokeResult уже существует и неизменяем")
            else:
                # Result записывается первым: при сбое записи state запуск остаётся незавершённым
                # и может быть безопасно восстановлен, но не выдаётся за PASS.
                self._write_json(result_path, result_payload)
            self._write_json(self._file(smoke_id, "state.json"), _safe_model_json(updated))
            return updated

    def save_result(self, result: SmokeResult) -> None:
        with self._locked():
            record = self._load_unlocked(result.smoke_id)
            if record.state is not SmokeState.FINISHED:
                raise SmokeStoreError("DEV_SMOKE_RESULT_STATE_INVALID", "результат разрешён только для завершённого SmokeRun")
            if not self._result_matches_record(result, record):
                raise SmokeStoreError("DEV_SMOKE_RESULT_MISMATCH", "SmokeResult не соответствует замороженному SmokeRun")
            path = self._file(result.smoke_id, "result.json")
            if os.path.lexists(path):
                existing = self._read_json(path, SMOKE_MAX_RUN_BYTES)
                if existing != _safe_model_json(result):
                    raise SmokeStoreError("DEV_SMOKE_RESULT_IMMUTABLE", "SmokeResult уже существует и неизменяем")
                return
            self._write_json(path, _safe_model_json(result))

    def load_result(self, smoke_id: str) -> SmokeResult | None:
        with self._locked():
            path = self._file(smoke_id, "result.json")
            if not os.path.lexists(path):
                return None
            record = self._load_unlocked(smoke_id)
            raw = self._read_json(path, SMOKE_MAX_RUN_BYTES)
            try:
                result = _validate_json_model(SmokeResult, raw)
            except ValidationError as exc:
                raise SmokeStoreError("DEV_SMOKE_RESULT_CORRUPT", "SmokeResult имеет некорректную схему") from exc
            if not isinstance(result, SmokeResult):
                raise SmokeStoreError("DEV_SMOKE_RESULT_CORRUPT", "SmokeResult имеет некорректный тип")
            if not self._result_matches_record(result, record):
                raise SmokeStoreError("DEV_SMOKE_RESULT_MISMATCH", "SmokeResult не соответствует замороженному SmokeRun")
            return result

    def request_cancel(self, smoke_id: str, timestamp: str) -> SmokeControl:
        with self._locked():
            self._load_unlocked(smoke_id)
            control_path = self._file(smoke_id, "control.json")
            if os.path.lexists(control_path):
                raw = self._read_json(control_path, 32 * 1024)
                try:
                    control = _validate_json_model(SmokeControl, raw)
                except ValidationError as exc:
                    raise SmokeStoreError("DEV_SMOKE_CONTROL_CORRUPT", "SmokeRun cancel control повреждён") from exc
            else:
                control = SmokeControl()
            if not control.cancel_requested:
                control = SmokeControl(cancel_requested=True, requested_at=timestamp)
                self._write_json(control_path, _safe_model_json(control))
            return control

    def is_cancel_requested(self, smoke_id: str) -> bool:
        with self._locked():
            path = self._file(smoke_id, "control.json")
            if not os.path.lexists(path):
                return False
            raw = self._read_json(path, 32 * 1024)
            try:
                return _validate_json_model(SmokeControl, raw).cancel_requested
            except ValidationError as exc:
                raise SmokeStoreError("DEV_SMOKE_CONTROL_CORRUPT", "SmokeRun cancel control повреждён") from exc

    def list_records(self) -> list[SmokeRunRecord]:
        with self._locked():
            if not os.path.lexists(self.root):
                return []
            if _is_reparse_point(self.root):
                raise SmokeStoreError("DEV_SMOKE_STATE_UNSAFE_PATH", "корень состояния SmokeRun не должен быть ссылкой")
            records: list[SmokeRunRecord] = []
            try:
                entries = list(os.scandir(self.root))
            except OSError as exc:
                raise SmokeStoreError("DEV_SMOKE_STATE_UNREADABLE", "корень SmokeRun невозможно прочитать") from exc
            if len(entries) > SMOKE_MAX_RUNS * 4:
                raise SmokeStoreError("DEV_SMOKE_RETENTION_INVALID", "корень SmokeRun содержит слишком много каталогов")
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                record = self._load_unlocked(entry.name)
                self._validate_finished_result_unlocked(record)
                records.append(record)
            return sorted(records, key=lambda item: item.created_at)

    def prune(self, *, active_ids: set[str] | None = None, now: datetime | None = None) -> int:
        active_ids = active_ids or set()
        now = now or self.now()
        removed = 0
        with self._locked():
            if not os.path.lexists(self.root):
                return 0
            records = self.list_records_unlocked()
            protected = {
                record.smoke_id
                for record in records
                if record.smoke_id in active_ids
                or record.state in {SmokeState.CREATED, SmokeState.PREPARING, SmokeState.RUNNING, SmokeState.EVALUATING, SmokeState.CLEANING_UP, SmokeState.AWAITING_EXTERNAL_EVALUATION}
            }
            cutoff = now.astimezone(UTC) - timedelta(seconds=SMOKE_MAX_RUN_AGE_SECONDS)
            candidates = []
            completed = [record for record in records if record.smoke_id not in protected]
            overflow = max(0, len(completed) - SMOKE_MAX_RUNS)
            for record in completed:
                try:
                    created = datetime.fromisoformat(record.created_at)
                except ValueError:
                    continue
                if created < cutoff or overflow > 0:
                    candidates.append(record)
                    if overflow > 0:
                        overflow -= 1
            for record in candidates:
                run_dir = self._run_dir(record.smoke_id)
                if _is_reparse_point(run_dir):
                    continue
                try:
                    for child in run_dir.iterdir():
                        if _is_reparse_point(child) or child.is_dir():
                            continue
                        child.unlink(missing_ok=True)
                    run_dir.rmdir()
                except OSError as exc:
                    raise SmokeStoreError(
                        "DEV_SMOKE_RETENTION_CLEANUP_FAILED",
                        "Старый каталог SmokeRun невозможно безопасно удалить",
                    ) from exc
                removed += 1
        return removed

    def list_records_unlocked(self) -> list[SmokeRunRecord]:
        entries = list(os.scandir(self.root)) if os.path.lexists(self.root) else []
        if len(entries) > SMOKE_MAX_RUNS * 4:
            raise SmokeStoreError("DEV_SMOKE_RETENTION_INVALID", "корень SmokeRun содержит слишком много каталогов")
        records: list[SmokeRunRecord] = []
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                record = self._load_unlocked(entry.name)
                self._validate_finished_result_unlocked(record)
                records.append(record)
        return sorted(records, key=lambda item: item.created_at)

    def _validate_finished_result_unlocked(self, record: SmokeRunRecord) -> None:
        if record.state is not SmokeState.FINISHED:
            return
        raw = self._read_json(self._file(record.smoke_id, "result.json"), SMOKE_MAX_RUN_BYTES)
        try:
            result = _validate_json_model(SmokeResult, raw)
        except ValidationError as exc:
            raise SmokeStoreError("DEV_SMOKE_RESULT_CORRUPT", "SmokeResult имеет некорректную схему") from exc
        if not isinstance(result, SmokeResult) or not self._result_matches_record(result, record):
            raise SmokeStoreError("DEV_SMOKE_RESULT_MISMATCH", "SmokeResult не соответствует завершённому SmokeRun")


class SmokeValidationIssue(_StrictModel):
    code: str
    message: str

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return _identifier(value, field_name="validation.code")

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _text(value, field_name="validation.message", maximum=SMOKE_MAX_RESULT_TEXT)


class SmokeRunManager:
    """Постоянная оркестрация SmokeRun через существующие API Dev Runtime."""

    def __init__(
        self,
        environment: DevEnvironment | None = None,
        *,
        runtime_factory: Callable[[], object] | None = None,
        supervisor_backend: SmokeSupervisorBackend | None = None,
        now: Callable[[], datetime] | None = None,
        poll_seconds: float = SMOKE_POLL_SECONDS,
        game_bridge: object | None = None,
        game_bridge_factory: Callable[[], object] | None = None,
    ) -> None:
        self.environment = environment or DevEnvironment.current()
        self.runtime_factory = runtime_factory or self._default_runtime_factory
        self.supervisor_backend = supervisor_backend or SmokeSupervisorBackend()
        self.now = now or (lambda: datetime.now(UTC))
        self.poll_seconds = min(max(float(poll_seconds), 0.01), 5.0)
        self.capabilities = SmokeCapabilityRegistry()
        self.store = SmokeStateStore(self.environment, now=self.now)
        self._registry: ConfigRegistry | None = None
        self._manager_lock = threading.RLock()
        self._game_bridge = game_bridge
        self._game_bridge_factory = game_bridge_factory

    def _default_runtime_factory(self) -> object:
        from module.dev_runtime.manager import DevSessionManager

        return DevSessionManager(
            environment=self.environment,
            target_locked=True,
            smoke_owner=True,
        )

    def _get_game_bridge(self) -> object:
        bridge = self._game_bridge
        if bridge is not None:
            return bridge
        if self._game_bridge_factory is not None:
            bridge = self._game_bridge_factory()
        else:
            from module.dev_runtime.game_bridge import build_runtime_game_bridge

            bridge = build_runtime_game_bridge(self.environment, clock=self.now)
        self._game_bridge = bridge
        return bridge

    def _bind_record_target(self, record: SmokeRunRecord) -> None:
        if record.target_profile is None or record.target_identity is None:
            raise SmokeStoreError(
                "DEV_SMOKE_TARGET_MISSING",
                "SmokeRun не содержит immutable target binding",
            )
        try:
            target = DevTarget(record.target_profile)
        except ValueError as exc:
            raise SmokeStoreError(
                "DEV_SMOKE_TARGET_INVALID",
                "SmokeRun содержит некорректный target profile",
            ) from exc
        if record.target_identity != target_identity(target):
            raise SmokeStoreError(
                "DEV_SMOKE_TARGET_MISMATCH",
                "SmokeRun target identity не соответствует target profile",
            )
        if self.environment.dev_target != target:
            self.environment = replace(self.environment, dev_target=target)
            self.store = SmokeStateStore(self.environment, now=self.now)
            self._registry = None
            self._game_bridge = None

    def _config_registry(self) -> ConfigRegistry:
        registry = self._registry
        if registry is None:
            with self._manager_lock:
                registry = self._registry
                if registry is None:
                    registry = ConfigRegistry(self.environment)
                    self._registry = registry
        return registry

    def _result(
        self,
        *,
        ok: bool,
        code: str,
        message: str,
        state: str,
        smoke_id: str | None = None,
        session_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> DevResult:
        payload = dict(details or {})
        if smoke_id is not None:
            payload.setdefault("smoke_id", smoke_id)
        return DevResult(ok, code, message, state, session_id, payload)

    def list_capabilities(self) -> DevResult:
        return self._result(
            ok=True,
            code="DEV_SMOKE_CAPABILITIES_READY",
            message="Реестр возможностей Smoke готов",
            state=SmokeState.CREATED.value,
            details={"capabilities": [_safe_model_json(item) for item in self.capabilities.descriptors()]},
        )

    @staticmethod
    def _spec_from_input(spec: object) -> SmokeSpec:
        if isinstance(spec, SmokeSpec):
            return SmokeSpec.model_validate(spec.model_dump(mode="python"), strict=True)
        return SmokeSpec.model_validate(spec, strict=True)

    def _validation_details(
        self,
        spec: SmokeSpec | None,
        source: SmokeSourceSnapshot | None,
        issues: Sequence[SmokeValidationIssue],
    ) -> dict[str, object]:
        details: dict[str, object] = {
            "valid": not issues,
            "issues": [_safe_model_json(item) for item in issues],
        }
        if spec is not None:
            details["spec_hash"] = spec.spec_hash()
            details["scope"] = {
                "root_tasks": list(spec.session.root_tasks),
                "excluded_tasks": list(spec.session.excluded_tasks),
                "config_override_count": len(spec.setup.config_overrides),
                "assertion_count": len(spec.assertions),
                "visual_assertion_count": len(spec.visual_assertions),
                "game_observation_count": (
                    len(spec.game_observations.observations)
                    if spec.game_observations is not None
                    else 0
                ),
                "game_checkpoint_count": (
                    len(spec.game_observations.checkpoints)
                    if spec.game_observations is not None
                    else 0
                ),
            }
        if source is not None:
            details["source"] = _safe_model_json(source)
        return details

    def _validate_spec_and_preconditions(
        self,
        raw_spec: object,
        *,
        check_runtime_conflict: bool,
    ) -> tuple[SmokeSpec | None, SmokeSourceSnapshot | None, list[SmokeValidationIssue]]:
        issues: list[SmokeValidationIssue] = []
        try:
            spec = self._spec_from_input(raw_spec)
        except (TypeError, ValueError, ValidationError):
            return None, None, [SmokeValidationIssue(code="DEV_SMOKE_SPEC_INVALID", message="SmokeSpec не прошёл строгую проверку")]
        try:
            self.capabilities.validate_spec(spec)
            if spec.game_observations is not None:
                bridge = self._get_game_bridge()
                requests = [
                    *spec.game_observations.observations,
                    *(
                        request
                        for checkpoint in spec.game_observations.checkpoints
                        for request in checkpoint.observations
                    ),
                ]
                for request in requests:
                    bridge.validate_request(
                        request.capability_id,
                        request.parameters,
                    )
            if len(spec.visual_assertions) > 1:
                issues.append(
                    SmokeValidationIssue(
                        code="DEV_SMOKE_MULTIPLE_VISUAL_UNSUPPORTED",
                        message="В одном SmokeRun поддерживается только одно внешнее визуальное утверждение",
                    )
                )
            registry = self._config_registry()
            if spec.setup.config_overrides:
                payload = read_profile_payload(self.environment.profile_file, repository_root=self.environment.repository_root)
                registry.validate_overrides(spec.setup.config_overrides, payload)
            for assertion in spec.assertions:
                if isinstance(assertion, (ConfigValueAssertion, ConfigRestoredAssertion)):
                    registry.leaf(assertion.path)
        except SmokeStoreError as exc:
            issues.append(SmokeValidationIssue(code=exc.code, message=str(exc)))
        except GameObservationError as exc:
            issues.append(SmokeValidationIssue(code=exc.code, message=str(exc)))
        except TaskSandboxError as exc:
            issues.append(SmokeValidationIssue(code=exc.code, message=str(exc)))
        except (OSError, ValueError):
            issues.append(
                SmokeValidationIssue(
                    code="DEV_SMOKE_PRECONDITION_FAILED",
                    message="Development target невозможно безопасно проверить",
                )
            )

        source = _source_snapshot(capture_git_snapshot(self.environment.repository_root))
        if not source.available:
            issues.append(SmokeValidationIssue(code="DEV_SMOKE_SOURCE_UNAVAILABLE", message="HEAD, ветка или состояние Git недоступны"))
        elif source.dirty is not False or source.changed_paths:
            issues.append(SmokeValidationIssue(code="DEV_SMOKE_SOURCE_DIRTY", message="Для доказательного smoke требуется чистое отслеживаемое дерево и индекс"))

        try:
            runtime = self.runtime_factory()
            planned = runtime.plan(
                root_tasks=list(spec.session.root_tasks),
                excluded_tasks=list(spec.session.excluded_tasks),
            )
            if not _result_ok(planned):
                issues.append(
                    SmokeValidationIssue(
                        code=str(getattr(planned, "code", "DEV_SMOKE_TASK_PLAN_FAILED")),
                        message="Область задач не прошла существующую проверку политики Task Sandbox",
                    )
                )
            if check_runtime_conflict:
                status = runtime.status()
                runtime_state = _runtime_state(_result_state(status))
                if runtime_state in {"starting", "running"}:
                    issues.append(SmokeValidationIssue(code="DEV_SMOKE_RUNTIME_ACTIVE", message="Уже существует активная DevSession"))
                elif _result_state(status) in {DevStatusKind.STALE.value, DevStatusKind.OWNERSHIP_MISMATCH.value}:
                    issues.append(SmokeValidationIssue(code="DEV_SMOKE_RUNTIME_STALE", message="Сначала требуется явное безопасное восстановление DevSession"))
        except Exception as exc:  # noqa: BLE001 — граница предварительной проверки скрывает детали реализации
            issues.append(SmokeValidationIssue(code="DEV_SMOKE_RUNTIME_UNAVAILABLE", message=f"Предварительные условия Dev Runtime недоступны: {type(exc).__name__}"))
        return spec, source, issues

    def validate_smoke(self, spec: object) -> DevResult:
        parsed, source, issues = self._validate_spec_and_preconditions(spec, check_runtime_conflict=True)
        if parsed is None:
            return self._result(
                ok=False,
                code="DEV_SMOKE_VALIDATION_FAILED",
                message="SmokeSpec отклонён строгой проверкой",
                state=SmokeState.FINISHED.value,
                details=self._validation_details(parsed, source, issues),
            )
        return self._result(
            ok=not issues,
            code="DEV_SMOKE_VALID" if not issues else "DEV_SMOKE_VALIDATION_FAILED",
            message="SmokeSpec прошёл предварительную проверку условий" if not issues else "SmokeSpec не прошёл предварительную проверку условий",
            state=SmokeState.CREATED.value if not issues else SmokeState.FINISHED.value,
            details=self._validation_details(parsed, source, issues),
        )

    def _active_record(self) -> SmokeRunRecord | None:
        records = self.store.list_records()
        active_states = {
            SmokeState.CREATED,
            SmokeState.PREPARING,
            SmokeState.RUNNING,
            SmokeState.EVALUATING,
            SmokeState.CLEANING_UP,
            SmokeState.AWAITING_EXTERNAL_EVALUATION,
        }
        for record in reversed(records):
            if record.state in active_states:
                return record
        return None

    def has_active_run(self) -> bool:
        """Проверить наличие активного SmokeRun без изменения его состояния."""

        return self._active_record() is not None

    def _control_reservation_active(self) -> bool:
        from module.dev_runtime.control import ControlStore

        store = ControlStore(self.environment)
        with store.lock(create=False):
            operation = store.read()
        return operation is not None and operation.active

    def _session_owner_issue(self) -> SmokeValidationIssue | None:
        """Проверить marker DevSession без повторной полной диагностики runtime."""

        try:
            session = _read_session(self.environment)
        except TaskSandboxError as exc:
            return SmokeValidationIssue(code=exc.code, message=str(exc))
        except (OSError, ValueError) as exc:
            return SmokeValidationIssue(
                code="DEV_SMOKE_RUNTIME_UNAVAILABLE",
                message=f"Состояние DevSession невозможно безопасно проверить: {type(exc).__name__}",
            )
        if session is None:
            return None
        if session.state in {
            DevSessionState.CREATED,
            DevSessionState.STARTING,
            DevSessionState.RUNNING,
            DevSessionState.STOPPING,
            DevSessionState.STALE,
        } or (session.state in {DevSessionState.FAILED, DevSessionState.STOPPED} and session.process is not None):
            code = (
                "DEV_SMOKE_RUNTIME_ACTIVE"
                if session.state in {DevSessionState.STARTING, DevSessionState.RUNNING, DevSessionState.STOPPING}
                else "DEV_SMOKE_RUNTIME_STALE"
            )
            message = (
                "Уже существует активная DevSession"
                if code == "DEV_SMOKE_RUNTIME_ACTIVE"
                else "Сначала требуется явное безопасное восстановление DevSession"
            )
            return SmokeValidationIssue(code=code, message=message)
        return None

    def start_smoke(self, spec: object) -> DevResult:
        parsed, source, issues = self._validate_spec_and_preconditions(spec, check_runtime_conflict=True)
        if parsed is None or source is None or issues:
            return self._result(
                ok=False,
                code="DEV_SMOKE_PRECONDITION_FAILED",
                message="SmokeRun не создан: предварительная проверка условий не пройдена",
                state=SmokeState.FINISHED.value,
                details=self._validation_details(parsed, source, issues),
            )
        try:
            with runtime_coordination_lock(self.environment):
                self.store.prune(now=self.now())
                active = self._active_record()
                if active is not None:
                    return self._result(
                        ok=False,
                        code="DEV_SMOKE_ACTIVE_CONFLICT",
                        message="Новый SmokeRun запрещён, пока предыдущий запуск не завершён или не отменён явно",
                        state=active.state.value,
                        smoke_id=active.smoke_id,
                        session_id=active.session_id,
                        details={"conflict_state": active.state.value},
                    )
                try:
                    control_active = self._control_reservation_active()
                except Exception as exc:  # noqa: BLE001 — неизвестное состояние owner блокирует запуск
                    return self._result(
                        ok=False,
                        code="DEV_RUNTIME_OWNER_STATE_UNAVAILABLE",
                        message=f"Нельзя подтвердить отсутствие control operation: {type(exc).__name__}",
                        state=SmokeState.FINISHED.value,
                    )
                if control_active:
                    return self._result(
                        ok=False,
                        code="DEV_SMOKE_CONTROL_CONFLICT",
                        message="SmokeRun запрещён при активной control operation",
                        state=SmokeState.FINISHED.value,
                        details={"outcome": "CONFLICT"},
                    )
                session_issue = self._session_owner_issue()
                if session_issue is not None:
                    return self._result(
                        ok=False,
                        code="DEV_SMOKE_PRECONDITION_FAILED",
                        message="SmokeRun не создан: предварительная проверка условий не пройдена",
                        state=SmokeState.FINISHED.value,
                        details={"issues": [_safe_model_json(session_issue)]},
                    )
                created_at = _timestamp_now(self.now)
                deadline_at = _add_seconds(created_at, float(parsed.timeout_seconds))
                record = self.store.create(
                    parsed,
                    source,
                    created_at=created_at,
                    deadline_at=deadline_at,
                )
                record = self.store.update(
                    record.smoke_id,
                    {"state": SmokeState.PREPARING, "started_at": created_at},
                )
        except RuntimeCoordinationError as exc:
            return self._result(
                ok=False,
                code=exc.code,
                message=str(exc),
                state=SmokeState.FINISHED.value,
            )
        except SmokeStoreError as exc:
            return self._result(ok=False, code=exc.code, message=str(exc), state=SmokeState.FINISHED.value)
        supervisor: SmokeSupervisorIdentity | None = None
        try:
            supervisor = self.supervisor_backend.launch(self.environment, record.smoke_id)
            record = self.store.update(record.smoke_id, {"supervisor": supervisor})
        except (SmokeStoreError, OSError, RuntimeError) as exc:
            supervisor_stopped = True
            if supervisor is not None:
                stop_supervisor = getattr(self.supervisor_backend, "stop", None)
                supervisor_stopped = bool(
                    stop_supervisor(self.environment, record.smoke_id, supervisor)
                    if callable(stop_supervisor)
                    else False
                )
            if "record" in locals():
                self._finalize_without_runtime(
                    record,
                    outcome=SmokeOutcome.HARNESS_FAILED,
                    code="DEV_SMOKE_SUPERVISOR_START_FAILED",
                    message=f"Supervisor не запущен: {type(exc).__name__}",
                    harness_failure=SmokeFailure(code="DEV_SMOKE_SUPERVISOR_START_FAILED", message="Независимый supervisor не запущен"),
                    no_owned_orphan=supervisor_stopped,
                )
            if isinstance(exc, SmokeStoreError) and exc.code == "DEV_SMOKE_ACTIVE_CONFLICT":
                return self._result(
                    ok=False,
                    code=exc.code,
                    message=str(exc),
                    state=SmokeState.FINISHED.value,
                )
            return self._result(
                ok=False,
                code="DEV_SMOKE_SUPERVISOR_START_FAILED",
                message="SmokeRun не смог запустить независимый supervisor",
                state=SmokeState.FINISHED.value,
                smoke_id=locals().get("record").smoke_id if "record" in locals() else None,
            )
        return self._result(
            ok=True,
            code="DEV_SMOKE_STARTED",
            message="SmokeRun создан; длительная работа выполняется независимым supervisor",
            state=record.state.value,
            smoke_id=record.smoke_id,
            details={
                "spec_hash": record.spec_hash,
                "deadline_at": record.deadline_at,
                "source": _safe_model_json(record.source),
                "progress": _safe_model_json(record.progress),
            },
        )

    def get_smoke(self, smoke_id: str) -> DevResult:
        try:
            smoke_id = _identifier(smoke_id, field_name="smoke_id")
            record = self.store.load(smoke_id)
            self._bind_record_target(record)
        except (SmokeStoreError, ValueError) as exc:
            code = exc.code if isinstance(exc, SmokeStoreError) else "DEV_SMOKE_ID_INVALID"
            return self._result(ok=False, code=code, message=str(exc), state=SmokeState.FINISHED.value, smoke_id=smoke_id if isinstance(smoke_id, str) else None)
        if record.state in {SmokeState.CREATED, SmokeState.PREPARING, SmokeState.RUNNING, SmokeState.EVALUATING, SmokeState.CLEANING_UP}:
            if record.supervisor is None:
                record = self._recover_crashed(record, "DEV_SMOKE_SUPERVISOR_IDENTITY_MISSING")
            else:
                alive = self.supervisor_backend.matches(self.environment, record.smoke_id, record.supervisor)
                if alive is not True:
                    record = self._recover_crashed(record, "DEV_SMOKE_SUPERVISOR_EXITED")
        try:
            result = self.store.load_result(record.smoke_id) if record.state is SmokeState.FINISHED else None
        except SmokeStoreError as exc:
            return self._result(
                ok=False,
                code=exc.code,
                message=str(exc),
                state=record.state.value,
                smoke_id=record.smoke_id,
                session_id=record.session_id,
                details=self._record_details(record, result=None),
            )
        if record.state is SmokeState.FINISHED and result is None:
            return self._result(
                ok=False,
                code="DEV_SMOKE_RESULT_MISSING",
                message="Для завершённого SmokeRun отсутствует неизменяемый SmokeResult",
                state=record.state.value,
                smoke_id=record.smoke_id,
                session_id=record.session_id,
                details=self._record_details(record, result=None),
            )
        details = self._record_details(record, result=result)
        return self._result(
            ok=True,
            code="DEV_SMOKE_READY" if result is None else "DEV_SMOKE_RESULT_READY",
            message="Состояние SmokeRun прочитано" if result is None else "Неизменяемый SmokeResult прочитан",
            state=record.state.value,
            smoke_id=record.smoke_id,
            session_id=record.session_id,
            details=details,
        )

    def cancel_smoke(self, smoke_id: str) -> DevResult:
        try:
            smoke_id = _identifier(smoke_id, field_name="smoke_id")
            record = self.store.load(smoke_id)
            self._bind_record_target(record)
        except (SmokeStoreError, ValueError) as exc:
            return self._result(ok=False, code=exc.code if isinstance(exc, SmokeStoreError) else "DEV_SMOKE_ID_INVALID", message=str(exc), state=SmokeState.FINISHED.value)
        if record.state is SmokeState.FINISHED:
            return self._result(ok=True, code="DEV_SMOKE_ALREADY_FINISHED", message="SmokeRun уже завершён", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id)
        if record.state is SmokeState.AWAITING_EXTERNAL_EVALUATION:
            record = self._finalize_cancelled(record)
            return self._result(ok=True, code="DEV_SMOKE_CANCELLED", message="Ожидающая внешняя оценка отменена", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id)
        try:
            self.store.request_cancel(smoke_id, _timestamp_now(self.now))
        except SmokeStoreError as exc:
            return self._result(ok=False, code=exc.code, message=str(exc), state=record.state.value, smoke_id=smoke_id)
        if record.supervisor is None or self.supervisor_backend.matches(self.environment, smoke_id, record.supervisor) is not True:
            record = self._recover_crashed(record, "DEV_SMOKE_CANCEL_RECOVERY")
            return self._result(ok=record.state is SmokeState.FINISHED, code="DEV_SMOKE_CANCELLED" if record.outcome is SmokeOutcome.CANCELLED else "DEV_SMOKE_CANCEL_RECOVERY_FAILED", message="Отмена обработана безопасным восстановлением" if record.outcome is SmokeOutcome.CANCELLED else "Отмена не смогла подтвердить очистку", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id)
        return self._result(ok=True, code="DEV_SMOKE_CANCEL_REQUESTED", message="Проверенный запрос отмены сохранён для supervisor", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id)

    def get_smoke_evaluation(self, smoke_id: str) -> EvidenceScreenshot:
        try:
            smoke_id = _identifier(smoke_id, field_name="smoke_id")
            record = self.store.load(smoke_id)
            self._bind_record_target(record)
        except (SmokeStoreError, ValueError) as exc:
            code = exc.code if isinstance(exc, SmokeStoreError) else "DEV_SMOKE_ID_INVALID"
            return EvidenceScreenshot(self._result(ok=False, code=code, message=str(exc), state=SmokeState.FINISHED.value))
        pending = record.pending_evaluation
        if record.state is not SmokeState.AWAITING_EXTERNAL_EVALUATION or pending is None:
            return EvidenceScreenshot(self._result(ok=False, code="DEV_SMOKE_EVALUATION_NOT_PENDING", message="Для SmokeRun нет ожидающей внешней оценки", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id))
        try:
            runtime = self.runtime_factory()
            screenshot = runtime.get_historical_screenshot(session_id=pending.session_id, screenshot_id=pending.screenshot_id)
        except Exception as exc:  # noqa: BLE001 — публичная граница не раскрывает детали реализации
            return EvidenceScreenshot(self._result(ok=False, code="DEV_SMOKE_EVALUATION_UNAVAILABLE", message=f"Сохранённый снимок экрана недоступен: {type(exc).__name__}", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id))
        if screenshot.image is None or not _result_ok(screenshot.result):
            return EvidenceScreenshot(self._result(ok=False, code="DEV_SMOKE_EVIDENCE_INCOMPLETE", message="Сохранённый снимок экрана не прошёл проверку Evidence API", state=SmokeState.AWAITING_EXTERNAL_EVALUATION.value, smoke_id=smoke_id, session_id=record.session_id))
        metadata = _result_details(screenshot.result).get("screenshot")
        if not isinstance(metadata, Mapping) or metadata.get("sha256") != pending.screenshot_sha256:
            return EvidenceScreenshot(self._result(ok=False, code="DEV_SMOKE_SCREENSHOT_HASH_MISMATCH", message="SHA снимка экрана не совпал с замороженной ссылкой оценки", state=SmokeState.AWAITING_EXTERNAL_EVALUATION.value, smoke_id=smoke_id, session_id=record.session_id))
        return EvidenceScreenshot(
            self._result(
                ok=True,
                code="DEV_SMOKE_EVALUATION_READY",
                message="Замороженные рубрика и точный сохранённый снимок экрана готовы для внешней оценки",
                state=SmokeState.AWAITING_EXTERNAL_EVALUATION.value,
                smoke_id=smoke_id,
                session_id=record.session_id,
                details={
                    "assertion_id": pending.assertion_id,
                    "rubric": pending.rubric,
                    "rubric_hash": pending.rubric_hash,
                    "spec_hash": pending.spec_hash,
                    "screenshot": dict(metadata),
                },
            ),
            screenshot.image,
            "image/png",
        )

    def submit_smoke_evaluation(self, smoke_id: str, assertion_id: str, verdict: str, rationale: str) -> DevResult:
        try:
            smoke_id = _identifier(smoke_id, field_name="smoke_id")
            assertion_id = _identifier(assertion_id, field_name="assertion_id")
            rationale = _text(rationale, field_name="rationale", maximum=SMOKE_MAX_RESULT_TEXT)
            if verdict not in {"pass", "fail"}:
                raise ValueError("verdict должен быть pass или fail")
            record = self.store.load(smoke_id)
            self._bind_record_target(record)
            spec = self.store.load_spec(smoke_id)
        except (SmokeStoreError, ValueError, ValidationError) as exc:
            code = exc.code if isinstance(exc, SmokeStoreError) else "DEV_SMOKE_EVALUATION_INPUT_INVALID"
            return self._result(ok=False, code=code, message=str(exc), state=SmokeState.FINISHED.value, smoke_id=smoke_id if isinstance(smoke_id, str) else None)
        pending = record.pending_evaluation
        visual = next((item for item in spec.visual_assertions if item.assertion_id == assertion_id), None)
        if record.state is not SmokeState.AWAITING_EXTERNAL_EVALUATION or pending is None or visual is None or pending.assertion_id != assertion_id:
            return self._result(ok=False, code="DEV_SMOKE_EVALUATION_NOT_PENDING", message="Внешняя оценка не соответствует замороженному ожидающему утверждению", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id)
        evaluation = self.get_smoke_evaluation(smoke_id)
        if evaluation.image is None or not _result_ok(evaluation.result):
            return self._result(ok=False, code="DEV_SMOKE_EVIDENCE_INCOMPLETE", message="Нельзя принять вердикт без проверенного точного снимка экрана", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id)
        metadata = _result_details(evaluation.result).get("screenshot")
        if not isinstance(metadata, Mapping) or metadata.get("sha256") != pending.screenshot_sha256:
            return self._result(ok=False, code="DEV_SMOKE_SCREENSHOT_HASH_MISMATCH", message="Несовпадение SHA снимка экрана отклонено", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id)
        external = SmokeExternalVerdict(
            source="mcp_client",
            assertion_id=assertion_id,
            screenshot_id=pending.screenshot_id,
            screenshot_sha256=pending.screenshot_sha256,
            spec_hash=record.spec_hash,
            rubric_hash=pending.rubric_hash,
            verdict=verdict,
            rationale=rationale,
            submitted_at=_timestamp_now(self.now),
        )
        visual_result = SmokeAssertionResult(
            assertion_id=assertion_id,
            capability_id="external_visual",
            required=visual.required,
            status=SmokeAssertionStatus.PASS if verdict == "pass" else SmokeAssertionStatus.FAIL,
            evidence_source="external_visual",
            evidence_refs=[
                SmokeEvidenceRef(source="external_visual", reference=f"screenshot:{pending.screenshot_id}", description="Точный сохранённый снимок экрана для внешней оценки"),
            ],
            message="Внешняя визуальная оценка пройдена" if verdict == "pass" else "Внешняя визуальная оценка отклонена",
        )
        outcome = SmokeOutcome.PASS if verdict == "pass" and record.cleanup.confirmed and record.cleanup.source_unchanged and record.cleanup.port_free and record.cleanup.no_owned_orphan else SmokeOutcome.PRODUCT_FAILED if verdict == "fail" else SmokeOutcome.HARNESS_FAILED
        code = "DEV_SMOKE_PASS" if outcome is SmokeOutcome.PASS else "DEV_SMOKE_VISUAL_FAILED" if outcome is SmokeOutcome.PRODUCT_FAILED else "DEV_SMOKE_CLEANUP_GATE_FAILED"
        finished_at = _timestamp_now(self.now)
        primary_failure = None if outcome is SmokeOutcome.PASS else SmokeFailure(code=code, message="Внешняя оценка или проверка очистки не дали PASS", assertion_id=assertion_id)
        harness_failure = None if outcome is not SmokeOutcome.HARNESS_FAILED else SmokeFailure(code=code, message="Проверки очистки и целостности source не подтверждены")
        updates = {
            "state": SmokeState.FINISHED,
            "outcome": outcome,
            "finished_at": finished_at,
            "pending_evaluation": None,
            "external_verdict": external,
            "assertions": [item for item in record.assertions if item.assertion_id != assertion_id] + [visual_result],
            "primary_failure": primary_failure,
            "harness_failure": harness_failure,
        }
        candidate = _validate_json_model(SmokeRunRecord, _safe_model_json(record.model_copy(update=updates)))
        if not isinstance(candidate, SmokeRunRecord):
            return self._result(ok=False, code="DEV_SMOKE_STATE_INVALID", message="Финальный SmokeRun имеет неверный тип", state=record.state.value, smoke_id=smoke_id, session_id=record.session_id)
        result = SmokeResult(
            smoke_id=candidate.smoke_id,
            spec_hash=candidate.spec_hash,
            outcome=outcome,
            code=code,
            message="SmokeRun завершён после внешнего визуального вердикта",
            source=candidate.source,
            session_id=candidate.session_id,
            target_profile=candidate.target_profile,
            target_identity=candidate.target_identity,
            assertions=candidate.assertions,
            cleanup=candidate.cleanup,
            primary_failure=primary_failure,
            harness_failure=harness_failure,
            external_verdict=external,
            finished_at=finished_at,
        )
        try:
            self.store.finish(smoke_id, updates, result)
        except SmokeStoreError as exc:
            return self._result(ok=False, code=exc.code, message=str(exc), state=record.state.value, smoke_id=smoke_id, session_id=record.session_id)
        return self._result(ok=outcome is SmokeOutcome.PASS, code=code, message=result.message, state=candidate.state.value, smoke_id=smoke_id, session_id=candidate.session_id, details={"outcome": outcome.value, "external_verdict": _safe_model_json(external), "cleanup": _safe_model_json(candidate.cleanup)})

    @staticmethod
    def _game_requests(
        spec: SmokeSpec,
        checkpoint_id: str,
    ) -> tuple[SmokeGameObservationRequest, ...]:
        game_spec = spec.game_observations
        if game_spec is None:
            return ()
        if checkpoint_id in {"before", "final"}:
            return tuple(game_spec.observations)
        checkpoint = next(
            (
                item
                for item in game_spec.checkpoints
                if item.checkpoint_id == checkpoint_id
            ),
            None,
        )
        if checkpoint is None:
            raise GameObservationError(
                "DEV_GAME_CHECKPOINT_UNKNOWN",
                "Запрошенный game checkpoint не объявлен в SmokeSpec",
            )
        return tuple(checkpoint.observations)

    @staticmethod
    def _game_evidence_refs(
        store: GameObservationStore,
        observations: Sequence[GameObservationSnapshot],
    ) -> list[dict[str, object]]:
        return [
            _safe_model_json(
                SmokeEvidenceRef(
                    source="game_observation",
                    reference=f"{store.relative_file}#{snapshot.observation_id}",
                    description=(
                        "Сохранённое game observation "
                        f"{snapshot.checkpoint_id}/{snapshot.capability_id}"
                    ),
                )
            )
            for snapshot in observations[:SMOKE_MAX_EVIDENCE_REFS]
        ]

    def _capture_game_checkpoint(
        self,
        record: SmokeRunRecord,
        spec: SmokeSpec,
        checkpoint_id: str,
        session_id: str | None,
        *,
        validated_requests: Sequence[SmokeGameObservationRequest] | None = None,
    ) -> tuple[bool, dict[str, object], SmokeFailure | None]:
        if spec.game_observations is None:
            return True, {}, None
        if session_id is None:
            failure = SmokeFailure(
                code="DEV_GAME_CHECKPOINT_SESSION_MISSING",
                message="Game checkpoint нельзя связать с DevSession",
            )
            return False, {"game_observations": {"checkpoint_id": checkpoint_id}}, failure
        try:
            requests = (
                tuple(validated_requests)
                if validated_requests is not None
                else self._game_requests(spec, checkpoint_id)
            )
            bridge = self._get_game_bridge()
            store = GameObservationStore(self.environment, record.smoke_id)
            stored = 0
            statuses: list[str] = []
            for request in requests:
                snapshot = bridge.capture(
                    self.environment.dev_target,
                    request.capability_id,
                    request.parameters,
                    checkpoint_id=checkpoint_id,
                    session_id=session_id,
                    smoke_id=record.smoke_id,
                    captured_at=self.now(),
                )
                if not isinstance(snapshot, GameObservationSnapshot):
                    raise GameObservationError(
                        "DEV_GAME_OBSERVATION_PROVIDER_INVALID",
                        "Bridge вернул некорректный game snapshot",
                    )
                expected_target = target_identity(self.environment.dev_target)
                if (
                    snapshot.smoke_id != record.smoke_id
                    or snapshot.session_id != session_id
                    or snapshot.profile_name != self.environment.profile_name
                    or snapshot.target_identity != expected_target
                ):
                    raise GameObservationError(
                        "DEV_GAME_OBSERVATION_TARGET_MISMATCH",
                        "Bridge вернул observation с другой session или target",
                    )
                if snapshot.checkpoint_id != checkpoint_id or snapshot.capability_id != request.capability_id:
                    raise GameObservationError(
                        "DEV_GAME_OBSERVATION_PROVIDER_INVALID",
                        "Bridge вернул observation с другой checkpoint или capability",
                    )
                appended = store.append(
                    snapshot,
                    duplicate_policy=spec.game_observations.duplicate_policy,
                )
                if appended:
                    stored += 1
                    statuses.append(snapshot.status.value)
                else:
                    retained = next(
                        (
                            item
                            for item in store.read(checkpoint_id=checkpoint_id)
                            if item.capability_id == request.capability_id
                        ),
                        None,
                    )
                    if retained is None:
                        raise GameObservationError(
                            "DEV_GAME_OBSERVATION_CORRUPT",
                            "После keep_first не найдено сохранённое observation",
                        )
                    statuses.append(retained.status.value)
            summary = store.summary()
            summary["evidence_refs"] = self._game_evidence_refs(
                store,
                store.read(checkpoint_id=checkpoint_id),
            )
            summary.update(
                {
                    "checkpoint_id": checkpoint_id,
                    "requested": len(requests),
                    "stored": stored,
                    "statuses": statuses,
                }
            )
            unavailable = next(
                (
                    status
                    for status in statuses
                    if status in {"unknown", "unavailable"}
                ),
                None,
            )
            if unavailable is not None:
                code = (
                    "DEV_GAME_OBSERVATION_UNKNOWN"
                    if unavailable == "unknown"
                    else "DEV_GAME_OBSERVATION_UNAVAILABLE"
                )
                return (
                    False,
                    {"game_observations": summary},
                    SmokeFailure(
                        code=code,
                        message="Обязательное game observation не имеет подтверждённого состояния known",
                    ),
                )
            return True, {"game_observations": summary}, None
        except GameObservationError as exc:
            return (
                False,
                {
                    "game_observations": {
                        "checkpoint_id": checkpoint_id,
                        "status": "unavailable",
                    }
                },
                SmokeFailure(code=exc.code, message=str(exc)),
            )
        except Exception as exc:
            return (
                False,
                {
                    "game_observations": {
                        "checkpoint_id": checkpoint_id,
                        "status": "unavailable",
                    }
                },
                SmokeFailure(
                    code="DEV_GAME_OBSERVATION_UNAVAILABLE",
                    message=f"Game checkpoint недоступен: {type(exc).__name__}",
                ),
            )

    def _game_required_complete(
        self,
        record: SmokeRunRecord,
        spec: SmokeSpec,
    ) -> bool:
        if spec.game_observations is None:
            return True
        try:
            items = GameObservationStore(self.environment, record.smoke_id).read()
        except GameObservationError:
            return False
        expected = {
            (checkpoint_id, request.capability_id)
            for checkpoint_id in ("before", "final")
            for request in spec.game_observations.observations
        }
        expected.update(
            (checkpoint.checkpoint_id, request.capability_id)
            for checkpoint in spec.game_observations.checkpoints
            for request in checkpoint.observations
        )
        if record.target_profile is None or record.target_identity is None:
            return False
        target_profile = record.target_profile
        target_id = record.target_identity
        actual = {
            (item.checkpoint_id, item.capability_id)
            for item in items
            if item.profile_name == target_profile
            and item.target_identity == target_id
            and item.session_id == record.session_id
            and item.status.value == "known"
        }
        return expected.issubset(actual)

    def capture_game_checkpoint(self, smoke_id: str, checkpoint_id: str) -> DevResult:
        try:
            smoke_id = _identifier(smoke_id, field_name="smoke_id")
            checkpoint_id = _identifier(checkpoint_id, field_name="checkpoint_id")
            if checkpoint_id in {"before", "final"}:
                raise GameObservationError(
                    "DEV_GAME_CHECKPOINT_RESERVED",
                    "before/final checkpoint создаются supervisor автоматически",
                )
            record = self.store.load(smoke_id)
            self._bind_record_target(record)
            spec = self.store.load_spec(smoke_id)
            if record.state is not SmokeState.RUNNING:
                return self._result(
                    ok=False,
                    code="DEV_GAME_CHECKPOINT_STATE_INVALID",
                    message="Промежуточный game checkpoint разрешён только для running SmokeRun",
                    state=record.state.value,
                    smoke_id=smoke_id,
                    session_id=record.session_id,
                )
            if spec.game_observations is None:
                return self._result(
                    ok=False,
                    code="DEV_GAME_OBSERVATION_NOT_DECLARED",
                    message="SmokeSpec не объявляет game observations",
                    state=record.state.value,
                    smoke_id=smoke_id,
                    session_id=record.session_id,
                )
            requests = self._game_requests(spec, checkpoint_id)
            ok, details, failure = self._capture_game_checkpoint(
                record,
                spec,
                checkpoint_id,
                record.session_id,
                validated_requests=requests,
            )
            return self._result(
                ok=ok,
                code="DEV_GAME_CHECKPOINT_CAPTURED" if ok else (failure.code if failure else "DEV_GAME_CHECKPOINT_FAILED"),
                message="Промежуточный game checkpoint сохранён" if ok else (failure.message if failure else "Game checkpoint не сохранён"),
                state=record.state.value,
                smoke_id=smoke_id,
                session_id=record.session_id,
                details=details,
            )
        except (SmokeStoreError, ValueError, GameObservationError) as exc:
            return self._result(
                ok=False,
                code=exc.code if isinstance(exc, (SmokeStoreError, GameObservationError)) else "DEV_GAME_CHECKPOINT_INPUT_INVALID",
                message=str(exc),
                state=SmokeState.FINISHED.value,
                smoke_id=smoke_id if isinstance(smoke_id, str) else None,
            )

    def get_game_observations(
        self,
        smoke_id: str,
        *,
        checkpoint_id: str | None = None,
    ) -> DevResult:
        try:
            smoke_id = _identifier(smoke_id, field_name="smoke_id")
            if checkpoint_id is not None:
                checkpoint_id = _identifier(checkpoint_id, field_name="checkpoint_id")
            record = self.store.load(smoke_id)
            self._bind_record_target(record)
            spec = self.store.load_spec(smoke_id)
            if spec.game_observations is None:
                return self._result(
                    ok=True,
                    code="DEV_GAME_OBSERVATIONS_EMPTY",
                    message="SmokeSpec не объявляет game observations",
                    state=record.state.value,
                    smoke_id=smoke_id,
                    session_id=record.session_id,
                    details={"observations": [], "required_complete": True},
                )
            if checkpoint_id is not None:
                self._game_requests(spec, checkpoint_id)
            store = GameObservationStore(self.environment, smoke_id)
            observations = store.read(checkpoint_id=checkpoint_id)
            if record.target_profile is None or record.target_identity is None:
                raise SmokeStoreError(
                    "DEV_SMOKE_TARGET_MISSING",
                    "SmokeRun не содержит immutable target binding",
                )
            target_profile = record.target_profile
            target_id = record.target_identity
            if any(
                item.profile_name != target_profile
                or item.target_identity != target_id
                or item.session_id != record.session_id
                for item in observations
            ):
                raise GameObservationError(
                    "DEV_GAME_OBSERVATION_TARGET_MISMATCH",
                    "Game observations относятся к другой session или target",
                )
            summary = store.summary()
            summary["evidence_refs"] = self._game_evidence_refs(store, observations)
            summary["required_complete"] = self._game_required_complete(record, spec)
            if checkpoint_id is not None:
                summary["checkpoint_id"] = checkpoint_id
                summary["selected_count"] = len(observations)
            return self._result(
                ok=True,
                code="DEV_GAME_OBSERVATIONS_READY",
                message="Сохранённые Smoke game observations прочитаны",
                state=record.state.value,
                smoke_id=smoke_id,
                session_id=record.session_id,
                details={
                    "observations": [item.as_dict() for item in observations],
                    "summary": summary,
                },
            )
        except (SmokeStoreError, ValueError, GameObservationError) as exc:
            return self._result(
                ok=False,
                code=exc.code if isinstance(exc, (SmokeStoreError, GameObservationError)) else "DEV_GAME_OBSERVATIONS_INPUT_INVALID",
                message=str(exc),
                state=SmokeState.FINISHED.value,
                smoke_id=smoke_id if isinstance(smoke_id, str) else None,
            )

    def run_supervisor(self, smoke_id: str) -> None:
        """Выполнить один замороженный SmokeRun; вызывается только фиксированной точкой входа supervisor."""

        try:
            self._run_supervisor(smoke_id)
        except Exception as exc:  # noqa: BLE001 — восстановление после сбоя завершается fail-closed
            try:
                record = self.store.load(smoke_id)
                if record.state is not SmokeState.FINISHED:
                    self._recover_crashed(record, f"DEV_SMOKE_SUPERVISOR_EXCEPTION_{type(exc).__name__.upper()[:40]}")
            except Exception:  # noqa: BLE001 — восстановление после сбоя работает fail-closed
                return

    def _run_supervisor(self, smoke_id: str) -> None:
        record = self.store.load(smoke_id)
        self._bind_record_target(record)
        if record.state is SmokeState.FINISHED or record.state is SmokeState.AWAITING_EXTERNAL_EVALUATION:
            return
        spec = self.store.load_spec(smoke_id)
        source_now = _source_snapshot(capture_git_snapshot(self.environment.repository_root))
        if not _same_source(record.source, source_now):
            self._finalize_without_runtime(record, outcome=SmokeOutcome.INVALIDATED, code="INVALIDATED_SOURCE_DRIFT", message="Снимок source изменился до запуска supervisor", harness_failure=SmokeFailure(code="INVALIDATED_SOURCE_DRIFT", message="Изменились HEAD или отслеживаемое дерево"))
            return
        runtime = self.runtime_factory()
        registry = self._config_registry()
        transaction = SmokeOverrideTransaction(
            self.environment,
            registry,
            spec.setup.config_overrides,
            save_state=lambda value: self.store.update(smoke_id, {"overrides": value}),
        )
        transaction.apply()
        record = self.store.load(smoke_id)
        started = runtime.start(root_tasks=list(spec.session.root_tasks), excluded_tasks=list(spec.session.excluded_tasks))
        if not _result_ok(started):
            failure = SmokeFailure(code=str(getattr(started, "code", "DEV_SMOKE_SESSION_START_FAILED")), message="DevSession не смогла запуститься")
            cleanup, cleanup_failure = self._cleanup_runtime(runtime, None, transaction, record)
            self._finish_record(record, SmokeOutcome.PRECONDITION_FAILED, failure.code, failure.message, cleanup, primary_failure=failure, harness_failure=cleanup_failure)
            return
        session_id = _result_session_id(started)
        if session_id is None:
            cleanup, cleanup_failure = self._cleanup_runtime(runtime, None, transaction, record)
            self._finish_record(record, SmokeOutcome.HARNESS_FAILED, "DEV_SMOKE_SESSION_ID_MISSING", "DevSession start не вернул session_id", cleanup, harness_failure=SmokeFailure(code="DEV_SMOKE_SESSION_ID_MISSING", message="Невозможно связать runtime и SmokeRun"), primary_failure=None)
            return
        record = self.store.update(smoke_id, {"state": SmokeState.RUNNING, "session_id": session_id})
        previous_results: list[SmokeAssertionResult] = []
        primary_failure: SmokeFailure | None = None
        primary_outcome: SmokeOutcome | None = None
        pending_visual = record.pending_evaluation
        before_ok, _before_details, before_failure = self._capture_game_checkpoint(
            record,
            spec,
            "before",
            session_id,
        )
        if not before_ok:
            primary_outcome = SmokeOutcome.EVIDENCE_INCOMPLETE
            primary_failure = before_failure or SmokeFailure(
                code="DEV_GAME_OBSERVATION_UNAVAILABLE",
                message="Game observations для before не подтверждены",
            )
        while True:
            if primary_failure is not None:
                break
            if self.store.is_cancel_requested(smoke_id):
                primary_outcome = SmokeOutcome.CANCELLED
                primary_failure = SmokeFailure(code="DEV_SMOKE_CANCELLED", message="Получен проверенный cancel request")
                break
            now_value = datetime.fromisoformat(_timestamp_now(self.now))
            deadline = datetime.fromisoformat(record.deadline_at)
            if now_value >= deadline:
                primary_outcome = SmokeOutcome.TIMEOUT
                primary_failure = SmokeFailure(code="DEV_SMOKE_TIMEOUT", message="SmokeRun достиг замороженного крайнего срока")
                break
            current_source = _source_snapshot(capture_git_snapshot(self.environment.repository_root))
            if not _same_source(record.source, current_source):
                primary_outcome = SmokeOutcome.INVALIDATED
                primary_failure = SmokeFailure(code="INVALIDATED_SOURCE_DRIFT", message="Снимок source изменился во время SmokeRun")
                break
            observed = self._observe(runtime, session_id, spec, transaction, completed=False)
            if not observed.evidence_ok:
                primary_outcome = SmokeOutcome.EVIDENCE_INCOMPLETE
                primary_failure = SmokeFailure(code="DEV_SMOKE_EVIDENCE_INCOMPLETE", message=observed.evidence_reason or "Данные Evidence API неполны")
                break
            isolation_failure = self._unexpected_task_failure(spec, observed.context)
            if isolation_failure is not None:
                primary_outcome = SmokeOutcome.HARNESS_FAILED
                primary_failure = isolation_failure
                break
            previous_results = self._evaluate_assertions(spec, observed.context, previous_results)
            record = self._save_progress(record, previous_results, observed.context)
            if self._has_unexpected_runtime_error(spec, observed.context):
                primary_outcome = SmokeOutcome.PRODUCT_FAILED
                primary_failure = SmokeFailure(code="DEV_SMOKE_UNEXPECTED_RUNTIME_ERROR", message="Необработанная структурированная ошибка выполнения исключает PASS")
                break
            if any(item.required and item.status in {SmokeAssertionStatus.FAIL, SmokeAssertionStatus.UNAVAILABLE} for item in previous_results):
                primary_outcome = SmokeOutcome.PRODUCT_FAILED
                failed = next(item for item in previous_results if item.required and item.status in {SmokeAssertionStatus.FAIL, SmokeAssertionStatus.UNAVAILABLE})
                primary_failure = SmokeFailure(code="DEV_SMOKE_ASSERTION_FAILED", message=failed.message, assertion_id=failed.assertion_id)
                break
            if pending_visual is None and spec.visual_assertions:
                visual = self._capture_visual_if_ready(runtime, session_id, spec, observed.context)
                if visual is not None:
                    pending_visual = visual
                    record = self.store.update(smoke_id, {"pending_evaluation": pending_visual})
            deterministic_done = all(
                not item.required or item.status is SmokeAssertionStatus.PASS
                for item in previous_results
            )
            if deterministic_done and (not spec.visual_assertions or pending_visual is not None):
                break
            now_value = datetime.fromisoformat(_timestamp_now(self.now))
            deadline = datetime.fromisoformat(record.deadline_at)
            if now_value >= deadline:
                primary_outcome = SmokeOutcome.TIMEOUT
                primary_failure = SmokeFailure(code="DEV_SMOKE_TIMEOUT", message="SmokeRun достиг замороженного крайнего срока")
                break
            time.sleep(min(self.poll_seconds, max(0.01, (deadline - now_value).total_seconds())))
        record = self.store.update(smoke_id, {"state": SmokeState.EVALUATING})
        final_game_ok, _final_game_details, final_game_failure = self._capture_game_checkpoint(
            record,
            spec,
            "final",
            session_id,
        )
        if not final_game_ok and primary_failure is None:
            primary_outcome = SmokeOutcome.EVIDENCE_INCOMPLETE
            primary_failure = final_game_failure or SmokeFailure(
                code="DEV_GAME_OBSERVATION_UNAVAILABLE",
                message="Game observations для final не подтверждены",
            )
        record = self.store.update(smoke_id, {"state": SmokeState.CLEANING_UP})
        cleanup, cleanup_failure = self._cleanup_runtime(runtime, session_id, transaction, record)
        record = self.store.update(smoke_id, {"cleanup": cleanup})
        final_observed = self._observe(runtime, session_id, spec, transaction, completed=True)
        if final_observed.evidence_ok:
            final_results = self._evaluate_assertions(spec, final_observed.context, previous_results, final=True)
        else:
            final_results = previous_results
        record = self._save_progress(record, final_results, final_observed.context if final_observed.evidence_ok else None)
        if not self._game_required_complete(record, spec) and primary_failure is None:
            primary_outcome = SmokeOutcome.EVIDENCE_INCOMPLETE
            primary_failure = SmokeFailure(
                code="DEV_SMOKE_GAME_EVIDENCE_INCOMPLETE",
                message="Обязательные game checkpoints не имеют полного known evidence",
            )
        if cleanup_failure is not None and primary_failure is None:
            primary_outcome = SmokeOutcome.HARNESS_FAILED
            primary_failure = SmokeFailure(code=cleanup_failure.code, message=str(cleanup_failure))
        if not final_observed.evidence_ok and primary_failure is None:
            primary_outcome = SmokeOutcome.EVIDENCE_INCOMPLETE
            primary_failure = SmokeFailure(code="DEV_SMOKE_EVIDENCE_INCOMPLETE", message=final_observed.evidence_reason or "Итоговые данные Evidence API неполны")
        if self._has_unexpected_runtime_error(spec, final_observed.context) and primary_failure is None:
            primary_outcome = SmokeOutcome.PRODUCT_FAILED
            primary_failure = SmokeFailure(code="DEV_SMOKE_UNEXPECTED_RUNTIME_ERROR", message="Необработанная структурированная ошибка выполнения исключает PASS")
        failed = next((item for item in final_results if item.required and item.status in {SmokeAssertionStatus.FAIL, SmokeAssertionStatus.UNAVAILABLE}), None)
        if failed is not None and primary_failure is None:
            primary_outcome = SmokeOutcome.PRODUCT_FAILED
            primary_failure = SmokeFailure(code="DEV_SMOKE_ASSERTION_FAILED", message=failed.message, assertion_id=failed.assertion_id)
        pending_required = next((item for item in final_results if item.required and item.status is not SmokeAssertionStatus.PASS), None)
        if pending_required is not None and primary_failure is None:
            primary_outcome = SmokeOutcome.EVIDENCE_INCOMPLETE
            primary_failure = SmokeFailure(code="DEV_SMOKE_ASSERTION_INCOMPLETE", message="Обязательное утверждение не достигло итогового PASS", assertion_id=pending_required.assertion_id)
        if cleanup.confirmed is False and primary_failure is None:
            primary_outcome = SmokeOutcome.HARNESS_FAILED
            primary_failure = SmokeFailure(code=cleanup.failure_code or "DEV_SMOKE_CLEANUP_GATE_FAILED", message="Проверка очистки не подтверждена")
        if pending_visual is not None and primary_failure is None and cleanup.confirmed:
            visual = next(item for item in spec.visual_assertions if item.assertion_id == pending_visual.assertion_id)
            visual_result = SmokeAssertionResult(
                assertion_id=visual.assertion_id,
                capability_id=visual.capability_id,
                required=visual.required,
                status=SmokeAssertionStatus.PENDING,
                evidence_source="external_visual",
                evidence_refs=[
                    SmokeEvidenceRef(
                        source="external_visual",
                        reference=f"screenshot:{pending_visual.screenshot_id}",
                        description="Точный сохранённый снимок экрана ожидает внешней оценки",
                    )
                ],
                message="Ожидается внешняя визуальная оценка",
            )
            pending_results = [*final_results, visual_result]
            record = self.store.update(
                smoke_id,
                {
                    "state": SmokeState.AWAITING_EXTERNAL_EVALUATION,
                    "assertions": [_safe_model_json(item) for item in pending_results],
                },
            )
            return
        outcome = primary_outcome or SmokeOutcome.PASS
        if outcome is SmokeOutcome.PASS and (
            not cleanup.confirmed
            or cleanup.port_free is not True
            or cleanup.source_unchanged is not True
            or cleanup.no_owned_orphan is not True
            or (final_observed.context.evidence_health != EVIDENCE_HEALTH_COMPLETE if final_observed.evidence_ok else True)
        ):
            outcome = SmokeOutcome.HARNESS_FAILED
            primary_failure = SmokeFailure(code="DEV_SMOKE_PASS_GATE_FAILED", message="Одна из обязательных проверок целостности Smoke не подтверждена")
        self._finish_record(record, outcome, _outcome_code(outcome), _outcome_message(outcome), cleanup, assertions=final_results, primary_failure=primary_failure, harness_failure=cleanup_failure)

    def _observe(self, runtime: object, session_id: str, spec: SmokeSpec, transaction: SmokeOverrideTransaction, *, completed: bool) -> _RuntimeObservation:
        try:
            evidence = runtime.get_evidence(session_id=session_id)
            if not _result_ok(evidence):
                return _RuntimeObservation(self._empty_context(session_id, completed), _source_snapshot(capture_git_snapshot(self.environment.repository_root)), False, "Evidence API вернул ошибку")
            summary = _result_details(evidence)
            health_payload = summary.get("evidence_health")
            health = health_payload.get("status") if isinstance(health_payload, Mapping) else EVIDENCE_HEALTH_UNAVAILABLE
            if not isinstance(health, str):
                health = EVIDENCE_HEALTH_UNAVAILABLE
            timeline = self._read_timeline(runtime, session_id)
            logs, log_available, log_truncated = self._read_logs(runtime, session_id)
            status = runtime.status()
            runtime_state = _runtime_state(_result_state(status))
            task_policy = _result_details(status).get("task_policy")
            task_policy_state = task_policy.get("state") if isinstance(task_policy, Mapping) and isinstance(task_policy.get("state"), str) else None
            current_task = summary.get("current_task") if isinstance(summary.get("current_task"), str) else None
            config_paths = self._observed_config_paths(spec)
            config_values = self._read_safe_config(config_paths)
            port_probe = getattr(runtime, "port_probe", None)
            port_listening = port_probe(self.environment.host, self.environment.port) if callable(port_probe) else None
            errors = self._structured_errors(summary, timeline)
            screenshots = summary.get("screenshots")
            metadata: tuple[Mapping[str, object], ...] = ()
            if isinstance(screenshots, Mapping) and isinstance(screenshots.get("latest"), Mapping):
                metadata = (MappingProxyType(dict(screenshots["latest"])),)
            started_at = summary.get("lifecycle", {}).get("started_at") if isinstance(summary.get("lifecycle"), Mapping) else None
            elapsed = 0.0
            if isinstance(started_at, str):
                elapsed = max(0.0, (datetime.fromisoformat(_timestamp_now(self.now)) - datetime.fromisoformat(started_at)).total_seconds())
            context = SmokeObservationContext(
                timeline=tuple(timeline),
                logs=tuple(logs),
                evidence_health=health,
                runtime_state=runtime_state,
                task_policy_state=task_policy_state,
                current_task=current_task,
                config_values=MappingProxyType(config_values),
                restored_paths=frozenset(item.path for item in transaction.snapshots) if transaction.restored else frozenset(),
                port_listening=port_listening,
                elapsed_seconds=elapsed,
                completed=completed,
                session_id=session_id,
                structured_errors=tuple(errors),
                screenshot_metadata=metadata,
                log_available=log_available,
                log_truncated=log_truncated,
            )
            return _RuntimeObservation(context, _source_snapshot(capture_git_snapshot(self.environment.repository_root)), health == EVIDENCE_HEALTH_COMPLETE, None if health == EVIDENCE_HEALTH_COMPLETE else f"evidence health={health}")
        except Exception as exc:  # noqa: BLE001 — ошибка наблюдения означает сбой Harness или evidence
            return _RuntimeObservation(self._empty_context(session_id, completed), _source_snapshot(capture_git_snapshot(self.environment.repository_root)), False, f"Наблюдение Evidence API завершилось ошибкой: {type(exc).__name__}")

    @staticmethod
    def _empty_context(session_id: str, completed: bool) -> SmokeObservationContext:
        return SmokeObservationContext(
            timeline=(), logs=(), evidence_health=EVIDENCE_HEALTH_UNAVAILABLE, runtime_state="failed", task_policy_state=None, current_task=None, config_values=MappingProxyType({}), restored_paths=frozenset(), port_listening=None, elapsed_seconds=0.0, completed=completed, session_id=session_id, structured_errors=(), screenshot_metadata=(), log_available=False, log_truncated=False,
        )

    def _read_timeline(self, runtime: object, session_id: str) -> list[TimelineObservation]:
        events: list[TimelineObservation] = []
        after = 0
        for _ in range(12):
            result = runtime.get_timeline(session_id=session_id, after_sequence=after, limit=200)
            if not _result_ok(result):
                raise SmokeStoreError("DEV_SMOKE_TIMELINE_UNAVAILABLE", "Timeline Evidence API вернул ошибку")
            details = _result_details(result)
            raw_events = details.get("events", [])
            if not isinstance(raw_events, list):
                raise SmokeStoreError("DEV_SMOKE_TIMELINE_CORRUPT", "Timeline Evidence API имеет некорректную структуру")
            for raw in raw_events:
                if not isinstance(raw, Mapping) or not isinstance(raw.get("sequence"), int) or not isinstance(raw.get("type"), str):
                    continue
                fields = raw.get("fields")
                safe_fields = {
                    str(key): value
                    for key, value in fields.items()
                    if isinstance(key, str) and isinstance(value, (str, bool, int, float))
                } if isinstance(fields, Mapping) else {}
                events.append(TimelineObservation(raw["sequence"], raw["type"], MappingProxyType(safe_fields)))
            more = details.get("more") is True
            next_after = details.get("next_after_sequence")
            if not more or not isinstance(next_after, int) or next_after <= after:
                break
            after = next_after
        return events[-SMOKE_MAX_TIMELINE_EVENTS:]

    def _read_logs(self, runtime: object, session_id: str) -> tuple[list[str], bool, bool]:
        lines: list[str] = []
        cursor: str | None = None
        available = False
        truncated = False
        for _ in range(8):
            result = runtime.get_logs(session_id=session_id, cursor=cursor, limit=200)
            if not _result_ok(result):
                return lines, False, True
            details = _result_details(result)
            health_details = details.get("health")
            if isinstance(health_details, Mapping):
                available = available or health_details.get("status") == EVIDENCE_HEALTH_COMPLETE
            truncated = truncated or details.get("truncated") is True
            items = details.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                        lines.append(item["text"][:SMOKE_MAX_LITERAL])
            next_cursor = details.get("next_cursor")
            if details.get("more") is not True or not isinstance(next_cursor, str) or next_cursor == cursor:
                break
            cursor = next_cursor
            if sum(len(line) for line in lines) >= SMOKE_MAX_LOG_BYTES:
                truncated = True
                break
        return lines, available, truncated

    @staticmethod
    def _structured_errors(summary: Mapping[str, object], timeline: Sequence[TimelineObservation]) -> list[StructuredErrorObservation]:
        errors: list[StructuredErrorObservation] = []
        last_error = summary.get("last_error")
        if isinstance(last_error, Mapping):
            errors.append(StructuredErrorObservation(last_error.get("type") if isinstance(last_error.get("type"), str) else None, None, str(last_error.get("message", "runtime error"))[:SMOKE_MAX_RESULT_TEXT], last_error.get("sequence") if isinstance(last_error.get("sequence"), int) else None))
        for event in timeline:
            if event.event_type == "runtime_error":
                error = StructuredErrorObservation(
                    event.fields.get("exception_type") if isinstance(event.fields.get("exception_type"), str) else None,
                    event.fields.get("code") if isinstance(event.fields.get("code"), str) else None,
                    str(event.fields.get("reason", "runtime error"))[:SMOKE_MAX_RESULT_TEXT],
                    event.sequence,
                )
                if not any(item.sequence == error.sequence for item in errors):
                    errors.append(error)
        return errors[:16]

    @staticmethod
    def _observed_config_paths(spec: SmokeSpec) -> set[str]:
        paths = {item.path for item in spec.setup.config_overrides}
        paths.update(item.path for item in spec.assertions if isinstance(item, (ConfigValueAssertion, ConfigRestoredAssertion)))
        return paths

    def _read_safe_config(self, paths: Iterable[str]) -> dict[str, ScalarValue]:
        if not paths:
            return {}
        payload = read_profile_payload(self.environment.profile_file, repository_root=self.environment.repository_root)
        registry = self._config_registry()
        values: dict[str, ScalarValue] = {}
        for path in paths:
            registry.leaf(path)
            marker = object()
            value = _deep_get(payload, path, marker)
            if value is not marker and (value is None or isinstance(value, (bool, int, float, str))):
                values[path] = value
        return values

    def _evaluate_assertions(self, spec: SmokeSpec, context: SmokeObservationContext, previous: Sequence[SmokeAssertionResult], *, final: bool = False) -> list[SmokeAssertionResult]:
        previous_by_id = {item.assertion_id: item for item in previous}
        latched = {"event_occurred", "task_started", "dependency_occurred", "expected_safe_error", "config_value", "dev_port_state", "runtime_state"}
        results: list[SmokeAssertionResult] = []
        for assertion in spec.assertions:
            old = previous_by_id.get(assertion.assertion_id)
            if old is not None and old.status is SmokeAssertionStatus.FAIL:
                results.append(old)
                continue
            if old is not None and old.status is SmokeAssertionStatus.PASS and getattr(assertion, "capability_id", None) in latched:
                results.append(old)
                continue
            evaluation = self.capabilities.evaluate(assertion, context)
            results.append(SmokeAssertionResult(assertion_id=assertion.assertion_id, capability_id=assertion.capability_id, required=assertion.required, status=evaluation.status, evidence_source=evaluation.source, evidence_refs=list(evaluation.references), message=evaluation.message))
        return results

    @staticmethod
    def _has_unexpected_runtime_error(spec: SmokeSpec, context: SmokeObservationContext) -> bool:
        expected = [item for item in spec.assertions if isinstance(item, ExpectedSafeErrorAssertion)]
        for error in context.structured_errors:
            if not any(
                (item.error_type is None or item.error_type == error.exception_type)
                and (item.error_code is None or item.error_code == error.code)
                for item in expected
            ):
                return True
        return False

    @staticmethod
    def _unexpected_task_failure(spec: SmokeSpec, context: SmokeObservationContext) -> SmokeFailure | None:
        allowed = set(spec.session.root_tasks)
        for event in context.timeline:
            if event.event_type == "dependency_registered" and isinstance(event.fields.get("task"), str):
                allowed.add(event.fields["task"])
            if event.event_type == "task_started" and isinstance(event.fields.get("task"), str) and event.fields["task"] not in allowed:
                return SmokeFailure(code="DEV_SMOKE_UNEXPECTED_TASK", message="Хронология содержит задачу вне замороженного происхождения Task Sandbox", assertion_id=None)
        return None

    @staticmethod
    def _capture_visual_if_ready(runtime: object, session_id: str, spec: SmokeSpec, context: SmokeObservationContext) -> SmokePendingEvaluation | None:
        for visual in spec.visual_assertions:
            condition = visual.capture_condition
            ready = _event_matches(context, condition.event_type) is not None if condition.kind == "event" else _task_event(context, "task_started" if condition.kind == "task_started" else "task_finished", condition.task or "") is not None
            if not ready:
                continue
            screenshot = runtime.get_screenshot()
            if screenshot.image is None or not _result_ok(screenshot.result):
                return None
            metadata = _result_details(screenshot.result).get("screenshot")
            if not isinstance(metadata, Mapping):
                return None
            screenshot_id = metadata.get("screenshot_id")
            screenshot_sha = metadata.get("sha256")
            if not isinstance(screenshot_id, str) or not isinstance(screenshot_sha, str):
                return None
            rubric_hash = hashlib.sha256(
                json.dumps({"rubric": visual.rubric, "capture_condition": visual.capture_condition.model_dump(mode="json")}, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            return SmokePendingEvaluation(assertion_id=visual.assertion_id, screenshot_id=screenshot_id, screenshot_sha256=screenshot_sha, rubric=visual.rubric, rubric_hash=rubric_hash, spec_hash=spec.spec_hash(), session_id=session_id)
        return None

    def _save_progress(self, record: SmokeRunRecord, results: Sequence[SmokeAssertionResult], context: SmokeObservationContext | None) -> SmokeRunRecord:
        if context is None:
            return record
        counts = {status: sum(item.status is status for item in results) for status in SmokeAssertionStatus}
        progress = SmokeProgress(passed=counts[SmokeAssertionStatus.PASS], failed=counts[SmokeAssertionStatus.FAIL], pending=counts[SmokeAssertionStatus.PENDING], unavailable=counts[SmokeAssertionStatus.UNAVAILABLE], elapsed_seconds=float(context.elapsed_seconds), current_task=context.current_task, evidence_health=context.evidence_health)
        return self.store.update(record.smoke_id, {"assertions": [_safe_model_json(item) for item in results], "progress": progress})

    def _cleanup_runtime(self, runtime: object, session_id: str | None, transaction: SmokeOverrideTransaction, record: SmokeRunRecord) -> tuple[SmokeCleanup, SmokeFailure | None]:
        failures: list[str] = []
        stopped = False
        task_clean = False
        scheduler_clean = False
        no_orphan = False
        port_free = False
        restored = False
        source_unchanged = False
        try:
            status = runtime.status()
            current_id = _result_session_id(status)
            state = _result_state(status)
            active_state = _runtime_state(state) in {"starting", "running"}
            if active_state and (session_id is None or current_id not in {None, session_id}):
                failures.append("DEV_SMOKE_FOREIGN_SESSION" if current_id is not None else "DEV_SMOKE_SESSION_ID_UNKNOWN")
            else:
                if _runtime_state(state) in {"running", "starting"}:
                    stopped_result = runtime.stop(preserve_task_state=False)
                    stopped = _result_ok(stopped_result)
                    if not stopped:
                        failures.append(str(getattr(stopped_result, "code", "DEV_SMOKE_STOP_FAILED")))
                elif _runtime_state(state) in {"stopped", "failed"}:
                    stopped = True
                cleanup_result = runtime.cleanup()
                task_clean = _result_ok(cleanup_result) or _result_state(cleanup_result) in {DevStatusKind.NO_SESSION.value, DevStatusKind.STOPPED.value}
                final_status = runtime.status()
                final_runtime_state = _runtime_state(_result_state(final_status))
                final_session_id = _result_session_id(final_status)
                no_orphan = final_runtime_state not in {"running", "starting"} and (
                    session_id is None or final_session_id in {None, session_id}
                )
                final_details = _result_details(final_status)
                lifecycle = final_details.get("task_lifecycle")
                task_clean = task_clean and (not isinstance(lifecycle, Mapping) or lifecycle.get("phase") in {"clean", "none"})
                port_probe = getattr(runtime, "port_probe", None)
                if callable(port_probe):
                    probe_result = port_probe(self.environment.host, self.environment.port)
                    if isinstance(probe_result, bool):
                        port_free = not probe_result
                    else:
                        failures.append("DEV_SMOKE_PORT_PROBE_INVALID")
                else:
                    failures.append("DEV_SMOKE_PORT_PROBE_UNAVAILABLE")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"DEV_SMOKE_CLEANUP_{type(exc).__name__.upper()[:32]}")
        try:
            payload = read_profile_payload(self.environment.profile_file, repository_root=self.environment.repository_root)
            catalog = TaskCatalog.from_payload(
                payload,
                profile_name=self.environment.profile_name,
            )
            state = scheduler_state(payload, catalog)
            scheduler_clean = all(item["enabled"] is False and item["next_run"] == SCHEDULER_RESET_TIME for item in state.values()) and TaskPolicyStore(self.environment).read() is None
            if not scheduler_clean:
                failures.append("DEV_SMOKE_SCHEDULER_NOT_CLEAN")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"DEV_SMOKE_SCHEDULER_{type(exc).__name__.upper()[:32]}")
        try:
            restored = transaction.restore()
            if not restored:
                failures.append("DEV_SMOKE_OVERRIDE_RESTORE_FAILED")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"DEV_SMOKE_OVERRIDE_RESTORE_{type(exc).__name__.upper()[:32]}")
        try:
            restored = restored and transaction.mutation_guard_ok()
            if not restored:
                failures.append("DEV_SMOKE_CONFIG_MUTATION_GUARD_FAILED")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"DEV_SMOKE_CONFIG_GUARD_{type(exc).__name__.upper()[:32]}")
        try:
            source_unchanged = _same_source(record.source, _source_snapshot(capture_git_snapshot(self.environment.repository_root)))
            if not source_unchanged:
                failures.append("INVALIDATED_SOURCE_DRIFT")
        except Exception:  # noqa: BLE001 — ошибка проверки source никогда не даёт PASS
            failures.append("DEV_SMOKE_SOURCE_GUARD_FAILED")
        if not stopped and "DEV_SMOKE_FOREIGN_SESSION" not in failures and "DEV_SMOKE_SESSION_ID_UNKNOWN" not in failures:
            failures.append("DEV_SMOKE_SESSION_NOT_STOPPED")
        if not task_clean:
            failures.append("DEV_SMOKE_TASK_CLEANUP_NOT_CONFIRMED")
        if not scheduler_clean:
            failures.append("DEV_SMOKE_SCHEDULER_NOT_CLEAN")
        if not restored:
            failures.append("DEV_SMOKE_OVERRIDE_RESTORE_FAILED")
        if not no_orphan:
            failures.append("DEV_SMOKE_OWNED_ORPHAN_REMAINS")
        if not port_free:
            failures.append("DEV_SMOKE_PORT_NOT_FREE")
        confirmed = not failures and stopped and task_clean and scheduler_clean and restored and source_unchanged and no_orphan and port_free
        cleanup = SmokeCleanup(attempted=True, session_stopped=stopped, task_cleanup_confirmed=task_clean, scheduler_clean=scheduler_clean, overrides_restored=restored, source_unchanged=source_unchanged, no_owned_orphan=no_orphan, port_free=port_free, confirmed=confirmed, failure_code=failures[0] if failures else None)
        return cleanup, SmokeFailure(code=failures[0], message="; ".join(failures)) if failures else None

    def _finish_record(self, record: SmokeRunRecord, outcome: SmokeOutcome, code: str, message: str, cleanup: SmokeCleanup, *, assertions: Sequence[SmokeAssertionResult] | None = None, primary_failure: SmokeFailure | None = None, harness_failure: SmokeFailure | None = None) -> SmokeResult:
        finished_at = _timestamp_now(self.now)
        updates: dict[str, object] = {
            "state": SmokeState.FINISHED,
            "outcome": outcome,
            "finished_at": finished_at,
            "cleanup": cleanup,
            "pending_evaluation": None,
        }
        if assertions is not None:
            updates["assertions"] = list(assertions)
        if primary_failure is not None:
            updates["primary_failure"] = primary_failure
        if harness_failure is not None:
            updates["harness_failure"] = harness_failure
        candidate = record.model_copy(update=updates)
        candidate = _validate_json_model(SmokeRunRecord, _safe_model_json(candidate))
        if not isinstance(candidate, SmokeRunRecord):
            raise SmokeStoreError("DEV_SMOKE_STATE_INVALID", "Финальный SmokeRun имеет неверный тип")
        result = SmokeResult(smoke_id=candidate.smoke_id, spec_hash=candidate.spec_hash, outcome=candidate.outcome, code=code, message=message, source=candidate.source, session_id=candidate.session_id, target_profile=candidate.target_profile, target_identity=candidate.target_identity, assertions=candidate.assertions, cleanup=candidate.cleanup, primary_failure=candidate.primary_failure, harness_failure=candidate.harness_failure, external_verdict=candidate.external_verdict, finished_at=candidate.finished_at or finished_at)
        self.store.finish(record.smoke_id, updates, result)
        return result

    def _finalize_without_runtime(self, record: SmokeRunRecord, *, outcome: SmokeOutcome, code: str, message: str, harness_failure: SmokeFailure | None = None, no_owned_orphan: bool = True) -> None:
        cleanup = SmokeCleanup(
            attempted=False,
            session_stopped=True,
            task_cleanup_confirmed=True,
            scheduler_clean=True,
            overrides_restored=True,
            source_unchanged=outcome is not SmokeOutcome.INVALIDATED,
            no_owned_orphan=no_owned_orphan,
            port_free=False,
            confirmed=False,
            failure_code=code,
        )
        try:
            self._finish_record(record, outcome, code, message, cleanup, primary_failure=harness_failure, harness_failure=harness_failure)
        except SmokeStoreError:
            return

    def _finalize_cancelled(self, record: SmokeRunRecord) -> SmokeRunRecord:
        failure = SmokeFailure(code="DEV_SMOKE_CANCELLED", message="SmokeRun отменён до внешней оценки")
        cleanup = record.cleanup
        if not cleanup.attempted:
            cleanup = SmokeCleanup(
                attempted=True,
                session_stopped=True,
                task_cleanup_confirmed=True,
                scheduler_clean=True,
                overrides_restored=True,
                source_unchanged=True,
                no_owned_orphan=True,
                port_free=False,
                confirmed=False,
                failure_code="DEV_SMOKE_PORT_PROBE_UNAVAILABLE",
            )
        self._finish_record(record, SmokeOutcome.CANCELLED, "DEV_SMOKE_CANCELLED", failure.message, cleanup, primary_failure=failure)
        return self.store.load(record.smoke_id)

    def _recover_crashed(self, record: SmokeRunRecord, reason: str) -> SmokeRunRecord:
        runtime = self.runtime_factory()
        try:
            recover = getattr(runtime, "recover", None)
            if callable(recover):
                recover()
        except Exception:  # noqa: BLE001, S110 — восстановление должно продолжить cleanup
            pass
        try:
            self.store.load_spec(record.smoke_id)
            transaction = SmokeOverrideTransaction.from_state(self.environment, self._config_registry(), record.overrides, save_state=lambda value: self.store.update(record.smoke_id, {"overrides": value}))
            cleanup, cleanup_failure = self._cleanup_runtime(runtime, record.session_id, transaction, record)
        except Exception as exc:  # noqa: BLE001
            cleanup = SmokeCleanup(attempted=True, source_unchanged=False, failure_code=f"DEV_SMOKE_RECOVERY_{type(exc).__name__.upper()[:32]}")
            cleanup_failure = SmokeFailure(code=cleanup.failure_code or "DEV_SMOKE_RECOVERY_FAILED", message="Восстановление после сбоя не смогло подтвердить очистку")
        outcome = SmokeOutcome.CANCELLED if self._cancel_requested_safely(record.smoke_id) else SmokeOutcome.HARNESS_FAILED
        if record.pending_evaluation is not None and cleanup.confirmed and outcome is not SmokeOutcome.CANCELLED:
            try:
                return self.store.update(record.smoke_id, {"state": SmokeState.AWAITING_EXTERNAL_EVALUATION, "cleanup": cleanup})
            except SmokeStoreError:
                pass
        try:
            self._finish_record(record, outcome, "DEV_SMOKE_CANCELLED" if outcome is SmokeOutcome.CANCELLED else "DEV_SMOKE_SUPERVISOR_CRASHED", "Supervisor завершился до подтверждения SmokeRun", cleanup, primary_failure=SmokeFailure(code="DEV_SMOKE_CANCELLED" if outcome is SmokeOutcome.CANCELLED else reason, message="Запуск закрыт в безопасном режиме после сбоя supervisor"), harness_failure=cleanup_failure)
        except SmokeStoreError:
            pass
        return self.store.load(record.smoke_id)

    def _cancel_requested_safely(self, smoke_id: str) -> bool:
        try:
            return self.store.is_cancel_requested(smoke_id)
        except SmokeStoreError:
            return False

    @staticmethod
    def _record_details(record: SmokeRunRecord, *, result: SmokeResult | None) -> dict[str, object]:
        details: dict[str, object] = {
            "smoke_id": record.smoke_id,
            "state": record.state.value,
            "outcome": record.outcome.value if record.outcome is not None else None,
            "spec_hash": record.spec_hash,
            "source": _safe_model_json(record.source),
            "deadline_at": record.deadline_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "session_id": record.session_id,
            "target_profile": record.target_profile,
            "target_identity": record.target_identity,
            "progress": _safe_model_json(record.progress),
            "assertions": [_safe_model_json(item) for item in record.assertions],
            "cleanup": _safe_model_json(record.cleanup),
            "overrides": {
                "persisted": record.overrides.persisted,
                "applied": record.overrides.applied,
                "restored": record.overrides.restored,
                "verified": record.overrides.verified,
                "baseline_digest": record.overrides.baseline_digest,
            },
            "pending_evaluation": _safe_model_json(record.pending_evaluation) if record.pending_evaluation is not None else None,
        }
        if result is not None:
            details["result"] = _safe_model_json(result)
        if record.primary_failure is not None:
            details["primary_failure"] = _safe_model_json(record.primary_failure)
        if record.harness_failure is not None:
            details["harness_failure"] = _safe_model_json(record.harness_failure)
        if record.external_verdict is not None:
            details["external_verdict"] = _safe_model_json(record.external_verdict)
        return details


def _runtime_state(state: str | None) -> str:
    if state == DevStatusKind.RUNNING_OWNED.value or state == DevSessionState.RUNNING.value:
        return "running"
    if state in {DevStatusKind.STARTING.value, DevSessionState.STARTING.value}:
        return "starting"
    if state in {DevStatusKind.STOPPED.value, DevStatusKind.NO_SESSION.value, DevSessionState.STOPPED.value, None}:
        return "stopped"
    return "failed"


def _outcome_code(outcome: SmokeOutcome) -> str:
    return {
        SmokeOutcome.PASS: "DEV_SMOKE_PASS",
        SmokeOutcome.PRODUCT_FAILED: "DEV_SMOKE_PRODUCT_FAILED",
        SmokeOutcome.PRECONDITION_FAILED: "DEV_SMOKE_PRECONDITION_FAILED",
        SmokeOutcome.HARNESS_FAILED: "DEV_SMOKE_HARNESS_FAILED",
        SmokeOutcome.EVIDENCE_INCOMPLETE: "DEV_SMOKE_EVIDENCE_INCOMPLETE",
        SmokeOutcome.TIMEOUT: "DEV_SMOKE_TIMEOUT",
        SmokeOutcome.INVALIDATED: "INVALIDATED_SOURCE_DRIFT",
        SmokeOutcome.CANCELLED: "DEV_SMOKE_CANCELLED",
    }[outcome]


def _outcome_message(outcome: SmokeOutcome) -> str:
    return {
        SmokeOutcome.PASS: "SmokeRun и все проверки целостности пройдены",
        SmokeOutcome.PRODUCT_FAILED: "Проверка продукта не пройдена",
        SmokeOutcome.PRECONDITION_FAILED: "Предварительные условия или запуск DevSession не пройдены",
        SmokeOutcome.HARNESS_FAILED: "Smoke Harness не подтвердил безопасное завершение",
        SmokeOutcome.EVIDENCE_INCOMPLETE: "Подтверждающих данных Evidence API недостаточно для PASS",
        SmokeOutcome.TIMEOUT: "SmokeRun завершён по замороженному крайнему сроку",
        SmokeOutcome.INVALIDATED: "SmokeRun признан недействительным из-за изменения source",
        SmokeOutcome.CANCELLED: "SmokeRun отменён",
    }[outcome]


__all__ = [
    "ConfigRegistry",
    "ConfigRestoredAssertion",
    "ConfigValueAssertion",
    "DependencyOccurredAssertion",
    "DevPortStateAssertion",
    "DurationWithinBoundAssertion",
    "EventNotOccurredAssertion",
    "EventOccurredAssertion",
    "EvidenceHealthAssertion",
    "ExpectedSafeErrorAssertion",
    "NoRuntimeErrorAssertion",
    "RuntimeStateAssertion",
    "SessionLogContainsAssertion",
    "SessionLogNotContainsAssertion",
    "SmokeAssertion",
    "SmokeAssertionResult",
    "SmokeAssertionStatus",
    "SmokeCapabilityDescriptor",
    "SmokeCapabilityRegistry",
    "SmokeCapabilitySchema",
    "SmokeCleanup",
    "SmokeConfigOverride",
    "SmokeControl",
    "SmokeExternalVerdict",
    "SmokeFieldSchema",
    "SmokeGameCheckpoint",
    "SmokeGameObservationRequest",
    "SmokeGameObservationSpec",
    "SmokeOutcome",
    "SmokePendingEvaluation",
    "SmokeProgress",
    "SmokeResult",
    "SmokeRunManager",
    "SmokeRunRecord",
    "SmokeSessionSpec",
    "SmokeSetupSpec",
    "SmokeSourceSnapshot",
    "SmokeSpec",
    "SmokeState",
    "SmokeStateStore",
    "SmokeStoreError",
    "SmokeSupervisorBackend",
    "SmokeSupervisorIdentity",
    "SmokeValidationIssue",
    "SmokeVisualAssertion",
    "TaskNotStartedAssertion",
    "TaskStartedAssertion",
    "VisualCaptureCondition",
]
