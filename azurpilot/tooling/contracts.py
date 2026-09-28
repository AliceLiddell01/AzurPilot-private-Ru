"""Закрытые DTO и стабильные коды инструментов Python.

Эти модели являются границей между сервисами, CLI и будущими транспортными адаптерами.
Свободные словари намеренно не используются в данных операции: добавление
неизвестного поля должно быть заметно в тестах и при чтении машинного вывода.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from module.mcp_shared.windows_mcp_bridge_contract import BridgeSourceIdentity

from .mcp_contracts import ProcessEvidence
from .result import ExitCode, OperationState, ResultCode, exit_code_for


class CapabilityStatus(StrEnum):
    """Состояние необязательной возможности."""

    READY = "ready"
    NOT_CHECKED = "not_checked"
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


class GitRange(ClosedModel):
    """Точный диапазон Git для ограниченного анализа."""

    start_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    end_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class AnalysisScope(ClosedModel):
    """Список разрешённых путей и/или точный диапазон Git для анализатора безопасности."""

    paths: tuple[str, ...] = Field(default_factory=tuple, max_length=128)
    git_range: GitRange | None = None
    mode: Literal["staged", "committed_range"]

    @model_validator(mode="after")
    def validate_mode(self) -> AnalysisScope:
        if self.mode == "committed_range" and self.git_range is None:
            raise ValueError("committed_range требует точный диапазон Git")
        if self.mode == "staged" and self.git_range is not None:
            raise ValueError("staged не принимает диапазон Git")
        return self


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
    """Ограниченные сведения о развёртывании без секретов и обнаружения публичного IP."""

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
    """Внешняя сводка интеграций, добавляемая только для чтения Doctor."""

    name: str = Field(min_length=1, max_length=80)
    status: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,31}$")
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    route: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)


class DoctorDetails(ClosedModel):
    checks: tuple[CapabilityCheck, ...] = Field(max_length=32)
    healthy: bool
    external_integrations: tuple[IntegrationSummary, ...] = Field(
        default_factory=tuple, max_length=5
    )


class DoctorEvidence(ClosedModel):
    repository: RepositoryRootEvidence
    python_version: str = Field(min_length=1, max_length=32)
    platform: str = Field(min_length=1, max_length=80)


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
    """Внутренний файл состояния жизненного цикла, содержащий полную идентичность владения."""

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


class BotRuntimeWorker(ClosedModel):
    profile: str = Field(min_length=1, max_length=128)
    pid: int = Field(ge=1)
    created_at: float = Field(gt=0)
    running: bool


class BotRuntimeDetails(ClosedModel):
    status: OperationState
    owner_pid: int | None = Field(default=None, ge=1)
    owner_created_at: float | None = Field(default=None, gt=0)
    owner_running: bool = False
    workers: tuple[BotRuntimeWorker, ...] = Field(default_factory=tuple, max_length=128)
    recovery_required: bool = False


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


class McpDigest(ClosedModel):
    """Именованный SHA-256 компонента."""

    name: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class McpImpactPath(ClosedModel):
    """Связь одного пути в итоговом сравнении изменений с набором исходников."""

    path: str = Field(min_length=1, max_length=512)
    source_sets: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    affected_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)


class McpImpactDetails(ClosedModel):
    """Классификация влияния на MCP до публикации варианта, только для чтения."""

    action: Literal["impact"] = "impact"
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    status: Literal["NOT_REQUIRED", "REQUIRED"]
    candidate_path_count: int = Field(default=0, ge=0)
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
        """Связь классификации влияния с обязательной приёмкой."""

        return self.status == "REQUIRED"


class McpServerStatus(ClosedModel):
    """Не зависящее от транспорта состояние одного семейства серверов проекта."""

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
    authentication: Literal["configured", "unavailable", "unknown"] = "unknown"
    ownership_confirmed: bool = False
    reason_code: str | None = Field(default=None, max_length=128)


class McpStatusDetails(ClosedModel):
    """Сводка по исходникам, среде выполнения, плагину и клиентской сессии MCP."""

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
    """Полная ограниченная сводка версий и ревизий канонического пакета."""

    action: Literal["versions"] = "versions"
    bundle_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    plugin_version: str = Field(min_length=5, max_length=128)
    skill_bundle_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    servers: tuple[McpServerStatus, ...] = Field(min_length=1, max_length=2)
    component_digests: tuple[McpDigest, ...] = Field(max_length=32)


class McpLifecycleDetails(ClosedModel):
    """Ограниченные сведения об операции жизненного цикла локального процесса управления MCP."""

    action: Literal["start", "stop", "restart"]
    supervisor_code: str = Field(min_length=1, max_length=128)
    services: tuple[McpServerStatus, ...] = Field(min_length=1, max_length=2)
    ownership_confirmed: bool
    readiness_confirmed: bool


class McpAcceptanceDetails(ClosedModel):
    """Результат одной новой клиентской сессии MCP в заданных пределах, только для чтения."""

    action: Literal["accept"] = "accept"
    acceptance_state: Literal["READY", "INCOMPATIBLE", "UNAVAILABLE", "UNKNOWN"]
    reason_code: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    initialized: bool = False
    protocol_version: str | None = Field(default=None, max_length=80)
    server_name: str | None = Field(default=None, max_length=128)
    server_version: str | None = Field(default=None, max_length=128)
    source_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    tool_count: int | None = Field(default=None, ge=0, le=256)
    tool_catalog_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    capability_catalog_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    contract_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    called_tools: tuple[str, ...] = Field(default_factory=tuple, max_length=256)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple, max_length=16)

    @field_validator("called_tools")
    @classmethod
    def validate_called_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", item) is None
            for item in value
        ):
            raise ValueError("called_tools содержит повтор или недопустимое имя")
        return value

    @field_validator("diagnostics")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(item) > 240 or any(ord(char) < 32 for char in item) for item in value):
            raise ValueError("diagnostics содержит слишком длинный или управляющий текст")
        return value


class McpBridgeProcessStatus(ClosedModel):
    """Сведения о подтверждённом владении процессом моста для Windows без путей."""

    supervisor_pid: int | None = Field(default=None, ge=1)
    process_pid: int | None = Field(default=None, ge=1)
    ownership_confirmed: bool = False


class McpBridgeUpstreamStatus(ClosedModel):
    """Ограниченные сведения о готовности и идентичности целевого сервера."""

    route: str = Field(min_length=1, max_length=32)
    server_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    status: Literal["ready", "stale", "stopped", "unavailable", "unknown", "conflict"]
    identity: BridgeSourceIdentity | None = None
    reason_code: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_canonical_route(self) -> McpBridgeUpstreamStatus:
        from module.mcp_shared import windows_mcp_bridge_contract

        canonical_route = windows_mcp_bridge_contract.BRIDGE_ROUTES.get(self.route)
        if (
            canonical_route is None
            or canonical_route.server_name != self.server_name
            or (
                self.identity is not None
                and self.identity.server_name != self.server_name
            )
        ):
            raise ValueError("route и server_name должны соответствовать каноническому маршруту моста")
        return self


class McpBridgeStatusDetails(ClosedModel):
    """Типизированное состояние отдельной возможности моста для Windows."""

    action: Literal["status", "start", "stop", "restart"]
    state: Literal["ready", "stale", "stopped", "unknown", "conflict"]
    endpoint: str = Field(min_length=1, max_length=512)
    routes: tuple[str, ...] = Field(min_length=1, max_length=16)
    caller_authentication: Literal["configured", "unavailable", "unknown"]
    process: McpBridgeProcessStatus
    upstreams: tuple[McpBridgeUpstreamStatus, ...] = Field(min_length=1, max_length=16)
    reason_code: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_canonical_bridge_contract(self) -> McpBridgeStatusDetails:
        from module.mcp_shared import windows_mcp_bridge_contract

        routes = windows_mcp_bridge_contract.BRIDGE_ROUTES
        canonical_paths = tuple(route.path for route in routes.values())
        canonical_upstreams = tuple(
            (family, route.server_name) for family, route in routes.items()
        )
        actual_upstreams = tuple(
            (upstream.route, upstream.server_name) for upstream in self.upstreams
        )
        if (
            self.endpoint != windows_mcp_bridge_contract.bridge_endpoint()
            or self.routes != canonical_paths
            or actual_upstreams != canonical_upstreams
        ):
            raise ValueError("Сведения должны соответствовать каноническому контракту моста")
        return self


class McpBridgeAcceptanceDetails(ClosedModel):
    """Результат новой клиентской приёмки через оба фиксированных маршрута моста Windows."""

    action: Literal["accept"] = "accept"
    acceptance_state: Literal["READY", "INCOMPATIBLE", "UNAVAILABLE", "UNKNOWN"]
    reason_code: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    routes: tuple[McpAcceptanceDetails, ...] = Field(min_length=1, max_length=16)
    modern_routes: tuple[McpAcceptanceDetails, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_route_acceptance(self) -> McpBridgeAcceptanceDetails:
        from module.mcp_shared import windows_mcp_bridge_contract

        expected_server_names = tuple(
            route.server_name
            for route in windows_mcp_bridge_contract.BRIDGE_ROUTES.values()
        )
        for mode_results in (self.routes, self.modern_routes):
            actual_server_names = tuple(
                result.server_name for result in mode_results
            )
            if len(actual_server_names) != len(expected_server_names) or any(
                actual is not None and actual != expected
                for actual, expected in zip(
                    actual_server_names, expected_server_names, strict=True
                )
            ):
                raise ValueError("Результаты должны соответствовать каноническим маршрутам моста")

        results = (*self.routes, *self.modern_routes)
        expected_state = (
            "READY"
            if all(item.acceptance_state == "READY" for item in results)
            else "INCOMPATIBLE"
            if any(item.acceptance_state == "INCOMPATIBLE" for item in results)
            else "UNKNOWN"
            if any(item.acceptance_state == "UNKNOWN" for item in results)
            else "UNAVAILABLE"
        )
        if self.acceptance_state != expected_state:
            raise ValueError("Состояние приёмки должно соответствовать результатам маршрутов")
        return self


class CommissionRecoveryProjection(ClosedModel):
    """Штатное состояние приложения только для чтения, без представления из WebUI."""

    state_id: Literal["commission/recovery"] = "commission/recovery"
    profile: str = Field(min_length=1, max_length=128)
    status: Literal["confirmed", "unknown", "unavailable"]
    remaining: int | None = Field(default=None, ge=0, le=5)
    used: int | None = Field(default=None, ge=0, le=5)
    next_oil_cost: int | None = Field(default=None, ge=0, le=100_000)
    next_ap_gain: int | None = Field(default=None, ge=0, le=10_000)
    confirmed_at: datetime | None = None
    reset_at: datetime
    source: Literal["game_ocr", "emergency_ap_purchase", "dorm_fallback"] | None = None
    last_result: str | None = Field(default=None, max_length=128)
    cache_status: str = Field(min_length=1, max_length=64)
    error: str | None = Field(default=None, max_length=128)


class ApplicationStateDetails(ClosedModel):
    """Результат общего запроса только для чтения к зарегистрированному состоянию приложения."""

    state_id: Literal["commission/recovery"] = "commission/recovery"
    value: CommissionRecoveryProjection


class McpReconcileDetails(ClosedModel):
    """Раздельный результат согласования исходников и готовности среды выполнения."""

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
    services: tuple[McpServerStatus, ...] = Field(default_factory=tuple, max_length=2)
    session_state: Literal["current", "reload_required", "not_observable", "unknown"]
    reload_required: bool = False


class McpSyncDetails(ClosedModel):
    """Итог синхронизации MCP-исходников, среды выполнения и клиента с учётом базы."""

    action: Literal["sync"] = "sync"
    terminal: Literal["NO_CHANGES", "SYNCED", "FAILED"]
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    impact: McpImpactDetails
    changed_components: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    affected_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)
    generated_artifacts: tuple[str, ...] = Field(default_factory=tuple, max_length=3)
    runtime: McpReconcileDetails | None = None
    acceptance: McpAcceptanceDetails | None = None


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
    """Общий закрытый конверт результата для человекочитаемых и машинных адаптеров."""

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
    "AnalysisScope",
    "ApplicationStateDetails",
    "BotRuntimeDetails",
    "BotRuntimeWorker",
    "BuildDetails",
    "BuildEvidence",
    "CapabilityCheck",
    "CapabilityStatus",
    "ClosedModel",
    "CommissionRecoveryProjection",
    "DoctorDetails",
    "DoctorEvidence",
    "ExitCode",
    "GitRange",
    "IntegrationSummary",
    "LifecycleDetails",
    "LifecycleEvidence",
    "LifecycleRecord",
    "McpAcceptanceDetails",
    "McpBridgeAcceptanceDetails",
    "McpBridgeProcessStatus",
    "McpBridgeStatusDetails",
    "McpBridgeUpstreamStatus",
    "McpDigest",
    "McpImpactDetails",
    "McpImpactPath",
    "McpLifecycleDetails",
    "McpReconcileDetails",
    "McpServerStatus",
    "McpStatusDetails",
    "McpSyncDetails",
    "McpVersionDetails",
    "OperationState",
    "PostgreSqlBackupEvidence",
    "ProcessEvidence",
    "RepairDetails",
    "RepairEvidence",
    "RepositoryDetails",
    "RepositoryRootEvidence",
    "ResultCode",
    "RootSource",
    "ToolingResult",
    "ToolingWarning",
    "TransactionJournal",
    "WarningCode",
    "exit_code_for",
]
