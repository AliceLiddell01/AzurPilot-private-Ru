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
from azurpilot.tooling.filesystem import ScopedPath
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
from .mcp_client import McpCallPlan, McpProbeResult, probe_http, probe_stdio

_VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MAX_SCAN_BYTES = 2 * 1024 * 1024
_MAX_FINDINGS = 128

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
        "tempo_get-trace",
        "generate_deeplink",
    }
)
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
    if not isinstance(raw_name, str) or _ENV_NAME_RE.fullmatch(raw_name) is None:
        return CredentialRef()
    configured = bool(os.environ.get(raw_name, "").strip())
    return CredentialRef(
        configured=configured,
        source=CredentialSource.ENVIRONMENT if configured else CredentialSource.NONE,
        name=raw_name,
        auth_verified=None if not required else False,
    )


def _executable(command: object) -> str | None:
    if not isinstance(command, str) or _SAFE_ID_RE.fullmatch(command) is None:
        return None
    return shutil.which(command) or shutil.which(f"{command}.exe")


def _evidence(
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
        diagnostics=tuple(str(item)[:240] for item in diagnostics if item)[:8],
    )


def _record(
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
        paths = self._path_list(root, scope)
        if executable is None:
            record = self.status(root, config)
            return AdapterOutcome(record)
        runner = StructuredProcessRunner()
        result = runner.run(
            ProcessSpec(
                executable=executable,
                argv=("scan", "--json", "--quiet", "--config", "auto", "--", *paths),
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
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
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
        if credential.configured and credential.name:
            token = os.environ.get(credential.name, "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        result = await probe_http(
            endpoint=endpoint,
            headers=headers,
            plan=self.plan,
            timeout_seconds=30,
            credential_configured=credential.configured,
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


class _ContainerMcpAdapter(IntegrationAdapter):
    image_name: str
    plan: McpCallPlan
    blocked_tools: frozenset[str]
    requires_endpoint: bool = False
    requires_credential: bool = False

    def _settings(self, config: IntegrationConfig) -> dict[str, object]:
        return config.provider(self.name.value)

    def _command_args(
        self,
        settings: dict[str, object],
        credential: CredentialRef,
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
        if credential.configured and credential.name:
            value = os.environ.get(credential.name, "").strip()
            if value:
                env[credential.name] = value
                args.extend(("--env", credential.name))
        args.append(image)
        if self.name is IntegrationName.GRAFANA:
            args.extend(
                (
                    "-transport",
                    "stdio",
                    "-disable-write",
                    "-disable-proxied",
                )
            )
        return executable, tuple(args), env

    def status(self, root: Path, config: IntegrationConfig) -> IntegrationRecord:
        settings = self._settings(config)
        credential = _credential(settings, required=self.requires_credential)
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
                "GRAFANA_ENDPOINT_NOT_CONFIGURED",
                "Grafana endpoint не настроен; адрес Compose не угадывается.",
                _evidence(
                    config=settings,
                    credential=credential,
                    configured=False,
                    blocked_tools=self.blocked_tools,
                ),
            )
        if self.requires_credential and not credential.configured:
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
            ),
        )

    async def probe(self, root: Path, config: IntegrationConfig) -> AdapterOutcome:
        settings = self._settings(config)
        credential = _credential(settings, required=self.requires_credential)
        command = self._command_args(settings, credential)
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
        required_tools=frozenset({"list_datasources"}),
        probe_tool="list_datasources",
        arguments={},
        blocked_tools=GRAFANA_BLOCKED_TOOLS,
    )
    blocked_tools = GRAFANA_BLOCKED_TOOLS
    requires_endpoint = True
    requires_credential = True


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
    "GRAFANA_READ_ONLY_TOOLS",
    "AdapterOutcome",
    "Context7Adapter",
    "DockerDocsAdapter",
    "DockerHubAdapter",
    "GrafanaAdapter",
    "IntegrationAdapter",
    "SemgrepAdapter",
]
