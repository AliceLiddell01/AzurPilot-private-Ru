"""Закрытые DTO и стабильные коды Python tooling.

Эти модели являются границей между сервисами, CLI и будущими transport adapters.
Свободные словари намеренно не используются в operation payload: добавление
неизвестного поля должно быть заметно в тестах и при чтении машинного вывода.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ResultCode(StrEnum):
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
    TOOLING_MANIFEST_INVALID = "TOOLING_MANIFEST_INVALID"
    TOOLING_DELIVERY_SCOPE_INVALID = "TOOLING_DELIVERY_SCOPE_INVALID"
    TOOLING_SECRET_SCAN_FAILED = "TOOLING_SECRET_SCAN_FAILED"
    TOOLING_SECRET_SCANNER_UNAVAILABLE = "TOOLING_SECRET_SCANNER_UNAVAILABLE"
    TOOLING_PUSH_UNKNOWN = "TOOLING_PUSH_UNKNOWN"
    TOOLING_REMOTE_REF_CONFLICT = "TOOLING_REMOTE_REF_CONFLICT"
    TOOLING_PR_BODY_INVALID = "TOOLING_PR_BODY_INVALID"
    TOOLING_PR_IDENTITY_MISMATCH = "TOOLING_PR_IDENTITY_MISMATCH"
    TOOLING_PR_PUBLICATION_UNKNOWN = "TOOLING_PR_PUBLICATION_UNKNOWN"
    TOOLING_PROVIDER_UNAVAILABLE = "TOOLING_PROVIDER_UNAVAILABLE"
    TOOLING_PROVIDER_FAILED = "TOOLING_PROVIDER_FAILED"
    MCP_SOURCE_BUNDLE_INVALID = "MCP_SOURCE_BUNDLE_INVALID"
    MCP_SOURCE_BUNDLE_DRIFT = "MCP_SOURCE_BUNDLE_DRIFT"
    MCP_VERSION_BUMP_REQUIRED = "MCP_VERSION_BUMP_REQUIRED"
    MCP_RUNTIME_STALE = "MCP_RUNTIME_STALE"
    MCP_PLUGIN_RUNTIME_INCOMPATIBLE = "MCP_PLUGIN_RUNTIME_INCOMPATIBLE"
    MCP_RELOAD_REQUIRED = "MCP_RELOAD_REQUIRED"
    MCP_ENVIRONMENT_STALE = "MCP_ENVIRONMENT_STALE"
    MCP_AUTH_NOT_CONFIGURED = "MCP_AUTH_NOT_CONFIGURED"
    MCP_RUNTIME_UNAVAILABLE = "MCP_RUNTIME_UNAVAILABLE"
    TOOLING_UNEXPECTED = "TOOLING_UNEXPECTED"


class OperationState(StrEnum):
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


class CapabilityStatus(StrEnum):
    """Состояние необязательной возможности."""

    READY = "ready"
    NOT_CONFIGURED = "not_configured"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RootSource(StrEnum):
    """Источник корня репозитория."""

    EXPLICIT = "explicit"
    CONFIGURED = "configured"
    INSTALLATION = "installation"


class PublicationIntent(StrEnum):
    """Разрешённый объём delivery-операции."""

    VALIDATE_ONLY = "validate_only"
    COMMIT_AND_PUSH = "commit_and_push"


class DeliveryPhase(StrEnum):
    """Фазы публикации, сохраняемые для read-only recovery."""

    VALIDATED = "validated"
    STAGED = "staged"
    PRE_COMMIT_SCANNED = "pre_commit_scanned"
    COMMITTED = "committed"
    COMMITTED_RANGE_SCANNED = "committed_range_scanned"
    PUSH_IN_FLIGHT = "push_in_flight"
    DELIVERED = "delivered"
    PUSH_NOT_DELIVERED = "push_not_delivered"
    UNKNOWN = "unknown"
    FAILED = "failed"


class FindingSeverity(StrEnum):
    """Ограниченные уровни внешнего review finding."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    TRIVIAL = "trivial"
    INFO = "info"


class FindingDisposition(StrEnum):
    """Обязательная классификация результата внешнего review."""

    CONFIRMED = "confirmed"
    PARTIALLY_CONFIRMED = "partially confirmed"
    FALSE_POSITIVE = "false positive"
    INSUFFICIENT_EVIDENCE = "insufficient evidence"


class WarningCode(StrEnum):
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
    MCP_RECONCILIATION_FAILED = "MCP_RECONCILIATION_FAILED"


class ClosedModel(BaseModel):
    """Общая строгая конфигурация всех DTO."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class RepositoryIdentity(ClosedModel):
    """Transport-neutral identity hosted repository."""

    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
    owner: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repository}"


class RemoteIdentity(ClosedModel):
    """Подтверждённая identity настроенного Git remote."""

    name: str = Field(min_length=1, max_length=80)
    fetch_url: str = Field(min_length=1, max_length=256)
    push_url: str | None = Field(default=None, max_length=256)
    repository: RepositoryIdentity


class BranchIdentity(ClosedModel):
    """Имя ветки и подтверждённый commit без подмены SHA именем."""

    name: str = Field(min_length=1, max_length=120)
    sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class CommitIdentity(ClosedModel):
    """Commit и его подтверждённый родитель."""

    sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    parent_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class GitRange(ClosedModel):
    """Точный диапазон Git для scoped analysis."""

    start_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    end_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class AnalysisScope(ClosedModel):
    """Allowlist путей и/или exact Git range для security-анализатора."""

    paths: tuple[str, ...] = Field(default_factory=tuple, max_length=128)
    git_range: GitRange | None = None
    mode: Literal["staged", "committed_range"]


class GitSnapshot(ClosedModel):
    """Bounded snapshot Git-состояния перед mutating delivery."""

    repository: RepositoryIdentity
    root_identity: str = Field(pattern=r"^[0-9a-f]{16,64}$")
    branch: str = Field(min_length=1, max_length=120)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    base_branch: str = Field(default="personal/stable", min_length=1, max_length=256)
    remote_name: str = Field(min_length=1, max_length=80)
    remote_branch: str = Field(min_length=1, max_length=256)
    remote_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    upstream: str | None = Field(default=None, max_length=256)
    dirty_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=128)
    staged_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=128)
    active_operation: bool


class FileState(ClosedModel):
    """Ожидаемое содержимое одного allowlisted файла."""

    exists: bool
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size: int | None = Field(default=None, ge=0, le=16 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_hash_presence(self) -> FileState:
        if self.exists and self.sha256 is None:
            raise ValueError("для существующего файла требуется SHA-256")
        if not self.exists and (self.sha256 is not None or self.size is not None):
            raise ValueError("для отсутствующего файла нельзя указывать содержимое")
        return self


class DeliveryTarget(ClosedModel):
    """Одна repository-relative пара preimage/postimage."""

    path: str = Field(min_length=1, max_length=512)
    preimage: FileState
    postimage: FileState


class DeliveryChange(ClosedModel):
    """Безопасное обозначение изменения allowlisted target для adapters."""

    path: str = Field(min_length=1, max_length=512)
    change: Literal["A", "M", "D"]


class DeliveryManifest(ClosedModel):
    """Immutable closed-schema request для working-tree delivery."""

    schema_version: Literal[1] = 1
    repository: RepositoryIdentity
    expected_branch: str = Field(min_length=1, max_length=120)
    expected_local_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    expected_base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    base_remote_name: str = Field(default="origin", min_length=1, max_length=80)
    base_branch: str = Field(default="personal/stable", min_length=1, max_length=256)
    remote_name: str = Field(min_length=1, max_length=80)
    remote_branch: str = Field(min_length=1, max_length=256)
    expected_remote_sha: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{40,64}$"
    )
    targets: tuple[DeliveryTarget, ...] = Field(min_length=1, max_length=128)
    commit_message: str = Field(min_length=1, max_length=240)
    publication_intent: PublicationIntent


class DeliveryJournal(ClosedModel):
    """Внешнее состояние delivery для status/recover без повторной мутации."""

    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=80)
    repository_root_identity: str = Field(pattern=r"^[0-9a-f]{16,64}$")
    phase: DeliveryPhase
    branch: str = Field(min_length=1, max_length=120)
    remote_name: str = Field(min_length=1, max_length=80)
    remote_branch: str = Field(min_length=1, max_length=256)
    expected_local_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    expected_base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    expected_remote_sha: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{40,64}$"
    )
    target_paths: tuple[str, ...] = Field(min_length=1, max_length=128)
    changes: tuple[DeliveryChange, ...] = Field(default_factory=tuple, max_length=128)
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    updated_at: str = Field(min_length=1, max_length=40)
    last_error_code: ResultCode | None = None
    last_error_message: str | None = Field(default=None, max_length=300)


class DeliveryDetails(ClosedModel):
    """Краткий operator/machine результат delivery."""

    phase: DeliveryPhase
    target_paths: tuple[str, ...] = Field(max_length=128)
    target_count: int = Field(default=0, ge=0, le=128)
    changes: tuple[DeliveryChange, ...] = Field(default_factory=tuple, max_length=128)
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    remote_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    recovery_required: bool = False


class DeliveryEvidence(ClosedModel):
    snapshot: GitSnapshot
    commit: CommitIdentity | None = None
    scans: tuple[AnalysisScope, ...] = Field(default_factory=tuple, max_length=4)
    publication_remote: RemoteIdentity | None = None
    base_remote: RemoteIdentity | None = None
    branch: BranchIdentity | None = None


class PullRequestIdentity(ClosedModel):
    """Полная repository-qualified identity PR."""

    repository: RepositoryIdentity
    number: int = Field(ge=1)
    state: str = Field(min_length=1, max_length=32)
    draft: bool
    base_repository: RepositoryIdentity
    base_ref: str = Field(min_length=1, max_length=256)
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    head_repository: RepositoryIdentity
    head_ref: str = Field(min_length=1, max_length=256)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class CodeRabbitFinding(ClosedModel):
    """Нормализованный finding для durable PR disposition."""

    severity: FindingSeverity
    path: str = Field(min_length=1, max_length=512)
    impact: str = Field(min_length=1, max_length=1200)
    disposition: FindingDisposition
    resolution: str = Field(min_length=1, max_length=1200)
    fix_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")


class CodeRabbitReview(ClosedModel):
    """Evidence CodeRabbit, включая явный zero/rate-limit результат."""

    reviewed_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    findings: tuple[CodeRabbitFinding, ...] = Field(max_length=128)
    rate_limit: str | None = Field(default=None, max_length=500)


class PullRequestBody(ClosedModel):
    """Структурированное тело PR с обязательными durable разделами."""

    goal: str = Field(min_length=1, max_length=4000)
    scope: str = Field(min_length=1, max_length=4000)
    implementation: str = Field(min_length=1, max_length=8000)
    checks: str = Field(min_length=1, max_length=8000)
    ci: str = Field(min_length=1, max_length=4000)
    security_secret_scan: str = Field(min_length=1, max_length=4000)
    coderabbit_review: CodeRabbitReview | None = None
    migration_rollback: str = Field(min_length=1, max_length=4000)
    limitations: str = Field(min_length=1, max_length=4000)
    merge_method: Literal["squash", "merge", "rebase"] = "squash"


class PrPublicationSpec(ClosedModel):
    """Immutable closed-schema request для create/update draft PR."""

    schema_version: Literal[1] = 1
    repository: RepositoryIdentity
    base_ref: str = Field(min_length=1, max_length=256)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    head_ref: str = Field(min_length=1, max_length=256)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    remote_name: str = Field(default="origin", min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    draft: bool = True
    body: PullRequestBody
    pr_number: int | None = Field(default=None, ge=1)


class PrPreparationDetails(ClosedModel):
    """Подтверждённая read-only подготовка PR без provider mutation."""

    repository: RepositoryIdentity
    base_ref: str = Field(min_length=1, max_length=256)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    head_ref: str = Field(min_length=1, max_length=256)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PullRequestDetails(ClosedModel):
    identity: PullRequestIdentity
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PullRequestEvidence(ClosedModel):
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_sections: tuple[str, ...] = Field(min_length=1, max_length=16)


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
    mcp_reconciliation: Literal["not_required", "ready", "restarted", "failed"] = "not_required"
    mcp_restarted_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)
    mcp_session_state: Literal["not_observable", "reload_required"] = "not_observable"
    mcp_reload_required: bool = False


class McpFlag(ClosedModel):
    """Одна bounded capability flag в operator evidence."""

    name: str = Field(min_length=1, max_length=128)
    value: bool


class McpDigest(ClosedModel):
    """Именованный SHA-256 component digest."""

    name: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class McpServerStatus(ClosedModel):
    """Transport-neutral status одной first-party backend family."""

    server_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    expected_version: str = Field(min_length=5, max_length=128)
    observed_version: str | None = Field(default=None, max_length=128)
    source_revision: str | None = Field(
        default=None, pattern=r"^(unknown|[0-9a-f]{7,64})$"
    )
    status: Literal[
        "ready",
        "stale",
        "stopped",
        "unavailable",
        "not_configured",
        "unknown",
        "conflict",
    ]
    source_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    routes: tuple[Literal["stdio", "loopback_http", "public_https"], ...] = Field(
        default_factory=tuple, max_length=3
    )
    reason_code: str | None = Field(default=None, max_length=128)


class McpStatusDetails(ClosedModel):
    """Сводка source/runtime/plugin/session слоёв MCP."""

    action: Literal["status", "reconcile", "start", "stop", "restart"]
    source_state: Literal["ready", "drift", "invalid", "unknown"]
    runtime_state: Literal["ready", "stale", "stopped", "unknown", "conflict"]
    plugin_state: Literal["ready", "drift", "invalid", "unknown"]
    session_state: Literal["current", "reload_required", "not_observable", "unknown"]
    bundle_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    plugin_version: str = Field(min_length=5, max_length=128)
    skill_bundle_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    servers: tuple[McpServerStatus, ...] = Field(min_length=1, max_length=2)
    component_digests: tuple[McpDigest, ...] = Field(max_length=32)
    changed_components: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    affected_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)
    restarted_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)
    reload_required: bool = False


class McpVersionDetails(ClosedModel):
    """Полная bounded version/revision сводка canonical bundle."""

    action: Literal["versions"] = "versions"
    bundle_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    plugin_version: str = Field(min_length=5, max_length=128)
    skill_bundle_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    servers: tuple[McpServerStatus, ...] = Field(min_length=1, max_length=2)
    component_digests: tuple[McpDigest, ...] = Field(max_length=32)


class McpLifecycleDetails(ClosedModel):
    """Bounded evidence lifecycle-операции loopback supervisor."""

    action: Literal["start", "stop", "restart"]
    supervisor_code: str = Field(min_length=1, max_length=128)
    services: tuple[McpServerStatus, ...] = Field(min_length=1, max_length=2)
    ownership_confirmed: bool
    readiness_confirmed: bool


class McpReconcileDetails(ClosedModel):
    """Результат source или runtime reconciliation без свободного payload."""

    action: Literal["reconcile"] = "reconcile"
    mode: Literal["source", "runtime"]
    source_state: Literal["ready", "drift", "invalid", "unknown"]
    runtime_state: Literal["ready", "stale", "stopped", "unknown", "conflict"]
    mutation_performed: bool
    changed_components: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    affected_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)
    restarted_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)
    session_state: Literal["current", "reload_required", "not_observable", "unknown"]
    reload_required: bool = False


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
        ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
        ResultCode.TOOLING_REMOTE_REF_CONFLICT,
        ResultCode.TOOLING_PR_IDENTITY_MISMATCH,
        ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
        ResultCode.TOOLING_CLEANUP_UNKNOWN,
        ResultCode.MCP_RUNTIME_STALE,
        ResultCode.MCP_PLUGIN_RUNTIME_INCOMPATIBLE,
        ResultCode.MCP_RELOAD_REQUIRED,
    }:
        return ExitCode.OWNERSHIP_CONFLICT
    if code in {
        ResultCode.TOOLING_TIMEOUT,
        ResultCode.TOOLING_CANCELLED,
        ResultCode.TOOLING_PUSH_UNKNOWN,
        ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN,
    }:
        return ExitCode.TIMEOUT
    if code in {
        ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
        ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
        ResultCode.TOOLING_CAPABILITY_UNSUPPORTED,
        ResultCode.TOOLING_SECRET_SCANNER_UNAVAILABLE,
        ResultCode.TOOLING_PROVIDER_UNAVAILABLE,
        ResultCode.TOOLING_BACKUP_REQUIRED,
        ResultCode.TOOLING_BACKUP_FAILED,
        ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
        ResultCode.TOOLING_ADB_FAILED,
        ResultCode.TOOLING_SHORTCUT_FAILED,
        ResultCode.MCP_AUTH_NOT_CONFIGURED,
        ResultCode.MCP_RUNTIME_UNAVAILABLE,
    }:
        return ExitCode.DEPENDENCY_UNAVAILABLE
    if code is ResultCode.TOOLING_APPLY_FAILED_ROLLED_BACK:
        return ExitCode.ROLLED_BACK
    if code in {
        ResultCode.MCP_SOURCE_BUNDLE_INVALID,
        ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
        ResultCode.MCP_VERSION_BUMP_REQUIRED,
    }:
        return ExitCode.PRECONDITION
    if code in {
        ResultCode.TOOLING_ROLLBACK_UNKNOWN,
        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
        ResultCode.TOOLING_SECRET_SCAN_FAILED,
        ResultCode.MCP_ENVIRONMENT_STALE,
    }:
        return ExitCode.ROLLBACK_UNKNOWN
    if code is ResultCode.TOOLING_UNEXPECTED:
        return ExitCode.UNEXPECTED
    return ExitCode.PRECONDITION


__all__ = [
    "AnalysisScope",
    "BranchIdentity",
    "BuildDetails",
    "BuildEvidence",
    "CapabilityCheck",
    "CapabilityStatus",
    "ClosedModel",
    "CodeRabbitFinding",
    "CodeRabbitReview",
    "CommitIdentity",
    "DeliveryChange",
    "DeliveryDetails",
    "DeliveryEvidence",
    "DeliveryJournal",
    "DeliveryManifest",
    "DeliveryPhase",
    "DeliveryTarget",
    "DoctorDetails",
    "DoctorEvidence",
    "ExitCode",
    "FileState",
    "FindingDisposition",
    "FindingSeverity",
    "GitEvidence",
    "GitRange",
    "GitSnapshot",
    "LifecycleDetails",
    "LifecycleEvidence",
    "LifecycleRecord",
    "McpDigest",
    "McpFlag",
    "McpLifecycleDetails",
    "McpReconcileDetails",
    "McpServerStatus",
    "McpStatusDetails",
    "McpVersionDetails",
    "OperationState",
    "PostgreSqlBackupEvidence",
    "PrPreparationDetails",
    "PrPublicationSpec",
    "ProcessEvidence",
    "PublicationIntent",
    "PullRequestBody",
    "PullRequestDetails",
    "PullRequestEvidence",
    "PullRequestIdentity",
    "RemoteIdentity",
    "RemoteIdentityEvidence",
    "RepairDetails",
    "RepairEvidence",
    "RepositoryDetails",
    "RepositoryIdentity",
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
