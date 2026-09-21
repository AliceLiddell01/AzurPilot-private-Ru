"""Закрытые DTO и стабильные коды Python tooling.

Эти модели являются границей между сервисами, CLI и будущими transport adapters.
Свободные словари намеренно не используются в operation payload: добавление
неизвестного поля должно быть заметно в тестах и при чтении машинного вывода.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .mcp_contracts import ProcessEvidence
from .result import ExitCode, OperationState, ResultCode, exit_code_for


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
    """Допустимая классификация после независимого triage внешнего review."""

    CONFIRMED = "confirmed"
    PARTIALLY_CONFIRMED = "partially confirmed"
    FALSE_POSITIVE = "false positive"


class CodeRabbitConflictKind(StrEnum):
    """Единственные основания отклонить применимый CodeRabbit finding."""

    REPOSITORY_CONTRACT_CONFLICT = "repository_contract_conflict"
    TASK_PROMPT_CONFLICT = "task_prompt_conflict"
    DEPENDENCY_VERSION_CONFLICT = "dependency_version_conflict"


class WarningCode(StrEnum):
    """Ограниченные предупреждения, не меняющие основной код результата."""

    TOOLING_ADB_NOT_CONFIGURED = "TOOLING_ADB_NOT_CONFIGURED"
    TOOLING_CLI_NOT_ON_PATH = "TOOLING_CLI_NOT_ON_PATH"
    TOOLING_SHORTCUT_UNSUPPORTED = "TOOLING_SHORTCUT_UNSUPPORTED"
    TOOLING_POSTGRES_UNAVAILABLE = "TOOLING_POSTGRES_UNAVAILABLE"
    TOOLING_REDIS_UNAVAILABLE = "TOOLING_REDIS_UNAVAILABLE"
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

    @model_validator(mode="after")
    def validate_mode(self) -> AnalysisScope:
        if self.mode == "committed_range" and self.git_range is None:
            raise ValueError("committed_range требует exact Git range")
        if self.mode == "staged" and self.git_range is not None:
            raise ValueError("staged не принимает Git range")
        return self


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


class DockerDeploymentDetails(ClosedModel):
    """Типизированный результат явного развёртывания образа/контейнера Docker."""

    action: Literal["deploy"] = "deploy"
    image: str = Field(min_length=1, max_length=256)
    container: str = Field(min_length=1, max_length=128)
    port: int = Field(ge=1, le=65535)
    source: str = Field(min_length=1, max_length=80)
    build_confirmed: bool
    container_started: bool
    readiness_confirmed: bool
    replace_performed: bool = False
    runtime_secret_mode: Literal[
        "not_configured",
        "readonly_env_file",
        "readonly_backend_marker",
        "readonly_env_and_backend_marker",
    ] = "not_configured"
    postgres_compose_project: str = "azurpilot-infrastructure"
    postgres_compose_service: str = "postgres"
    postgres_network: str = Field(default="", max_length=256)
    postgres_endpoint: Literal["postgres:5432"] = "postgres:5432"
    redis_compose_project: str = "azurpilot-infrastructure"
    redis_compose_service: str = "redis"
    redis_network: str = Field(default="", max_length=256)
    redis_endpoint: Literal["redis:6379"] = "redis:6379"


class DockerDeploymentEvidence(ClosedModel):
    """Ограниченное evidence развёртывания без секретов и public-IP discovery."""

    docker_cli: str = Field(min_length=1, max_length=80)
    capability: CapabilityStatus
    image: str = Field(min_length=1, max_length=256)
    container: str = Field(min_length=1, max_length=128)
    readiness_probe: str = Field(min_length=1, max_length=80)
    runtime_secret_mode: Literal[
        "not_configured",
        "readonly_env_file",
        "readonly_backend_marker",
        "readonly_env_and_backend_marker",
    ] = "not_configured"
    postgres_compose_project: str = "azurpilot-infrastructure"
    postgres_compose_service: str = "postgres"
    postgres_network: str = Field(default="", max_length=256)
    postgres_endpoint: Literal["postgres:5432"] = "postgres:5432"
    redis_compose_project: str = "azurpilot-infrastructure"
    redis_compose_service: str = "redis"
    redis_network: str = Field(default="", max_length=256)
    redis_endpoint: Literal["redis:6379"] = "redis:6379"


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


class CodeRabbitFindingTriage(ClosedModel):
    """Индивидуальное доказательство проверки одного provider finding."""

    disposition: FindingDisposition
    reviewed_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    affected_code: str = Field(min_length=1, max_length=1200)
    call_sites: str = Field(min_length=1, max_length=1200)
    nearest_tests: str = Field(min_length=1, max_length=1200)
    relevant_contracts: str = Field(min_length=1, max_length=1200)
    claimed_impact: str = Field(min_length=1, max_length=1200)
    decision_reason: str = Field(min_length=12, max_length=2000)
    change_summary: str = Field(min_length=12, max_length=2000)
    conflict_kind: CodeRabbitConflictKind | None = None
    authoritative_source: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def validate_decision_evidence(self) -> CodeRabbitFindingTriage:
        placeholder_values = {
            "false positive",
            "insufficient evidence",
            "not confirmed",
            "не подтверждено",
            "недостаточно данных",
            "индивидуальная проверка выполнена",
            "изменений не требуется",
        }
        normalized_reason = " ".join(self.decision_reason.casefold().split())
        normalized_change = " ".join(self.change_summary.casefold().split())
        if normalized_reason in placeholder_values or normalized_change in placeholder_values:
            raise ValueError("triage evidence не может быть placeholder-only объяснением")
        if self.disposition is FindingDisposition.FALSE_POSITIVE:
            if self.conflict_kind is None:
                raise ValueError(
                    "false positive требует typed repository/task/dependency conflict"
                )
            if not self.authoritative_source or len(self.authoritative_source.strip()) < 8:
                raise ValueError(
                    "conflict rejection требует authoritative source"
                )
        elif self.conflict_kind is not None or self.authoritative_source is not None:
            raise ValueError(
                "conflict evidence допустимо только для false positive rejection"
            )
        return self


class CodeRabbitFinding(ClosedModel):
    """Provider finding с отдельным optional verified triage."""

    severity: FindingSeverity
    path: str = Field(min_length=1, max_length=512)
    title: str | None = Field(default=None, max_length=160)
    line: int | None = Field(default=None, ge=1, le=10_000_000)
    line_end: int | None = Field(default=None, ge=1, le=10_000_000)
    impact: str = Field(min_length=1, max_length=1200)
    disposition: FindingDisposition | None = None
    resolution: str = Field(min_length=1, max_length=4000)
    codegen_instructions: str | None = Field(default=None, max_length=4000)
    suggestions: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    fix_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    triage: CodeRabbitFindingTriage | None = None

    @model_validator(mode="after")
    def validate_line_range(self) -> CodeRabbitFinding:
        if self.line is not None and self.line_end is not None and self.line_end < self.line:
            raise ValueError("line_end не может быть меньше line")
        if self.triage is None and self.disposition is not None:
            raise ValueError("provider finding не может иметь disposition без triage evidence")
        if self.triage is not None and self.disposition is not self.triage.disposition:
            raise ValueError("finding disposition должен совпадать с verified triage")
        return self


class CodeRabbitTriageEntry(ClosedModel):
    """Одна запись manifest-а triage, адресованная по порядковому индексу."""

    index: int = Field(ge=1, le=128)
    triage: CodeRabbitFindingTriage
    fix_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")

    @model_validator(mode="after")
    def validate_fix_head_owner(self) -> CodeRabbitTriageEntry:
        if (
            self.triage.disposition is FindingDisposition.FALSE_POSITIVE
            and self.fix_head is not None
        ):
            raise ValueError("rejected conflict finding не должен иметь fix_head")
        return self


class CodeRabbitTriageManifest(ClosedModel):
    """Закрытый manifest индивидуального CodeRabbit triage."""

    schema_version: Literal[1] = 1
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    reviewed_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    findings: tuple[CodeRabbitTriageEntry, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def validate_unique_indices(self) -> CodeRabbitTriageManifest:
        indices = tuple(entry.index for entry in self.findings)
        if len(indices) != len(set(indices)):
            raise ValueError("triage manifest содержит дублирующиеся finding indices")
        return self


class CodeRabbitReview(ClosedModel):
    """Evidence CodeRabbit, включая append-only историю итераций."""

    reviewed_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    findings: tuple[CodeRabbitFinding, ...] = Field(max_length=128)
    history: str | None = Field(default=None, max_length=20_000)
    rate_limit: str | None = Field(default=None, max_length=500)


class MandatoryGateState(StrEnum):
    """Terminal semantic state обязательного product/verification gate."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED_PRECONDITION = "BLOCKED_PRECONDITION"
    NOT_REQUIRED = "NOT_REQUIRED"


FRESH_MCP_ACCEPTANCE_GATE_NAME = "fresh_mcp_task_acceptance"


class MandatoryGate(ClosedModel):
    """Один обязательный или неприменимый gate с bounded evidence."""

    name: str = Field(min_length=1, max_length=80)
    state: MandatoryGateState
    required: bool = True
    evidence: str = Field(min_length=1, max_length=1000)
    evidence_kind: Literal[
        "source", "runtime", "delegated_fresh_task", "other"
    ] = "other"

    @model_validator(mode="after")
    def validate_required_state(self) -> MandatoryGate:
        if not self.required and self.state is not MandatoryGateState.NOT_REQUIRED:
            raise ValueError("необязательный gate должен иметь state NOT_REQUIRED")
        if self.name == FRESH_MCP_ACCEPTANCE_GATE_NAME and (
            not self.required or self.state is MandatoryGateState.NOT_REQUIRED
        ):
            raise ValueError(
                "fresh MCP task acceptance не может быть NOT_REQUIRED"
            )
        return self


class ReadinessState(ClosedModel):
    """Непротиворечивое состояние реализации, gates и lifecycle readiness."""

    implementation_status: Literal["IN_PROGRESS", "COMPLETE", "BLOCKED"] = "IN_PROGRESS"
    mandatory_gates: tuple[MandatoryGate, ...] = Field(default_factory=tuple, max_length=32)
    mcp_impact: Literal["NOT_REQUIRED", "REQUIRED"] | None = None
    external_reviewer_status: Literal[
        "NOT_RUN", "SUBSTANTIVE", "LIMITED", "RATE_LIMITED"
    ] = "NOT_RUN"
    reviewer_limitation: str | None = Field(default=None, max_length=1000)
    overall_outcome: Literal["IN_PROGRESS", "BLOCKED", "READY"] = "IN_PROGRESS"
    ready_for_chatgpt_review: bool = False
    merge_ready: bool = False

    @model_validator(mode="after")
    def validate_lifecycle(self) -> ReadinessState:
        gate_names = tuple(gate.name for gate in self.mandatory_gates)
        if len(gate_names) != len(set(gate_names)):
            raise ValueError("mandatory gates должны иметь уникальные имена")
        fresh_gates = tuple(
            gate
            for gate in self.mandatory_gates
            if gate.name == FRESH_MCP_ACCEPTANCE_GATE_NAME
        )
        if self.mcp_impact == "REQUIRED":
            if len(fresh_gates) != 1:
                raise ValueError(
                    "MCP impact REQUIRED требует ровно один mandatory fresh MCP gate"
                )
            fresh_gate = fresh_gates[0]
            if fresh_gate.state is MandatoryGateState.NOT_REQUIRED:
                raise ValueError(
                    "MCP impact REQUIRED запрещает NOT_REQUIRED для fresh MCP gate"
                )
            if (
                fresh_gate.state is MandatoryGateState.PASS
                and fresh_gate.evidence_kind != "delegated_fresh_task"
            ):
                raise ValueError(
                    "PASS fresh MCP gate требует evidence новой independent task"
                )
        blocking = any(
            gate.required
            and gate.state
            in {MandatoryGateState.FAIL, MandatoryGateState.BLOCKED_PRECONDITION}
            for gate in self.mandatory_gates
        )
        if blocking and (
            self.overall_outcome != "BLOCKED"
            or self.ready_for_chatgpt_review
            or self.merge_ready
        ):
            raise ValueError(
                "FAIL/BLOCKED_PRECONDITION mandatory gate требует overall_outcome BLOCKED "
                "и запрещает readiness/merge"
            )
        if self.merge_ready and not self.ready_for_chatgpt_review:
            raise ValueError("merge_ready требует ready_for_chatgpt_review")
        if self.overall_outcome == "READY" and (
            self.implementation_status != "COMPLETE"
            or not self.ready_for_chatgpt_review
            or any(
                gate.required
                and gate.state not in {MandatoryGateState.PASS, MandatoryGateState.NOT_REQUIRED}
                for gate in self.mandatory_gates
            )
        ):
            raise ValueError("READY требует complete implementation и PASS/NOT_REQUIRED gates")
        if self.external_reviewer_status in {"LIMITED", "RATE_LIMITED"} and not self.reviewer_limitation:
            raise ValueError("ограничение внешнего reviewer требует reviewer_limitation")
        return self


class PullRequestBody(ClosedModel):
    """Структурированное тело PR с обязательными durable разделами."""

    goal: str = Field(min_length=1, max_length=4000)
    scope: str = Field(min_length=1, max_length=4000)
    implementation: str = Field(min_length=1, max_length=8000)
    checks: str = Field(min_length=1, max_length=8000)
    ci: str = Field(min_length=1, max_length=4000)
    security_secret_scan: str = Field(min_length=1, max_length=4000)
    coderabbit_review: CodeRabbitReview | None = None
    readiness: ReadinessState = Field(default_factory=ReadinessState)
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


class IntegrationSummary(ClosedModel):
    """Внешняя integration summary, добавляемая read-only Doctor."""

    name: str = Field(min_length=1, max_length=80)
    status: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    route: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)


class DoctorDetails(ClosedModel):
    checks: tuple[CapabilityCheck, ...] = Field(max_length=32)
    healthy: bool
    external_integrations: tuple[IntegrationSummary, ...] = Field(
        default_factory=tuple, max_length=6
    )


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
    mcp_session_state: Literal[
        "current", "not_observable", "reload_required", "unknown"
    ] = "not_observable"
    mcp_reload_required: bool = False


class McpDigest(ClosedModel):
    """Именованный SHA-256 component digest."""

    name: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class McpImpactPath(ClosedModel):
    """Одна path-to-source-set связь effective candidate diff."""

    path: str = Field(min_length=1, max_length=512)
    source_sets: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    affected_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)


class McpImpactDetails(ClosedModel):
    """Read-only классификация MCP impact до публикации candidate."""

    action: Literal["impact"] = "impact"
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    status: Literal["NOT_REQUIRED", "REQUIRED"]
    candidate_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=512)
    committed_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=512)
    working_tree_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=512)
    path_impacts: tuple[McpImpactPath, ...] = Field(default_factory=tuple, max_length=512)
    changed_components: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    affected_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)
    generated_artifacts: tuple[str, ...] = Field(default_factory=tuple, max_length=3)
    reconciliation_required: bool

    @property
    def fresh_acceptance_required(self) -> bool:
        """Механическая связь impact classification с mandatory acceptance."""

        return self.status == "REQUIRED"


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
    source_reconciled: bool
    runtime_ready: bool
    plugin_state: Literal["ready", "drift", "invalid", "unknown"]
    plugin_source_state: Literal["ready", "drift", "unknown"]
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
    """Раздельный результат source reconciliation и live runtime readiness."""

    action: Literal["reconcile"] = "reconcile"
    mode: Literal["source", "runtime"]
    source_state: Literal["ready", "drift", "invalid", "unknown"]
    runtime_state: Literal["ready", "stale", "stopped", "unknown", "conflict"]
    source_reconciled: bool
    runtime_ready: bool
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


__all__ = [
    "FRESH_MCP_ACCEPTANCE_GATE_NAME",
    "AnalysisScope",
    "BranchIdentity",
    "BuildDetails",
    "BuildEvidence",
    "CapabilityCheck",
    "CapabilityStatus",
    "ClosedModel",
    "CodeRabbitConflictKind",
    "CodeRabbitFinding",
    "CodeRabbitFindingTriage",
    "CodeRabbitReview",
    "CodeRabbitTriageEntry",
    "CodeRabbitTriageManifest",
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
    "IntegrationSummary",
    "LifecycleDetails",
    "LifecycleEvidence",
    "LifecycleRecord",
    "MandatoryGate",
    "MandatoryGateState",
    "McpDigest",
    "McpImpactDetails",
    "McpImpactPath",
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
    "ReadinessState",
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
