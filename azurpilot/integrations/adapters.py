"""Типизированные адаптеры Semgrep и прямых MCP-серверов."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from azurpilot.tooling.contracts import (
    AnalysisScope,
    CodeRabbitDeferredBacklog,
    ResultCode,
)
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import (
    ScopedPath,
    bounded_read_text,
    path_has_link,
)
from azurpilot.tooling.git import GitClient
from azurpilot.tooling.process import (
    INTEGRATION_CALLER_TOKEN_ENVIRONMENT_KEYS,
    ProcessSpec,
    StructuredProcessRunner,
)

from .config import IntegrationConfig
from .contracts import (
    CodeRabbitCycleSummary,
    CredentialRef,
    CredentialSource,
    IntegrationEvidence,
    IntegrationFinding,
    IntegrationName,
    IntegrationRecord,
    IntegrationState,
)
from .mcp_client import (
    LOOPBACK_HOSTS,
    HttpEndpointError,
    McpCallPlan,
    McpProbeResult,
    probe_http,
    validate_http_endpoint,
)

_VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_DOCKERHUB_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")
_MAX_SCAN_BYTES = 2 * 1024 * 1024
_MAX_FINDINGS = 128

GRAFANA_ENABLED_TOOL_CATEGORIES = (
    "search,datasource,prometheus,loki,dashboard,navigation,tempo"
)
GRAFANA_TEMPO_READ_ONLY_TOOLS = frozenset(
    {
        "diff_tempo_traces",
        "get_tempo_trace",
        "get_tempo_traceql_docs",
        "list_tempo_attribute_names",
        "list_tempo_attribute_values",
        "query_tempo_metrics",
        "search_tempo_traces",
    }
)
GRAFANA_EXPECTED_TOOL_NAMES = frozenset(
    {
        "analyze_loki_labels",
        "check_datasources_health",
        "generate_deeplink",
        "get_dashboard_by_uid",
        "get_dashboard_panel_queries",
        "get_dashboard_property",
        "get_dashboard_summary",
        "get_datasource",
        "list_dashboard_versions",
        "list_datasources",
        "list_loki_label_names",
        "list_loki_label_values",
        "list_prometheus_label_names",
        "list_prometheus_label_values",
        "list_prometheus_metric_metadata",
        "list_prometheus_metric_names",
        "query_loki_logs",
        "query_loki_patterns",
        "query_loki_stats",
        "query_prometheus",
        "query_prometheus_histogram",
        "search_dashboards",
        "search_folders",
        *GRAFANA_TEMPO_READ_ONLY_TOOLS,
    }
)

# Общий Grafana MCP HTTP service запускается с --disable-write и --disable-api,
# а его --enabled-tools ограничен read-only категориями. Поэтому negotiated
# catalog совпадает с полным allowlist-ом вызывающей стороны.
GRAFANA_READ_ONLY_TOOLS = GRAFANA_EXPECTED_TOOL_NAMES
GRAFANA_REQUIRED_READ_ONLY_TOOLS = frozenset(
    {
        "check_datasources_health",
        "list_datasources",
        "query_loki_logs",
        "query_prometheus",
    }
) | GRAFANA_TEMPO_READ_ONLY_TOOLS
GRAFANA_BLOCKED_TOOLS = frozenset(
    {
        "alerting_manage_routing",
        "alerting_manage_rules",
        "create_annotation",
        "create_datasource",
        "create_folder",
        "create_incident",
        "create_snapshot",
        "delete_snapshot",
        "grafana_api_request",
        "install_plugin",
        "update_annotation",
        "update_dashboard",
        "update_datasource",
    }
)
DOCKER_HUB_READ_ONLY_TOOLS = frozenset(
    {
        "checkRepository",
        "checkRepositoryTag",
        "dockerHardenedImages",
        "getPersonalNamespace",
        "getRepositoryInfo",
        "getRepositoryTag",
        "listAllNamespacesMemberOf",
        "listNamespaces",
        "listRepositoriesByNamespace",
        "listRepositoryTags",
        "search",
    }
)
DOCKER_HUB_BLOCKED_TOOLS = frozenset(
    {"createRepository", "updateRepositoryInfo"}
)


@dataclass(frozen=True, slots=True)
class AdapterOutcome:
    record: IntegrationRecord
    findings: tuple[IntegrationFinding, ...] = ()
    coderabbit_cycle: CodeRabbitCycleSummary | None = None
    coderabbit_backlog: CodeRabbitDeferredBacklog | None = None


class IntegrationAdapter:
    """Общий интерфейс; конкретный call policy находится в каждом адаптере."""

    name: IntegrationName

    def status(self, root: Path, config: IntegrationConfig) -> IntegrationRecord:
        raise NotImplementedError

    async def probe(self, root: Path, config: IntegrationConfig) -> AdapterOutcome:
        raise NotImplementedError


def _credential(config: dict[str, object], *, required: bool) -> CredentialRef:
    raw_name = config.get("credential_env")
    if (
        isinstance(raw_name, str)
        and _ENV_NAME_RE.fullmatch(raw_name)
        and os.environ.get(raw_name, "").strip()
    ):
        return CredentialRef(
            configured=True,
            source=CredentialSource.ENVIRONMENT,
            name=raw_name,
            auth_verified=None if not required else False,
        )
    raw_file = config.get("credential_file")
    if isinstance(raw_file, str) and raw_file.strip():
        path = Path(raw_file)
        if (
            path.is_absolute()
            and "\x00" not in raw_file
            and ".." not in path.parts
            and not path_has_link(path)
        ):
            try:
                value = bounded_read_text(path, max_bytes=16 * 1024).strip()
            except (OSError, UnicodeError, ToolingError):
                value = ""
            if value:
                safe_name = raw_name if isinstance(raw_name, str) else "credential_file"
                return CredentialRef(
                    configured=True,
                    source=CredentialSource.FILE,
                    name=safe_name,
                    auth_verified=None if not required else False,
                )
    return CredentialRef(
        configured=False,
        source=CredentialSource.NONE,
        name=raw_name if isinstance(raw_name, str) else None,
        auth_verified=None if not required else False,
    )


def _credential_value(
    config: dict[str, object], credential: CredentialRef
) -> str | None:
    """Прочитать credential только для конкретного child process."""

    if not credential.configured:
        return None
    if credential.source is CredentialSource.ENVIRONMENT and credential.name:
        value = os.environ.get(credential.name, "").strip()
        return value or None
    raw_file = config.get("credential_file")
    if credential.source is CredentialSource.FILE and isinstance(raw_file, str):
        path = Path(raw_file)
        if (
            not path.is_absolute()
            or "\x00" in raw_file
            or ".." in path.parts
            or path_has_link(path)
        ):
            return None
        try:
            value = bounded_read_text(path, max_bytes=16 * 1024).strip()
        except (OSError, UnicodeError, ToolingError):
            return None
        return value or None
    return None


def _executable(command: object) -> str | None:
    if not isinstance(command, str) or _SAFE_ID_RE.fullmatch(command) is None:
        return None
    return shutil.which(command) or shutil.which(f"{command}.exe")


def build_evidence(
    *,
    config: dict[str, object],
    credential: CredentialRef,
    configured: bool,
    reachable: bool = False,
    authenticated: bool | None = None,
    selected_tool: str | None = None,
    tool_count: int | None = None,
    blocked_tools: frozenset[str] = frozenset(),
    diagnostics: tuple[str, ...] = (),
    scope: AnalysisScope | None = None,
) -> IntegrationEvidence:
    endpoint = config.get("endpoint")
    image = config.get("image")
    command = config.get("command")
    return IntegrationEvidence(
        route=str(config.get("route", "direct")),
        transport=str(config.get("transport")) if config.get("transport") else None,
        endpoint=endpoint if isinstance(endpoint, str) else None,
        executable=command if isinstance(command, str) else None,
        image=image if isinstance(image, str) else None,
        configured=configured,
        reachable=reachable,
        authenticated=authenticated,
        read_only=True,
        tool_count=tool_count,
        selected_tool=selected_tool,
        blocked_write_tools=tuple(sorted(blocked_tools)),
        credential=credential,
        scope=scope,
        diagnostics=tuple(str(item)[:240] for item in diagnostics if item)[:16],
    )


def build_record(
    name: IntegrationName,
    state: IntegrationState,
    reason_code: str,
    message: str,
    evidence: IntegrationEvidence,
) -> IntegrationRecord:
    return IntegrationRecord(
        name=name,
        state=state,
        reason_code=reason_code,
        message=message,
        evidence=evidence,
    )


# Внутренние вызовы сохраняют короткие имена; внешние адаптеры используют
# публичные builders без импорта деталей закрытой реализации.
_evidence = build_evidence
_record = build_record


def _probe_record(
    name: IntegrationName,
    config: dict[str, object],
    credential: CredentialRef,
    result: McpProbeResult,
    *,
    blocked_tools: frozenset[str],
) -> IntegrationRecord:
    return _record(
        name,
        result.state,
        result.reason_code,
        {
            IntegrationState.READY: "Read-only MCP probe подтверждён.",
            IntegrationState.UNAUTHENTICATED: "MCP endpoint доступен, но credential не подтверждён.",
            IntegrationState.INCOMPATIBLE: "MCP server не соответствует требуемому read-only контракту.",
            IntegrationState.DEGRADED: "MCP server ответил без наблюдаемого результата.",
        }.get(result.state, "Прямой MCP probe не подтверждён."),
        _evidence(
            config=config,
            credential=credential,
            configured=True,
            reachable=result.state
            not in {IntegrationState.UNAVAILABLE, IntegrationState.UNKNOWN},
            authenticated=result.authenticated,
            selected_tool=result.selected_tool,
            tool_count=result.tool_count,
            blocked_tools=blocked_tools,
            diagnostics=result.diagnostics,
        ),
    )


class SemgrepAdapter(IntegrationAdapter):
    """Scoped Semgrep CLI, не запускающий scan всего repository по умолчанию."""

    name = IntegrationName.SEMGREP

    def status(self, root: Path, config: IntegrationConfig) -> IntegrationRecord:
        settings = config.provider("semgrep")
        executable = _executable(settings.get("command", "semgrep"))
        credential = CredentialRef()
        if executable is None:
            return _record(
                self.name,
                IntegrationState.UNAVAILABLE,
                "SEMGREP_EXECUTABLE_UNAVAILABLE",
                "Semgrep не найден в PATH.",
                _evidence(config=settings, credential=credential, configured=False),
            )
        ruleset = settings.get("ruleset")
        if not isinstance(ruleset, str) or not (root / ruleset).is_file():
            return _record(
                self.name,
                IntegrationState.NOT_CONFIGURED,
                "SEMGREP_RULESET_NOT_CONFIGURED",
                "Локальный ruleset Semgrep не найден.",
                _evidence(config=settings, credential=credential, configured=False),
            )
        return _record(
            self.name,
            IntegrationState.READY,
            "SEMGREP_EXECUTABLE_READY",
            "Semgrep executable найден; область scan задаётся явно.",
            _evidence(
                config=settings,
                credential=credential,
                configured=True,
                reachable=True,
            ),
        )

    def _path_list(self, root: Path, scope: AnalysisScope) -> tuple[str, ...]:
        explicit_paths = bool(scope.paths)
        if explicit_paths:
            raw_paths = tuple(scope.paths)
        elif scope.mode == "staged":
            raw_paths = GitClient(root).staged_paths()
        else:
            if scope.git_range is None:
                raise ToolingError(
                    ResultCode.TOOLING_INVALID_INVOCATION,
                    "Для committed_range требуется exact Git range.",
                )
            raw_paths = GitClient(root).changed_paths(
                scope.git_range.start_sha, scope.git_range.end_sha
            )
        if not raw_paths:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Semgrep scope не содержит ни одного файла.",
            )
        scoped = ScopedPath(root)
        resolved: list[str] = []
        for raw in raw_paths:
            if not isinstance(raw, str) or not raw.strip() or "*" in raw:
                raise ToolingError(
                    ResultCode.TOOLING_INVALID_INVOCATION,
                    "Semgrep paths должны быть явными файлами без glob.",
                )
            candidate = scoped.resolve(raw, allow_missing=not explicit_paths)
            if not candidate.exists():
                if not explicit_paths:
                    continue
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Semgrep scope содержит отсутствующий файл.",
                )
            if not candidate.is_file():
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Semgrep scope содержит не-файл.",
                )
            resolved.append(candidate.relative_to(root).as_posix())
        if not resolved:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Semgrep scope не содержит существующих файлов.",
            )
        return tuple(sorted(set(resolved)))

    @staticmethod
    def _findings(root: Path, payload: object) -> tuple[IntegrationFinding, ...]:
        if not isinstance(payload, dict):
            raise TypeError("Semgrep JSON должен быть object")
        results = payload.get("results")
        if not isinstance(results, list) or len(results) > _MAX_FINDINGS:
            raise ValueError("Semgrep results имеют неверный размер")
        findings: list[IntegrationFinding] = []
        root_resolved = root.resolve()
        scoped = ScopedPath(root)
        for item in results:
            if not isinstance(item, dict):
                raise TypeError("Semgrep finding имеет неверный тип")
            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError("Semgrep finding не содержит path")
            try:
                path = scoped.resolve(raw_path, allow_missing=False).relative_to(
                    root_resolved
                )
            except (ToolingError, ValueError) as exc:
                raise ValueError("Semgrep finding вышел за root") from exc
            location = item.get("start")
            if not isinstance(location, dict):
                location = (
                    item.get("extra", {}).get("start")
                    if isinstance(item.get("extra"), dict)
                    else {}
                )
            line = location.get("line") if isinstance(location, dict) else None
            line_value = line if isinstance(line, int) and line >= 1 else None
            extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            severity = extra.get("severity", "INFO")
            check_id = item.get("check_id", "unknown")
            fingerprint = item.get("fingerprint") or extra.get("fingerprint")
            raw_message = extra.get("message")
            message = (
                " ".join(raw_message.split())[:400]
                if isinstance(raw_message, str) and raw_message.strip()
                else "Semgrep finding обнаружен; безопасное сообщение правила отсутствует."
            )
            if not isinstance(severity, str) or not isinstance(check_id, str):
                raise TypeError("Semgrep finding metadata имеет неверный тип")
            findings.append(
                IntegrationFinding(
                    kind="semgrep",
                    identifier=check_id[:240],
                    path=path.as_posix()[:512],
                    line=line_value,
                    severity=severity[:40],
                    message=message,
                    fingerprint=fingerprint[:128] if isinstance(fingerprint, str) else None,
                )
            )
        return tuple(findings)

    def scan(
        self,
        root: Path,
        config: IntegrationConfig,
        scope: AnalysisScope,
    ) -> AdapterOutcome:
        settings = config.provider("semgrep")
        executable = _executable(settings.get("command", "semgrep"))
        if executable is None:
            record = self.status(root, config)
            return AdapterOutcome(record)
        paths = self._path_list(root, scope)
        ruleset = settings.get("ruleset")
        if not isinstance(ruleset, str) or not (root / ruleset).is_file():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Локальный ruleset Semgrep не найден.",
            )
        runner = StructuredProcessRunner()
        result = runner.run(
            ProcessSpec(
                executable=executable,
                argv=(
                    "scan",
                    "--json",
                    "--quiet",
                    "--metrics=off",
                    "--config",
                    ruleset,
                    "--",
                    *paths,
                ),
                cwd=root,
                timeout_seconds=120,
                max_output_bytes=_MAX_SCAN_BYTES,
                env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
        )
        if result.timed_out:
            record = _record(
                self.name,
                IntegrationState.UNAVAILABLE,
                "SEMGREP_SCAN_TIMEOUT",
                "Semgrep scan превысил ограниченный срок.",
                _evidence(
                    config=settings,
                    credential=CredentialRef(),
                    configured=True,
                    scope=scope,
                ),
            )
            return AdapterOutcome(record)
        if result.stdout_truncated:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Semgrep вернул усечённый JSON.",
            )
        if result.returncode not in {0, 1}:
            record = _record(
                self.name,
                IntegrationState.UNAVAILABLE,
                "SEMGREP_SCAN_FAILED",
                "Semgrep scan завершился ошибкой.",
                _evidence(
                    config=settings,
                    credential=CredentialRef(),
                    configured=True,
                    scope=scope,
                ),
            )
            return AdapterOutcome(record)
        try:
            payload = json.loads(result.stdout)
            findings = self._findings(root, payload)
        except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Semgrep вернул некорректный JSON.",
            ) from exc
        record = _record(
            self.name,
            IntegrationState.READY,
            "SEMGREP_SCAN_READY" if not findings else "SEMGREP_FINDINGS_OBSERVED",
            f"Scoped Semgrep scan завершён; findings: {len(findings)}.",
            _evidence(
                config=settings,
                credential=CredentialRef(),
                configured=True,
                reachable=True,
                authenticated=True,
                scope=scope,
                diagnostics=(f"scoped_paths={len(paths)}",),
            ),
        )
        return AdapterOutcome(record, findings)

    async def probe(self, root: Path, config: IntegrationConfig) -> AdapterOutcome:
        return AdapterOutcome(self.status(root, config))


class _HttpMcpAdapter(IntegrationAdapter):
    """База для двух официальных streamable HTTP endpoints."""

    required_tools: frozenset[str]
    plan: McpCallPlan
    requires_credential: bool = False

    def _settings(self, config: IntegrationConfig) -> dict[str, object]:
        return config.provider(self.name.value)

    def status(self, root: Path, config: IntegrationConfig) -> IntegrationRecord:
        settings = self._settings(config)
        endpoint = settings.get("endpoint")
        credential = _credential(settings, required=self.requires_credential)
        if not isinstance(endpoint, str) or not endpoint:
            return _record(
                self.name,
                IntegrationState.NOT_CONFIGURED,
                "INTEGRATION_ENDPOINT_NOT_CONFIGURED",
                "Endpoint прямого MCP не настроен.",
                _evidence(config=settings, credential=credential, configured=False),
            )
        if self.requires_credential and not credential.configured:
            state = IntegrationState.UNAUTHENTICATED
            reason = "INTEGRATION_CREDENTIAL_NOT_CONFIGURED"
            message = "Endpoint настроен, но credential не выбран явно."
        else:
            state = IntegrationState.READY
            reason = "INTEGRATION_ENDPOINT_CONFIGURED"
            message = "Официальный прямой MCP endpoint настроен."
        return _record(
            self.name,
            state,
            reason,
            message,
            _evidence(
                config=settings,
                credential=credential,
                configured=True,
                authenticated=credential.auth_verified,
            ),
        )

    async def probe(self, root: Path, config: IntegrationConfig) -> AdapterOutcome:
        settings = self._settings(config)
        endpoint = settings.get("endpoint")
        credential = _credential(settings, required=self.requires_credential)
        if not isinstance(endpoint, str) or not endpoint:
            return AdapterOutcome(self.status(root, config))
        headers: dict[str, str] = {}
        token = _credential_value(settings, credential)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        result = await probe_http(
            endpoint=endpoint,
            headers=headers,
            plan=self.plan,
            timeout_seconds=30,
            credential_configured=bool(token),
            credential_required=self.requires_credential,
        )
        if (
            result.state is IntegrationState.READY
            and self.requires_credential
            and not token
        ):
            result = McpProbeResult(
                IntegrationState.UNAUTHENTICATED,
                "INTEGRATION_AUTH_NOT_ASSERTED",
                tool_count=result.tool_count,
                selected_tool=result.selected_tool,
                authenticated=False,
                diagnostics=result.diagnostics + ("credential_not_asserted",),
            )
        return AdapterOutcome(
            _probe_record(
                self.name,
                settings,
                credential,
                result,
                blocked_tools=frozenset(),
            )
        )


class Context7Adapter(_HttpMcpAdapter):
    name = IntegrationName.CONTEXT7
    required_tools = frozenset({"resolve-library-id", "query-docs"})
    plan = McpCallPlan(
        required_tools=required_tools,
        probe_tool="resolve-library-id",
        arguments={"query": "Python documentation", "libraryName": "Python"},
    )
    # Hosted Context7 допускает bounded anonymous access с меньшей квотой;
    # API key уточняет auth evidence, но не становится обязательным.
    requires_credential = False


class DockerDocsAdapter(_HttpMcpAdapter):
    name = IntegrationName.DOCKER_DOCS
    required_tools = frozenset({"fetch_docker_docs"})
    plan = McpCallPlan(
        required_tools=required_tools,
        probe_tool="fetch_docker_docs",
        arguments={},
    )


def _shared_caller_token(settings: dict[str, object]) -> tuple[str | None, str | None]:
    """Вернуть имя и значение caller credential общего сервиса, не раскрывая секрет."""

    name = settings.get("caller_token_env")
    if (
        not isinstance(name, str)
        or name not in INTEGRATION_CALLER_TOKEN_ENVIRONMENT_KEYS
    ):
        return None, None
    return name, (os.environ.get(name, "").strip() or None)


def _shared_endpoint(settings: dict[str, object]) -> tuple[str | None, str]:
    """Проверить, что endpoint общего сервиса остаётся loopback-only HTTP.

    Общий долговременный service обслуживает клиентов одной машины, поэтому
    удалённый host отклоняется независимо от схемы: иначе клиент предъявлял бы
    caller token внешнему endpoint-у за пределами подтверждённой topology.
    """

    raw = settings.get("endpoint")
    if not isinstance(raw, str) or not raw:
        return None, "INTEGRATION_ENDPOINT_NOT_CONFIGURED"
    try:
        endpoint = validate_http_endpoint(raw)
    except HttpEndpointError as exc:
        return None, exc.code
    if (urlsplit(endpoint).hostname or "").casefold() not in LOOPBACK_HOSTS:
        return None, "INTEGRATION_SHARED_ENDPOINT_NOT_LOOPBACK"
    return endpoint, ""


@dataclass(frozen=True, slots=True)
class SharedMcpCallRoute:
    """Bounded маршрут вызова общего MCP HTTP service.

    Значение caller token исключено из `repr`, поэтому секрет не попадает в
    логи, диагностику и evidence; наружу наблюдаемо только имя переменной
    окружения, из которой он прочитан.
    """

    endpoint: str
    caller_token_env: str
    caller_token: str = field(repr=False)


class _SharedHttpMcpAdapter(_HttpMcpAdapter):
    """Клиент общего долгоживущего MCP HTTP service.

    Владельцем process-а, immutable image ref и read-only flags является
    `infrastructure/observability/compose.yaml`. Адаптер подтверждает вызывающую
    сторону caller credential-ом и вызывает bounded read-only tools; provider
    credential остаётся на стороне сервиса и в клиентское окружение не попадает.
    """

    blocked_tools: frozenset[str] = frozenset()
    read_only_enforced: bool = True

    def call_route(
        self, config: IntegrationConfig
    ) -> tuple[SharedMcpCallRoute | None, str]:
        """Вернуть bounded маршрут вызова общего сервиса или typed код отказа.

        Проверяются только endpoint и caller credential: provider credential
        принадлежит сервису и в окружении вызывающей стороны не требуется.
        """

        settings = self._settings(config)
        endpoint, endpoint_code = _shared_endpoint(settings)
        if endpoint_code or endpoint is None:
            return None, endpoint_code or "INTEGRATION_ENDPOINT_NOT_CONFIGURED"
        caller_env_name, caller_credential = self._caller_credentials(settings)
        if caller_credential is None:
            return None, "INTEGRATION_CALLER_TOKEN_NOT_CONFIGURED"
        return (
            SharedMcpCallRoute(
                endpoint=endpoint,
                caller_token_env=caller_env_name or "",
                caller_token=caller_credential,
            ),
            "",
        )

    def _caller_credentials(
        self, settings: dict[str, object]
    ) -> tuple[str | None, str | None]:
        return _shared_caller_token(settings)

    def _state_record(
        self,
        settings: dict[str, object],
        credential: CredentialRef,
        *,
        caller_env_name: str | None,
        caller_credential: str | None,
    ) -> IntegrationRecord:
        diagnostics = [
            f"caller_configured={str(caller_credential is not None).lower()}",
            f"provider_credential_configured={str(credential.configured).lower()}",
            "read_only_enforced="
            + ("server" if self.read_only_enforced else "client_allowlist"),
        ]
        evidence_arguments: dict[str, object] = {
            "config": settings,
            "credential": credential,
            "configured": True,
            "authenticated": credential.auth_verified,
            "blocked_tools": self.blocked_tools,
            "diagnostics": tuple(diagnostics),
        }
        if caller_credential is None:
            return _record(
                self.name,
                IntegrationState.UNAUTHENTICATED,
                "INTEGRATION_CALLER_TOKEN_NOT_CONFIGURED",
                "Общий MCP HTTP service настроен, но caller credential не задан в "
                f"окружении вызывающей стороны; ожидается {caller_env_name or 'caller token'}.",
                _evidence(**evidence_arguments),
            )
        return _record(
            self.name,
            IntegrationState.READY,
            "INTEGRATION_SHARED_SERVICE_CONFIGURED",
            "Общий MCP HTTP service настроен; provider credential передаётся сервису, а не клиенту.",
            _evidence(**evidence_arguments),
        )

    def status(self, root: Path, config: IntegrationConfig) -> IntegrationRecord:
        settings = self._settings(config)
        credential = _credential(settings, required=False)
        _endpoint, endpoint_code = _shared_endpoint(settings)
        if endpoint_code:
            return _record(
                self.name,
                IntegrationState.NOT_CONFIGURED,
                endpoint_code,
                "Endpoint общего MCP HTTP service не настроен или не является loopback-адресом.",
                _evidence(
                    config=settings,
                    credential=credential,
                    configured=False,
                    blocked_tools=self.blocked_tools,
                ),
            )
        caller_env_name, caller_credential = self._caller_credentials(settings)
        return self._state_record(
            settings,
            credential,
            caller_env_name=caller_env_name,
            caller_credential=caller_credential,
        )

    async def probe(self, root: Path, config: IntegrationConfig) -> AdapterOutcome:
        settings = self._settings(config)
        endpoint, endpoint_code = _shared_endpoint(settings)
        credential = _credential(settings, required=False)
        _caller_env_name, caller_credential = self._caller_credentials(settings)
        if (
            endpoint_code
            or endpoint is None
            or caller_credential is None
        ):
            return AdapterOutcome(self.status(root, config))
        result = await probe_http(
            endpoint=endpoint,
            headers={"Authorization": f"Bearer {caller_credential}"},
            plan=self.plan,
            timeout_seconds=30,
            credential_configured=True,
            credential_required=True,
        )
        return AdapterOutcome(
            _probe_record(
                self.name,
                settings,
                credential,
                result,
                blocked_tools=self.blocked_tools,
            )
        )


class GrafanaAdapter(_SharedHttpMcpAdapter):
    name = IntegrationName.GRAFANA
    plan = McpCallPlan(
        required_tools=GRAFANA_REQUIRED_READ_ONLY_TOOLS,
        probe_tool="list_datasources",
        arguments={},
        blocked_tools=GRAFANA_BLOCKED_TOOLS,
        expected_tools=GRAFANA_EXPECTED_TOOL_NAMES,
        tempo_tools=GRAFANA_TEMPO_READ_ONLY_TOOLS,
        missing_tempo_reason_code="GRAFANA_TEMPO_TOOL_UNAVAILABLE",
        toolset_drift_reason_code="GRAFANA_PROXIED_TOOLSET_DRIFT",
    )
    blocked_tools = GRAFANA_BLOCKED_TOOLS
    read_only_enforced = True


class DockerHubAdapter(_SharedHttpMcpAdapter):
    name = IntegrationName.DOCKER_HUB
    plan = McpCallPlan(
        required_tools=frozenset(
            {"checkRepository", "getRepositoryInfo", "listRepositoryTags"}
        ),
        probe_tool="checkRepository",
        arguments={"namespace": "library", "repository": "alpine"},
        blocked_tools=DOCKER_HUB_BLOCKED_TOOLS,
    )
    blocked_tools = DOCKER_HUB_BLOCKED_TOOLS
    # Серверной фильтрации tools у Docker Hub MCP нет: мутирующие tools
    # запрещает клиентский allowlist, это defence-in-depth, а не контроль сервиса.
    read_only_enforced = False

__all__ = [
    "DOCKER_HUB_BLOCKED_TOOLS",
    "DOCKER_HUB_READ_ONLY_TOOLS",
    "GRAFANA_BLOCKED_TOOLS",
    "GRAFANA_ENABLED_TOOL_CATEGORIES",
    "GRAFANA_EXPECTED_TOOL_NAMES",
    "GRAFANA_READ_ONLY_TOOLS",
    "GRAFANA_REQUIRED_READ_ONLY_TOOLS",
    "GRAFANA_TEMPO_READ_ONLY_TOOLS",
    "AdapterOutcome",
    "Context7Adapter",
    "DockerDocsAdapter",
    "DockerHubAdapter",
    "GrafanaAdapter",
    "IntegrationAdapter",
    "SemgrepAdapter",
    "SharedMcpCallRoute",
    "build_evidence",
    "build_record",
]
