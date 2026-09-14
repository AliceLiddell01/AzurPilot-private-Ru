"""Закрытые DTO и стабильные коды Python tooling.

Эти модели являются границей между сервисами, CLI и будущими transport adapters.
Свободные словари намеренно не используются в operation payload: добавление
неизвестного поля должно быть заметно в тестах и при чтении машинного вывода.
"""

from __future__ import annotations

from enum import Enum, IntEnum

from pydantic import BaseModel, ConfigDict, Field


class _StrEnum(str, Enum):
    """Строковый enum, совместимый с JSON и обычным Python 3.14."""


class ExitCode(IntEnum):
    """Категории завершения CLI."""

    SUCCESS = 0
    INVOCATION = 2
    PRECONDITION = 20
    OWNERSHIP_CONFLICT = 21
    TIMEOUT = 22
    DEPENDENCY_UNAVAILABLE = 23
    ROLLED_BACK = 24
    ROLLBACK_UNKNOWN = 25
    UNEXPECTED = 30


class ResultCode(_StrEnum):
    """Стабильные machine-readable причины результата."""

    OK = "OK"
    TOOLING_INVALID_INVOCATION = "TOOLING_INVALID_INVOCATION"
    TOOLING_REPOSITORY_NOT_FOUND = "TOOLING_REPOSITORY_NOT_FOUND"
    TOOLING_REPOSITORY_INVALID = "TOOLING_REPOSITORY_INVALID"
    TOOLING_REPOSITORY_AMBIGUOUS = "TOOLING_REPOSITORY_AMBIGUOUS"
    TOOLING_PRECONDITION_FAILED = "TOOLING_PRECONDITION_FAILED"
    TOOLING_REMOTE_IDENTITY_UNVERIFIED = "TOOLING_REMOTE_IDENTITY_UNVERIFIED"
    TOOLING_BACKUP_REQUIRED = "TOOLING_BACKUP_REQUIRED"
    TOOLING_BACKUP_FAILED = "TOOLING_BACKUP_FAILED"
    TOOLING_CAPABILITY_UNAVAILABLE = "TOOLING_CAPABILITY_UNAVAILABLE"
    TOOLING_CAPABILITY_UNSUPPORTED = "TOOLING_CAPABILITY_UNSUPPORTED"
    TOOLING_OPERATION_CONFLICT = "TOOLING_OPERATION_CONFLICT"
    TOOLING_TRANSACTION_RECOVERY_REQUIRED = "TOOLING_TRANSACTION_RECOVERY_REQUIRED"
    TOOLING_PORT_CONFLICT = "TOOLING_PORT_CONFLICT"
    TOOLING_TIMEOUT = "TOOLING_TIMEOUT"
    TOOLING_CANCELLED = "TOOLING_CANCELLED"
    TOOLING_PROCESS_EXITED = "TOOLING_PROCESS_EXITED"
    TOOLING_CLEANUP_UNKNOWN = "TOOLING_CLEANUP_UNKNOWN"
    TOOLING_INFRASTRUCTURE_FAILED = "TOOLING_INFRASTRUCTURE_FAILED"
    TOOLING_ADB_FAILED = "TOOLING_ADB_FAILED"
    TOOLING_SHORTCUT_FAILED = "TOOLING_SHORTCUT_FAILED"
    TOOLING_DEPENDENCY_UNAVAILABLE = "TOOLING_DEPENDENCY_UNAVAILABLE"
    TOOLING_APPLY_FAILED_ROLLED_BACK = "TOOLING_APPLY_FAILED_ROLLED_BACK"
    TOOLING_REPAIR_REQUIRED = "TOOLING_REPAIR_REQUIRED"
    TOOLING_REPAIR_FAILED = "TOOLING_REPAIR_FAILED"
    TOOLING_UPDATE_DIRTY = "TOOLING_UPDATE_DIRTY"
    TOOLING_UPDATE_LOCAL_AHEAD = "TOOLING_UPDATE_LOCAL_AHEAD"
    TOOLING_UPDATE_DIVERGED = "TOOLING_UPDATE_DIVERGED"
    TOOLING_GIT_FAILED = "TOOLING_GIT_FAILED"
    TOOLING_ROLLBACK_UNKNOWN = "TOOLING_ROLLBACK_UNKNOWN"
    TOOLING_VERIFICATION_UNKNOWN = "TOOLING_VERIFICATION_UNKNOWN"
    TOOLING_UNEXPECTED = "TOOLING_UNEXPECTED"


class OperationState(_StrEnum):
    """Состояние операции в общем envelope."""

    READY = "ready"
    RUNNING = "running"
    STOPPED = "stopped"
    NOT_CONFIGURED = "not_configured"
    DIAGNOSTIC = "diagnostic"
    CONFLICT = "conflict"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    IN_FLIGHT = "in_flight"
    UNKNOWN = "unknown"


class CapabilityStatus(_StrEnum):
    """Состояние необязательной возможности."""

    READY = "ready"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class RootSource(_StrEnum):
    """Источник корня репозитория."""

    EXPLICIT = "explicit"
    CONFIGURED = "configured"
    INSTALLATION = "installation"


class WarningCode(_StrEnum):
    """Ограниченные предупреждения, не меняющие основной код результата."""

    TOOLING_ADB_NOT_CONFIGURED = "TOOLING_ADB_NOT_CONFIGURED"
    TOOLING_CLI_NOT_ON_PATH = "TOOLING_CLI_NOT_ON_PATH"
    TOOLING_SHORTCUT_UNSUPPORTED = "TOOLING_SHORTCUT_UNSUPPORTED"
    TOOLING_POSTGRES_UNAVAILABLE = "TOOLING_POSTGRES_UNAVAILABLE"
    TOOLING_POSTGRES_BACKUP_NOT_RUN = "TOOLING_POSTGRES_BACKUP_NOT_RUN"
    TOOLING_CADDY_NOT_CONFIGURED = "TOOLING_CADDY_NOT_CONFIGURED"
    TOOLING_BROWSER_NOT_OPENED = "TOOLING_BROWSER_NOT_OPENED"
    TOOLING_OUTPUT_TRUNCATED = "TOOLING_OUTPUT_TRUNCATED"
    TOOLING_LEGACY_COMPATIBILITY = "TOOLING_LEGACY_COMPATIBILITY"


class ClosedModel(BaseModel):
    """Общая строгая конфигурация всех DTO."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ToolingWarning(ClosedModel):
    code: WarningCode
    message: str = Field(min_length=1, max_length=240)


class RepositoryRootEvidence(ClosedModel):
    source: RootSource
    candidate_count: int = Field(ge=1, le=8)
    validation_checks: tuple[str, ...] = Field(min_length=1, max_length=12)
    root_identity: str = Field(
        pattern=r"^[0-9a-f]{16,64}$", min_length=16, max_length=64
    )


class RepositoryDetails(ClosedModel):
    source: RootSource
    project_name: str = Field(min_length=1, max_length=80)


class CapabilityCheck(ClosedModel):
    name: str = Field(min_length=1, max_length=80)
    status: CapabilityStatus
    message: str = Field(min_length=1, max_length=240)


class DoctorDetails(ClosedModel):
    checks: tuple[CapabilityCheck, ...] = Field(max_length=32)
    healthy: bool


class DoctorEvidence(ClosedModel):
    repository: RepositoryRootEvidence
    python_version: str = Field(min_length=1, max_length=32)
    platform: str = Field(min_length=1, max_length=80)


class RemoteIdentityEvidence(ClosedModel):
    """Безопасное доказательство канонической идентичности без публикации учётных данных URL."""

    configured: str = Field(min_length=1, max_length=256)
    actual: str = Field(min_length=1, max_length=256)
    equivalent: bool
    tracking: str = Field(min_length=1, max_length=256)
    upstream_push_policy: str = Field(min_length=1, max_length=80)


class PostgreSqlBackupEvidence(ClosedModel):
    """Минимальные сведения о логической резервной копии; абсолютный путь намеренно не хранится."""

    backup_id: str = Field(min_length=8, max_length=120)
    format: str = Field(default="custom", min_length=1, max_length=40)
    validated: bool
    external: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pre_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    provenance: str = Field(min_length=16, max_length=64)


class ProcessEvidence(ClosedModel):
    pid: int | None = Field(default=None, ge=1)
    started_at: float | None = None
    executable_name: str = Field(min_length=1, max_length=120)
    argv: tuple[str, ...] = Field(max_length=32)
    working_directory_name: str = Field(min_length=1, max_length=120)
    identity_confirmed: bool


class LifecycleRecord(ClosedModel):
    """Внутренний файл состояния lifecycle, содержащий полную идентичность владения."""

    schema_version: int = Field(default=1, ge=1, le=1)
    root_identity: str = Field(
        pattern=r"^[0-9a-f]{16,64}$", min_length=16, max_length=64
    )
    pid: int = Field(ge=1)
    started_at: float
    executable: str = Field(min_length=1, max_length=1024)
    argv: tuple[str, ...] = Field(min_length=1, max_length=32)
    working_directory: str = Field(min_length=1, max_length=1024)
    process_group: int | None = Field(default=None, ge=1)
    port: int = Field(ge=1, le=65535)
    updated_at: str = Field(min_length=1, max_length=40)


class LifecycleDetails(ClosedModel):
    status: OperationState
    pid: int | None = Field(default=None, ge=1)
    port: int = Field(ge=1, le=65535)
    readiness: str = Field(min_length=1, max_length=80)
    cleanup_confirmed: bool = True
    exit_status: int | None = None


class LifecycleEvidence(ClosedModel):
    repository: RepositoryRootEvidence
    process: ProcessEvidence | None = None
    port_owner: str = Field(min_length=1, max_length=40)


class BuildDetails(ClosedModel):
    status: OperationState
    venv_created: bool
    config_created: bool
    bootstrap_source: str = Field(min_length=1, max_length=80)
    adb_status: CapabilityStatus
    console_script: CapabilityStatus
    path_registration: CapabilityStatus
    adb_version: str | None = Field(default=None, max_length=40)
    shortcut_status: CapabilityStatus = CapabilityStatus.UNSUPPORTED


class BuildEvidence(ClosedModel):
    repository: RepositoryRootEvidence
    lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transaction_id: str | None = Field(default=None, max_length=80)


class RepairDetails(ClosedModel):
    status: OperationState
    diagnostic_only: bool
    issues: tuple[str, ...] = Field(max_length=16)
    repaired: bool
    recovery_reason: str | None = Field(default=None, max_length=120)
    ownership_state: str | None = Field(default=None, max_length=40)
    shortcut_status: CapabilityStatus = CapabilityStatus.UNSUPPORTED


class RepairEvidence(ClosedModel):
    repository: RepositoryRootEvidence
    lock_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    environment_checked: bool
    transaction_id: str | None = Field(default=None, max_length=80)


class UpdateDetails(ClosedModel):
    status: OperationState
    branch: str = Field(min_length=1, max_length=120)
    remote: str = Field(min_length=1, max_length=80)
    dependency_changed: bool
    fast_forwarded: bool
    backup_required: bool = False
    backup_validated: bool = False
    transaction_phase: str | None = Field(default=None, max_length=40)
    recovery_action: str | None = Field(default=None, max_length=120)


class UpdateEvidence(ClosedModel):
    repository: RepositoryRootEvidence
    pre_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    post_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    remote_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    transaction_id: str | None = Field(default=None, max_length=80)
    remote_identity: RemoteIdentityEvidence | None = None
    postgres_backup: PostgreSqlBackupEvidence | None = None


class GitEvidence(ClosedModel):
    branch: str = Field(min_length=1, max_length=120)
    remote: str = Field(min_length=1, max_length=80)
    pre_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    remote_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class TransactionJournal(ClosedModel):
    """Минимальный журнал для восстановления внешней транзакции."""

    schema_version: int = Field(default=1, ge=1, le=1)
    transaction_id: str = Field(min_length=8, max_length=80)
    operation: str = Field(min_length=1, max_length=40)
    root_identity: str = Field(
        pattern=r"^[0-9a-f]{16,64}$", min_length=16, max_length=64
    )
    phase: str = Field(min_length=1, max_length=40)
    pre_head: str | None = Field(default=None, max_length=64)
    target_head: str | None = Field(default=None, max_length=64)
    backup_present: bool = False
    updated_at: str = Field(min_length=1, max_length=40)
    candidate_path: str | None = Field(default=None, max_length=1024)
    backup_path: str | None = Field(default=None, max_length=1024)
    venv_path: str | None = Field(default=None, max_length=1024)
    previous_path: str | None = Field(default=None, max_length=1024)
    remote_name: str | None = Field(default=None, max_length=80)
    remote_branch: str | None = Field(default=None, max_length=256)
    backup_id: str | None = Field(default=None, max_length=120)
    failure_reason: str | None = Field(default=None, max_length=240)
    ownership_confirmed: bool = False


class ToolingResult[TDetails: BaseModel, TEvidence: BaseModel](ClosedModel):
    """Общий закрытый envelope для human и machine adapters."""

    schema_version: int = Field(default=1, ge=1, le=1)
    ok: bool
    code: ResultCode
    state: OperationState
    message: str = Field(min_length=1, max_length=300)
    operation_id: str | None = Field(default=None, max_length=80)
    details: TDetails | None = None
    warnings: tuple[ToolingWarning, ...] = Field(default_factory=tuple, max_length=16)
    evidence: TEvidence | None = None


def exit_code_for(code: ResultCode, ok: bool = False) -> ExitCode:
    """Преобразовать код результата в стабильную категорию процесса."""

    if ok or code is ResultCode.OK:
        return ExitCode.SUCCESS
    if code is ResultCode.TOOLING_INVALID_INVOCATION:
        return ExitCode.INVOCATION
    if code in {
        ResultCode.TOOLING_OPERATION_CONFLICT,
        ResultCode.TOOLING_PORT_CONFLICT,
        ResultCode.TOOLING_UPDATE_LOCAL_AHEAD,
        ResultCode.TOOLING_UPDATE_DIVERGED,
        ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
        ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
        ResultCode.TOOLING_CLEANUP_UNKNOWN,
    }:
        return ExitCode.OWNERSHIP_CONFLICT
    if code in {ResultCode.TOOLING_TIMEOUT, ResultCode.TOOLING_CANCELLED}:
        return ExitCode.TIMEOUT
    if code in {
        ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
        ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
        ResultCode.TOOLING_CAPABILITY_UNSUPPORTED,
        ResultCode.TOOLING_BACKUP_REQUIRED,
        ResultCode.TOOLING_BACKUP_FAILED,
        ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
        ResultCode.TOOLING_ADB_FAILED,
        ResultCode.TOOLING_SHORTCUT_FAILED,
    }:
        return ExitCode.DEPENDENCY_UNAVAILABLE
    if code is ResultCode.TOOLING_APPLY_FAILED_ROLLED_BACK:
        return ExitCode.ROLLED_BACK
    if code in {
        ResultCode.TOOLING_ROLLBACK_UNKNOWN,
        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
    }:
        return ExitCode.ROLLBACK_UNKNOWN
    if code is ResultCode.TOOLING_UNEXPECTED:
        return ExitCode.UNEXPECTED
    return ExitCode.PRECONDITION


__all__ = [
    "BuildDetails",
    "BuildEvidence",
    "CapabilityCheck",
    "CapabilityStatus",
    "ClosedModel",
    "DoctorDetails",
    "DoctorEvidence",
    "ExitCode",
    "GitEvidence",
    "LifecycleDetails",
    "LifecycleEvidence",
    "LifecycleRecord",
    "OperationState",
    "PostgreSqlBackupEvidence",
    "ProcessEvidence",
    "RemoteIdentityEvidence",
    "RepairDetails",
    "RepairEvidence",
    "RepositoryDetails",
    "RepositoryRootEvidence",
    "ResultCode",
    "RootSource",
    "ToolingResult",
    "ToolingWarning",
    "TransactionJournal",
    "UpdateDetails",
    "UpdateEvidence",
    "WarningCode",
    "exit_code_for",
]
