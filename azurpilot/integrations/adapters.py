"""Типизированные адаптеры Semgrep и прямых MCP-серверов."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from azurpilot.tooling.contracts import AnalysisScope, ResultCode
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import (
    ScopedPath,
    bounded_read_text,
    path_has_link,
)
from azurpilot.tooling.git import GitClient
from azurpilot.tooling.process import (
    ProcessSpec,
    StructuredProcessRunner,
    safe_environment,
)

from .config import IntegrationConfig
from .contracts import (
    CredentialRef,
    CredentialSource,
    IntegrationEvidence,
    IntegrationFinding,
    IntegrationName,
    IntegrationRecord,
    IntegrationState,
)
from .mcp_client import (
    McpCallPlan,
    McpProbeResult,
    probe_http,
    probe_stdio,
    validate_endpoint,
)

_VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MAX_SCAN_BYTES = 2 * 1024 * 1024
_MAX_FINDINGS = 128

GRAFANA_ENABLED_TOOL_CATEGORIES = (
    "datasource,loki,prometheus,dashboard,search,navigation,proxied"
)
GRAFANA_TEMPO_READ_ONLY_TOOLS = frozenset(
    {
        "tempo_docs-config",
        "tempo_docs-traceql",
        "tempo_get-attribute-names",
        "tempo_get-attribute-values",
        "tempo_get-trace",
        "tempo_traceql-metrics-instant",
        "tempo_traceql-metrics-range",
        "tempo_traceql-search",
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

GRAFANA_READ_ONLY_TOOLS = frozenset(
    {
        "check_datasources_health",
        "get_dashboard_panel_queries",
        "get_dashboard_property",
        "get_dashboard_summary",
        "get_datasource",
        "list_datasources",
        "list_loki_label_names",
        "list_loki_label_values",
        "list_prometheus_label_names",
        "list_prometheus_label_values",
        "list_prometheus_metric_metadata",
        "list_prometheus_metric_names",
        "query_loki_logs",
        "query_prometheus",
        "query_prometheus_histogram",
        "search_dashboards",
        "generate_deeplink",
    }
)
GRAFANA_READ_ONLY_TOOLS |= GRAFANA_TEMPO_READ_ONLY_TOOLS
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
    {"createRepository", "updateRepositoryInfo", "deleteRepository"}
)


@dataclass(frozen=True, slots=True)
class AdapterOutcome:
    record: IntegrationRecord
    findings: tuple[IntegrationFinding, ...] = ()


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
                location = item.get("extra", {}).get("start") if isinstance(item.get("extra"), dict) else {}
            line = location.get("line") if isinstance(location, dict) else None
            line_value = line if isinstance(line, int) and line >= 1 else None
            extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            severity = extra.get("severity", "INFO")
            check_id = item.get("check_id", "unknown")
            fingerprint = item.get("fingerprint") or extra.get("fingerprint")
            if not isinstance(severity, str) or not isinstance(check_id, str):
                raise TypeError("Semgrep finding metadata имеет неверный тип")
            findings.append(
                IntegrationFinding(
                    kind="semgrep",
                    identifier=check_id[:240],
                    path=path.as_posix()[:512],
                    line=line_value,
                    severity=severity[:40],
                    message="Semgrep finding обнаружен; raw scanner payload скрыт.",
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
                _evidence(config=settings, credential=CredentialRef(), configured=True, scope=scope),
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
                _evidence(config=settings, credential=CredentialRef(), configured=True, scope=scope),
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


def _docker_readonly(
    root: Path, executable: str, arguments: tuple[str, ...]
) -> str | None:
    """Выполнить только bounded read-only запрос к Docker CLI."""

    try:
        result = StructuredProcessRunner().run(
            ProcessSpec(
                executable=executable,
                argv=arguments,
                cwd=root,
                timeout_seconds=30,
                max_output_bytes=128 * 1024,
                env=safe_environment(),
            )
        )
    except (OSError, ToolingError):
        return None
    if (
        result.timed_out
        or result.stdout_truncated
        or result.stderr_truncated
        or result.returncode != 0
    ):
        return None
    return result.stdout


def _grafana_topology_matches(root: Path, labels: object) -> bool:
    if not isinstance(labels, dict):
        return False
    root_text = str(root).replace("\\", "/").rstrip("/").casefold()
    if not root_text:
        return False
    for key, value in labels.items():
        if key not in {
            "com.docker.compose.project.config_files",
            "com.docker.compose.project.working_dir",
        }:
            continue
        for candidate in str(value).replace("\\", "/").casefold().split(";"):
            candidate = candidate.strip().rstrip("/")
            if candidate == root_text or candidate.startswith(f"{root_text}/"):
                return True
    return False


def _discover_grafana_settings(
    root: Path, settings: dict[str, object]
) -> tuple[dict[str, object], str | None]:
    """Найти Grafana route только по validated текущей Compose topology."""

    if isinstance(settings.get("endpoint"), str) and settings["endpoint"]:
        return dict(settings), None
    executable = _executable(settings.get("command", "docker"))
    if executable is None:
        return dict(settings), "INTEGRATION_CONTAINER_RUNTIME_UNAVAILABLE"
    inventory = _docker_readonly(
        root,
        executable,
        (
            "ps",
            "--filter",
            "label=com.docker.compose.service=grafana",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ),
    )
    if inventory is None:
        return dict(settings), "GRAFANA_ENDPOINT_NOT_CONFIGURED"
    container_ids = tuple(sorted({line.strip() for line in inventory.splitlines() if line.strip()}))
    published: set[tuple[str, str | None]] = set()
    networks: set[tuple[str, str | None]] = set()
    for container_id in container_ids[:16]:
        payload = _docker_readonly(
            root,
            executable,
            (
                "inspect",
                "--format",
                "{{json .Config.Labels}}\t{{json .NetworkSettings.Ports}}\t{{json .NetworkSettings.Networks}}",
                container_id,
            ),
        )
        if not payload:
            continue
        line = payload.splitlines()[0]
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            labels = json.loads(parts[0])
            ports = json.loads(parts[1])
            network_payload = json.loads(parts[2])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not _grafana_topology_matches(root, labels):
            continue
        published_ports = ports.get("3000/tcp") if isinstance(ports, dict) else None
        if isinstance(published_ports, list):
            for item in published_ports:
                if not isinstance(item, dict):
                    continue
                host_port = item.get("HostPort")
                if isinstance(host_port, str) and host_port.isdecimal():
                    published.add((f"http://host.docker.internal:{int(host_port)}", None))
        if isinstance(network_payload, dict):
            for network_name in network_payload:
                if isinstance(network_name, str) and _SAFE_ID_RE.fullmatch(network_name):
                    networks.add(("http://grafana:3000", network_name))
    if len(published) == 1:
        endpoint, network = next(iter(published))
        try:
            validate_endpoint(endpoint, allow_http=True)
        except ValueError:
            return dict(settings), "GRAFANA_ENDPOINT_NOT_CONFIGURED"
        resolved = dict(settings)
        resolved["endpoint"] = endpoint
        if network:
            resolved["network"] = network
        return resolved, "GRAFANA_ENDPOINT_DISCOVERED"
    if len(published) > 1:
        return dict(settings), "GRAFANA_ENDPOINT_AMBIGUOUS"
    if len(networks) == 1:
        endpoint, network = next(iter(networks))
        try:
            validate_endpoint(endpoint, allow_http=True)
        except ValueError:
            return dict(settings), "GRAFANA_ENDPOINT_NOT_CONFIGURED"
        resolved = dict(settings)
        resolved["endpoint"] = endpoint
        resolved["network"] = network
        return resolved, "GRAFANA_ENDPOINT_DISCOVERED"
    if len(networks) > 1:
        return dict(settings), "GRAFANA_ENDPOINT_AMBIGUOUS"
    return dict(settings), "GRAFANA_ENDPOINT_NOT_CONFIGURED"


class _ContainerMcpAdapter(IntegrationAdapter):
    image_name: str
    plan: McpCallPlan
    blocked_tools: frozenset[str]
    requires_endpoint: bool = False
    requires_credential: bool = False

    def _settings(self, config: IntegrationConfig) -> dict[str, object]:
        return config.provider(self.name.value)

    def _resolved_settings(
        self, root: Path, config: IntegrationConfig
    ) -> tuple[dict[str, object], str | None]:
        return self._settings(config), None

    def _resolved_credential(
        self, root: Path, settings: dict[str, object]
    ) -> tuple[CredentialRef, str | None]:
        credential = _credential(settings, required=self.requires_credential)
        value = _credential_value(settings, credential)
        return credential, value

    def _command_args(
        self,
        settings: dict[str, object],
        credential: CredentialRef,
        *,
        credential_value: str | None = None,
    ) -> tuple[str, tuple[str, ...], dict[str, str]] | None:
        executable = _executable(settings.get("command", "docker"))
        image = settings.get("image")
        if executable is None or not isinstance(image, str):
            return None
        env: dict[str, str] = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        args: list[str] = ["run", "--rm", "-i"]
        endpoint = settings.get("endpoint")
        if self.requires_endpoint:
            if not isinstance(endpoint, str) or not endpoint:
                return None
            env["GRAFANA_URL"] = endpoint
            args.extend(("--env", "GRAFANA_URL"))
        value = credential_value
        if value is None:
            value = _credential_value(settings, credential)
        if self.requires_credential and not value:
            return None
        if value:
            environment_name = credential.name
            if environment_name:
                env[environment_name] = value
                args.extend(("--env", environment_name))
        network = settings.get("network")
        if isinstance(network, str) and _SAFE_ID_RE.fullmatch(network):
            args.extend(("--network", network))
        args.append(image)
        if self.name is IntegrationName.GRAFANA:
            args.extend(
                (
                    "-transport",
                    "stdio",
                    "-disable-write",
                    "-enabled-tools",
                    GRAFANA_ENABLED_TOOL_CATEGORIES,
                )
            )
        return executable, tuple(args), env

    def build_command(
        self, root: Path, config: IntegrationConfig
    ) -> tuple[str, tuple[str, ...], dict[str, str]] | None:
        """Вернуть готовый direct command без раскрытия credential в evidence."""

        settings, _resolution_code = self._resolved_settings(root, config)
        credential, credential_value = self._resolved_credential(root, settings)
        return self._command_args(
            settings, credential, credential_value=credential_value
        )

    def status(self, root: Path, config: IntegrationConfig) -> IntegrationRecord:
        settings, resolution_code = self._resolved_settings(root, config)
        credential, credential_value = self._resolved_credential(root, settings)
        executable = _executable(settings.get("command", "docker"))
        image = settings.get("image")
        if executable is None:
            return _record(
                self.name,
                IntegrationState.UNAVAILABLE,
                "INTEGRATION_CONTAINER_RUNTIME_UNAVAILABLE",
                "Docker executable не найден.",
                _evidence(
                    config=settings,
                    credential=credential,
                    configured=False,
                    blocked_tools=self.blocked_tools,
                ),
            )
        if not isinstance(image, str):
            return _record(
                self.name,
                IntegrationState.INCOMPATIBLE,
                "INTEGRATION_IMAGE_NOT_CONFIGURED",
                "Immutable image ref прямого server не настроен.",
                _evidence(
                    config=settings,
                    credential=credential,
                    configured=False,
                    blocked_tools=self.blocked_tools,
                ),
            )
        if self.requires_endpoint and not isinstance(settings.get("endpoint"), str):
            return _record(
                self.name,
                IntegrationState.NOT_CONFIGURED,
                resolution_code or "GRAFANA_ENDPOINT_NOT_CONFIGURED",
                "Grafana endpoint не настроен; адрес Compose не угадывается.",
                _evidence(
                    config=settings,
                    credential=credential,
                    configured=False,
                    blocked_tools=self.blocked_tools,
                ),
            )
        if self.requires_credential and (
            not credential.configured or not credential_value
        ):
            state = IntegrationState.UNAUTHENTICATED
            reason = "INTEGRATION_CREDENTIAL_NOT_CONFIGURED"
            message = "Прямой server настроен, но credential не выбран явно."
        else:
            state = IntegrationState.READY
            reason = "INTEGRATION_DIRECT_SERVER_CONFIGURED"
            message = "Прямой immutable MCP server настроен."
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
                blocked_tools=self.blocked_tools,
                diagnostics=(resolution_code,) if resolution_code else (),
            ),
        )

    async def probe(self, root: Path, config: IntegrationConfig) -> AdapterOutcome:
        settings, _resolution_code = self._resolved_settings(root, config)
        credential, credential_value = self._resolved_credential(root, settings)
        command = self._command_args(
            settings, credential, credential_value=credential_value
        )
        if command is None:
            return AdapterOutcome(self.status(root, config))
        executable, args, env_values = command
        environment = safe_environment(env_values)
        result = await probe_stdio(
            command=executable,
            args=args,
            cwd=root,
            environment=environment,
            plan=self.plan,
            timeout_seconds=45,
            credential_configured=credential.configured,
            credential_required=self.requires_credential,
        )
        if (
            result.state is IntegrationState.READY
            and self.requires_credential
            and not credential.configured
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
                blocked_tools=self.blocked_tools,
            )
        )


class GrafanaAdapter(_ContainerMcpAdapter):
    name = IntegrationName.GRAFANA
    image_name = "mcp/grafana"
    plan = McpCallPlan(
        required_tools=frozenset({"list_datasources"}) | GRAFANA_TEMPO_READ_ONLY_TOOLS,
        probe_tool="list_datasources",
        arguments={},
        blocked_tools=GRAFANA_BLOCKED_TOOLS,
        expected_tools=GRAFANA_EXPECTED_TOOL_NAMES,
        tempo_tools=GRAFANA_TEMPO_READ_ONLY_TOOLS,
        missing_tempo_reason_code="GRAFANA_TEMPO_TOOL_UNAVAILABLE",
        toolset_drift_reason_code="GRAFANA_PROXIED_TOOLSET_DRIFT",
    )
    blocked_tools = GRAFANA_BLOCKED_TOOLS
    requires_endpoint = True
    requires_credential = True

    def _resolved_settings(
        self, root: Path, config: IntegrationConfig
    ) -> tuple[dict[str, object], str | None]:
        return _discover_grafana_settings(root, self._settings(config))


class DockerHubAdapter(_ContainerMcpAdapter):
    name = IntegrationName.DOCKER_HUB
    image_name = "mcp/dockerhub"
    plan = McpCallPlan(
        required_tools=frozenset({"checkRepository", "getRepositoryInfo", "listRepositoryTags"}),
        probe_tool="checkRepository",
        arguments={"namespace": "library", "repository": "alpine"},
        blocked_tools=DOCKER_HUB_BLOCKED_TOOLS,
    )
    blocked_tools = DOCKER_HUB_BLOCKED_TOOLS


__all__ = [
    "GRAFANA_BLOCKED_TOOLS",
    "GRAFANA_ENABLED_TOOL_CATEGORIES",
    "GRAFANA_EXPECTED_TOOL_NAMES",
    "GRAFANA_READ_ONLY_TOOLS",
    "GRAFANA_TEMPO_READ_ONLY_TOOLS",
    "AdapterOutcome",
    "build_evidence",
    "build_record",
    "Context7Adapter",
    "DockerDocsAdapter",
    "DockerHubAdapter",
    "GrafanaAdapter",
    "IntegrationAdapter",
    "SemgrepAdapter",
]
