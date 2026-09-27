from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from azurpilot.cli import CliInvocationError, _render_human, build_parser
from azurpilot.integrations import IntegrationRegistry
from azurpilot.integrations.adapters import (
    DOCKER_HUB_BLOCKED_TOOLS,
    DOCKER_HUB_READ_ONLY_TOOLS,
    GRAFANA_BLOCKED_TOOLS,
    GRAFANA_EXPECTED_TOOL_NAMES,
    GRAFANA_READ_ONLY_TOOLS,
    GRAFANA_REQUIRED_READ_ONLY_TOOLS,
    GRAFANA_TEMPO_READ_ONLY_TOOLS,
    Context7Adapter,
    DockerHubAdapter,
    GrafanaAdapter,
    SemgrepAdapter,
    _credential,
)
from azurpilot.integrations.config import (
    SHARED_MCP_ENDPOINTS,
    SHARED_MCP_ROUTE,
    IntegrationConfig,
    _validate_value,
    load_integration_config,
)
from azurpilot.integrations.contracts import (
    CredentialSource,
    IntegrationDetails,
    IntegrationEvidence,
    IntegrationFinding,
    IntegrationName,
    IntegrationRecord,
    IntegrationState,
)
from azurpilot.integrations.mcp_client import (
    McpCallPlan,
    McpProbeResult,
    validate_tool_catalog,
)
from azurpilot.integrations.service import (
    ADAPTER_ORDER,
    AdapterOutcome,
    IntegrationService,
)
from azurpilot.tooling.contracts import (
    AnalysisScope,
    CapabilityStatus,
    GitRange,
    OperationState,
    ResultCode,
    ToolingResult,
)
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.infrastructure import InfrastructureService, SharedMcpOutcome
from azurpilot.tooling.process import (
    INTEGRATION_CALLER_TOKEN_ENVIRONMENT_KEYS,
    INTEGRATION_CREDENTIAL_ENVIRONMENT_KEYS,
    INTEGRATION_CREDENTIAL_FILE_ENVIRONMENT_KEYS,
)
from tests.support.paths import REPOSITORY_ROOT

GRAFANA_CALLER_TOKEN_ENV = "AZURPILOT_GRAFANA_MCP_CALLER_TOKEN"
DOCKER_HUB_CALLER_TOKEN_ENV = "AZURPILOT_DOCKER_HUB_MCP_CALLER_TOKEN"


def _caller_auth_header(value: str) -> dict[str, str]:
    """Собрать ожидаемый заголовок caller auth общего MCP HTTP service."""

    return {"Authorization": f"Bearer {value}"}


@pytest.fixture(autouse=True)
def isolate_integration_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не позволять реальным credentials и host config машины влиять на integration tests."""

    for variable in (
        *INTEGRATION_CREDENTIAL_ENVIRONMENT_KEYS,
        *INTEGRATION_CREDENTIAL_FILE_ENVIRONMENT_KEYS,
        *INTEGRATION_CALLER_TOKEN_ENVIRONMENT_KEYS,
        "AZURPILOT_MACHINE_CONFIG",
        "AZURPILOT_USER_CONFIG",
        "AZURPILOT_CONFIG_FILE",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_registry_is_closed_to_exactly_five_typed_families():
    registry = IntegrationRegistry()

    assert tuple(adapter.name for adapter in registry.adapters) == (
        IntegrationName.SEMGREP,
        IntegrationName.GRAFANA,
        IntegrationName.CONTEXT7,
        IntegrationName.DOCKER_DOCS,
        IntegrationName.DOCKER_HUB,
    )


def test_grafana_defaults_use_only_direct_credential_boundaries():
    settings = IntegrationConfig().provider("grafana")

    assert "credential_env" not in settings
    assert "credential_provider" not in settings
    assert "credential_ref" not in settings


def test_shared_families_use_repository_owned_http_service_registration():
    """Регистрация подключается к общему HTTP service и не владеет provider process-ом."""

    config = load_integration_config(REPOSITORY_ROOT)

    for family, service in (
        ("grafana", "grafana-mcp"),
        ("docker-hub", "dockerhub-mcp"),
    ):
        settings = config.provider(family)
        assert settings["route"] == SHARED_MCP_ROUTE
        assert settings["endpoint"] == SHARED_MCP_ENDPOINTS[family]
        assert settings["compose_service"] == service
        assert "command" not in settings
        assert "image" not in settings
        assert "args" not in settings


def test_shared_families_expose_caller_token_boundary_per_provider():
    config = load_integration_config(REPOSITORY_ROOT)

    assert config.provider("grafana")["caller_token_env"] == GRAFANA_CALLER_TOKEN_ENV
    assert (
        config.provider("docker-hub")["caller_token_env"]
        == DOCKER_HUB_CALLER_TOKEN_ENV
    )
    assert config.provider("docker-hub")["credential_env"] == "DOCKERHUB_PAT"


@pytest.mark.parametrize(
    ("state", "expected_ok"),
    [(CapabilityStatus.NOT_CONFIGURED, True), (CapabilityStatus.READY, False)],
)
def test_shared_mcp_stop_requires_proven_not_configured_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: CapabilityStatus,
    expected_ok: bool,
) -> None:
    """Stop не должен выдавать success без доказанного Compose postcondition."""

    monkeypatch.setattr(
        IntegrationService,
        "resolve_root",
        lambda _self, _root=None: tmp_path,
    )
    monkeypatch.setattr(
        InfrastructureService,
        "stop_shared_mcp",
        lambda _self, _root: SharedMcpOutcome(state, "test outcome"),
    )

    result = IntegrationService().shared_mcp("stop", tmp_path)

    assert result.ok is expected_ok
    assert result.details is not None
    assert result.details.state is state


def test_grafana_file_credential_is_bounded_and_not_serialized(
    tmp_path: Path,
):
    token = "fixture-grafana-token"
    credential_file = tmp_path / "grafana-token"
    credential_file.write_text(token + "\n", encoding="utf-8")
    settings = {
        "credential_env": "GRAFANA_SERVICE_ACCOUNT_TOKEN",
        "credential_file": str(credential_file),
    }

    credential = _credential(settings, required=True)

    assert credential.configured is True
    assert credential.source is CredentialSource.FILE
    assert token not in credential.model_dump_json()


def test_shared_probe_asserts_caller_token_and_keeps_provider_secret_on_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Клиент предъявляет только caller token; provider credential не покидает сервис."""

    caller_token = "fixture-caller-token"
    provider_token = "fixture-provider-token"
    monkeypatch.setenv(GRAFANA_CALLER_TOKEN_ENV, caller_token)
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", provider_token)
    observed: dict[str, object] = {}

    async def fake_probe_http(**kwargs: object) -> McpProbeResult:
        observed.update(kwargs)
        return McpProbeResult(
            IntegrationState.READY,
            "MCP_READ_ONLY_PROBE_READY",
            authenticated=True,
        )

    monkeypatch.setattr("azurpilot.integrations.adapters.probe_http", fake_probe_http)

    outcome = asyncio.run(GrafanaAdapter().probe(tmp_path, IntegrationConfig()))

    assert observed["endpoint"] == SHARED_MCP_ENDPOINTS["grafana"]
    assert observed["headers"] == _caller_auth_header(caller_token)
    assert observed["credential_configured"] is True
    assert observed["credential_required"] is True
    assert provider_token not in json.dumps(observed["headers"])
    assert outcome.record.state is IntegrationState.READY


def test_shared_probe_for_docker_hub_uses_its_own_caller_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    caller_token = "fixture-dockerhub-caller"
    monkeypatch.setenv(DOCKER_HUB_CALLER_TOKEN_ENV, caller_token)
    observed: dict[str, object] = {}

    async def fake_probe_http(**kwargs: object) -> McpProbeResult:
        observed.update(kwargs)
        return McpProbeResult(
            IntegrationState.READY,
            "MCP_READ_ONLY_PROBE_READY",
            authenticated=True,
        )

    monkeypatch.setattr("azurpilot.integrations.adapters.probe_http", fake_probe_http)

    outcome = asyncio.run(DockerHubAdapter().probe(tmp_path, IntegrationConfig()))

    assert observed["endpoint"] == SHARED_MCP_ENDPOINTS["docker-hub"]
    assert observed["headers"] == _caller_auth_header(caller_token)
    assert outcome.record.state is IntegrationState.READY


@pytest.mark.parametrize(
    "adapter",
    [GrafanaAdapter, DockerHubAdapter],
    ids=["grafana", "docker-hub"],
)
def test_shared_probe_never_reaches_service_without_caller_token(
    adapter: type, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    def fail_if_called(**_kwargs: object) -> object:
        raise AssertionError("probe_http не должен вызываться без caller token")

    monkeypatch.setattr("azurpilot.integrations.adapters.probe_http", fail_if_called)

    outcome = asyncio.run(adapter().probe(tmp_path, IntegrationConfig()))

    assert outcome.record.state is IntegrationState.UNAUTHENTICATED
    assert outcome.record.reason_code == "INTEGRATION_CALLER_TOKEN_NOT_CONFIGURED"


def test_shared_status_ready_requires_only_caller_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Готовность общего сервиса определяется caller auth, а не секретом провайдера."""

    caller_token = "fixture-caller-token"
    provider_token = "fixture-provider-token"
    monkeypatch.setenv(GRAFANA_CALLER_TOKEN_ENV, caller_token)
    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", raising=False)

    record = GrafanaAdapter().status(tmp_path, IntegrationConfig())
    serialized = record.model_dump_json()

    assert record.state is IntegrationState.READY
    assert record.reason_code == "INTEGRATION_SHARED_SERVICE_CONFIGURED"
    assert record.evidence.route == SHARED_MCP_ROUTE
    assert record.evidence.endpoint == SHARED_MCP_ENDPOINTS["grafana"]
    assert caller_token not in serialized
    assert provider_token not in serialized


def test_shared_status_reports_missing_caller_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "fixture-provider-token")

    record = GrafanaAdapter().status(tmp_path, IntegrationConfig())

    assert record.state is IntegrationState.UNAUTHENTICATED
    assert record.reason_code == "INTEGRATION_CALLER_TOKEN_NOT_CONFIGURED"
    assert GRAFANA_CALLER_TOKEN_ENV in record.message


def test_shared_status_does_not_require_provider_credential_of_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Provider credential принадлежит общему сервису, а не окружению клиента."""

    monkeypatch.setenv(GRAFANA_CALLER_TOKEN_ENV, "fixture-caller-token")

    record = GrafanaAdapter().status(tmp_path, IntegrationConfig())

    assert record.state is IntegrationState.READY
    assert record.reason_code == "INTEGRATION_SHARED_SERVICE_CONFIGURED"
    assert "provider_credential_configured=false" in record.evidence.diagnostics
    assert "read_only_enforced=server" in record.evidence.diagnostics


def test_docker_hub_status_reports_missing_caller_token(tmp_path: Path):
    """Docker Hub caller auth подтверждается общим сервисом без provider credential клиента."""

    record = DockerHubAdapter().status(tmp_path, IntegrationConfig())

    assert record.state is IntegrationState.UNAUTHENTICATED
    assert record.reason_code == "INTEGRATION_CALLER_TOKEN_NOT_CONFIGURED"


def test_shared_status_rejects_non_loopback_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv(GRAFANA_CALLER_TOKEN_ENV, "fixture-caller-token")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "fixture-provider-token")
    config = IntegrationConfig(
        values={"grafana": {"endpoint": "http://192.0.2.10:8777/mcp"}}
    )

    record = GrafanaAdapter().status(tmp_path, config)

    assert record.state is IntegrationState.NOT_CONFIGURED
    assert record.reason_code == "INTEGRATION_SHARED_ENDPOINT_NOT_LOOPBACK"


def test_shared_probe_does_not_start_transport_for_non_loopback_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv(GRAFANA_CALLER_TOKEN_ENV, "fixture-caller-token")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "fixture-provider-token")

    def fail_if_called(**_kwargs: object) -> object:
        raise AssertionError("probe_http не должен вызываться для внешнего endpoint-а")

    monkeypatch.setattr("azurpilot.integrations.adapters.probe_http", fail_if_called)
    config = IntegrationConfig(
        values={"grafana": {"endpoint": "http://192.0.2.10:8777/mcp"}}
    )

    outcome = asyncio.run(GrafanaAdapter().probe(tmp_path, config))

    assert outcome.record.state is IntegrationState.NOT_CONFIGURED
    assert outcome.record.reason_code == "INTEGRATION_SHARED_ENDPOINT_NOT_LOOPBACK"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8777/mcp",
        "http://localhost:8777/mcp",
        "http://[::1]:8777/mcp",
        "https://localhost:8777/mcp",
    ],
)
def test_shared_status_accepts_loopback_endpoints(
    endpoint: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv(GRAFANA_CALLER_TOKEN_ENV, "fixture-caller-token")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "fixture-provider-token")
    config = IntegrationConfig(values={"grafana": {"endpoint": endpoint}})

    record = GrafanaAdapter().status(tmp_path, config)

    assert record.state is IntegrationState.READY
    assert record.reason_code == "INTEGRATION_SHARED_SERVICE_CONFIGURED"
    assert record.evidence.endpoint == endpoint


@pytest.mark.parametrize(
    ("endpoint", "expected_code"),
    [
        ("http://192.0.2.10:8777/mcp", "INTEGRATION_SHARED_ENDPOINT_NOT_LOOPBACK"),
        ("http://grafana.example.test/mcp", "INTEGRATION_SHARED_ENDPOINT_NOT_LOOPBACK"),
        ("ftp://127.0.0.1:8777/mcp", "INTEGRATION_ENDPOINT_INVALID"),
        ("", "INTEGRATION_ENDPOINT_NOT_CONFIGURED"),
        (None, "INTEGRATION_ENDPOINT_NOT_CONFIGURED"),
    ],
)
def test_shared_status_fails_closed_outside_loopback(
    endpoint: object,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setenv(GRAFANA_CALLER_TOKEN_ENV, "fixture-caller-token")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "fixture-provider-token")
    config = IntegrationConfig(values={"grafana": {"endpoint": endpoint}})

    record = GrafanaAdapter().status(tmp_path, config)

    assert record.state is IntegrationState.NOT_CONFIGURED
    assert record.reason_code == expected_code


def test_caller_token_and_compose_service_are_validated_per_family():
    assert (
        _validate_value("grafana", "caller_token_env", GRAFANA_CALLER_TOKEN_ENV)
        == GRAFANA_CALLER_TOKEN_ENV
    )
    assert (
        _validate_value("docker-hub", "caller_token_env", DOCKER_HUB_CALLER_TOKEN_ENV)
        == DOCKER_HUB_CALLER_TOKEN_ENV
    )
    assert _validate_value("grafana", "compose_service", "grafana-mcp") == "grafana-mcp"
    with pytest.raises(ToolingError, match="caller_token_env"):
        _validate_value("grafana", "caller_token_env", DOCKER_HUB_CALLER_TOKEN_ENV)
    with pytest.raises(ToolingError, match="caller_token_env"):
        _validate_value("grafana", "caller_token_env", "UNAPPROVED_TOKEN")
    with pytest.raises(ToolingError, match="compose_service"):
        _validate_value("docker-hub", "compose_service", "grafana-mcp")


def test_grafana_tool_catalog_is_exact_and_fail_closed():
    plan = GrafanaAdapter.plan
    expected = tuple(sorted(GRAFANA_EXPECTED_TOOL_NAMES))

    assert GRAFANA_REQUIRED_READ_ONLY_TOOLS <= GRAFANA_EXPECTED_TOOL_NAMES
    assert validate_tool_catalog(plan, expected) is None

    missing_tempo = tuple(name for name in expected if name != "get_tempo_trace")
    assert validate_tool_catalog(plan, missing_tempo) == (
        "GRAFANA_TEMPO_TOOL_UNAVAILABLE",
        "tempo_tools_missing",
    )

    with_unknown = (*expected, "tempo_future_query")
    assert validate_tool_catalog(plan, with_unknown) == (
        "GRAFANA_PROXIED_TOOLSET_DRIFT",
        "toolset_drift",
    )


def test_grafana_tempo_tools_are_read_only_and_not_mutations():
    assert GRAFANA_TEMPO_READ_ONLY_TOOLS <= GRAFANA_READ_ONLY_TOOLS
    assert GRAFANA_TEMPO_READ_ONLY_TOOLS.isdisjoint(GRAFANA_BLOCKED_TOOLS)
    assert "grafana_api_request" in GRAFANA_BLOCKED_TOOLS


def test_probe_records_run_adapters_concurrently(monkeypatch, tmp_path: Path):

    started: list[IntegrationName] = []
    release = asyncio.Event()

    class ProbeAdapter:
        def __init__(self, name: IntegrationName):
            self.name = name

        async def probe(self, _root, _config):
            started.append(self.name)
            await release.wait()
            return AdapterOutcome(
                IntegrationRecord(
                    name=self.name,
                    state=IntegrationState.READY,
                    reason_code="PROBE_READY",
                    message="Готово",
                    evidence=IntegrationEvidence(route="test"),
                )
            )

    registry = IntegrationRegistry(
        adapters=tuple(ProbeAdapter(name) for name in ADAPTER_ORDER)
    )

    async def run():
        task = asyncio.create_task(registry.probe_records(tmp_path, IntegrationConfig()))
        for _ in range(20):
            await asyncio.sleep(0)
            if len(started) == len(ADAPTER_ORDER):
                break
        assert tuple(started) == ADAPTER_ORDER
        release.set()
        return await task

    outcomes = asyncio.run(run())
    assert tuple(outcome.record.name for outcome in outcomes) == ADAPTER_ORDER


def test_http_probe_uses_file_credential_value(monkeypatch, tmp_path: Path):
    token = "fixture-http-token"
    credential_file = tmp_path / "http-token"
    credential_file.write_text(token + "\n", encoding="utf-8")
    config = IntegrationConfig(
        values={
            "context7": {
                "endpoint": "https://context7.example.test/mcp",
                "credential_env": "CONTEXT7_API_KEY",
                "credential_file": str(credential_file),
            }
        }
    )
    observed: dict[str, object] = {}

    async def fake_probe_http(**kwargs: object) -> McpProbeResult:
        observed.update(kwargs)
        return McpProbeResult(
            IntegrationState.READY,
            "MCP_READ_ONLY_PROBE_READY",
            authenticated=True,
        )

    monkeypatch.setattr("azurpilot.integrations.adapters.probe_http", fake_probe_http)
    adapter = Context7Adapter()
    adapter.requires_credential = True

    outcome = asyncio.run(adapter.probe(tmp_path, config))

    assert observed["headers"] == {"Authorization": f"Bearer {token}"}
    assert observed["credential_configured"] is True
    assert outcome.record.state is IntegrationState.READY
    assert token not in outcome.record.model_dump_json()


def test_shared_registration_does_not_discover_provider_process(
    monkeypatch, tmp_path: Path
):
    """Общий сервис владеет process-ом: адаптер не выполняет docker discovery."""

    def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Общий HTTP route не выполняет provider discovery")

    monkeypatch.setattr("azurpilot.integrations.adapters._executable", fail_if_called)
    monkeypatch.setenv(GRAFANA_CALLER_TOKEN_ENV, "fixture-caller-token")
    monkeypatch.setenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "fixture-provider-token")
    observed: dict[str, object] = {}

    async def fake_probe_http(**kwargs: object) -> McpProbeResult:
        observed.update(kwargs)
        return McpProbeResult(
            IntegrationState.READY,
            "MCP_READ_ONLY_PROBE_READY",
            authenticated=True,
        )

    monkeypatch.setattr("azurpilot.integrations.adapters.probe_http", fake_probe_http)

    outcome = asyncio.run(GrafanaAdapter().probe(tmp_path, IntegrationConfig()))

    assert observed["endpoint"] == SHARED_MCP_ENDPOINTS["grafana"]
    assert outcome.record.evidence.route == SHARED_MCP_ROUTE
    assert outcome.record.evidence.image is None
    assert outcome.record.evidence.executable is None


def test_scoped_semgrep_rejects_exact_path_traversal(tmp_path: Path):
    inside = tmp_path / "inside.py"
    inside.write_text("print('ok')\n", encoding="utf-8")
    scope = AnalysisScope(paths=("../outside.py",), mode="staged")

    with pytest.raises(ToolingError, match="Файловая операция вышла"):
        SemgrepAdapter()._path_list(tmp_path, scope)


def test_scoped_semgrep_rejects_symlink_escape(tmp_path: Path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Окружение не разрешает создание symlink")
    scope = AnalysisScope(paths=("linked.py",), mode="staged")

    with pytest.raises(ToolingError, match="Symlink|reparse point"):
        SemgrepAdapter()._path_list(tmp_path, scope)


def test_scoped_semgrep_ignores_deleted_git_paths(monkeypatch, tmp_path: Path):
    deleted = tmp_path / "deleted.py"

    monkeypatch.setattr(
        "azurpilot.integrations.adapters.GitClient.staged_paths",
        lambda _client: ("deleted.py",),
    )

    with pytest.raises(ToolingError, match="существующих файлов"):
        SemgrepAdapter()._path_list(tmp_path, AnalysisScope(mode="staged"))

    deleted.write_text("print('ok')\n", encoding="utf-8")
    assert SemgrepAdapter()._path_list(tmp_path, AnalysisScope(mode="staged")) == (
        "deleted.py",
    )


def test_scoped_semgrep_findings_are_typed_and_bounded(tmp_path: Path):
    path = tmp_path / "inside.py"
    path.write_text("print('ok')\n", encoding="utf-8")
    findings = SemgrepAdapter._findings(
        tmp_path,
        {
            "results": [
                {
                    "path": "inside.py",
                    "check_id": "python.lang.security",
                    "start": {"line": 3},
                    "extra": {"severity": "WARNING"},
                }
            ]
        },
    )

    assert findings[0].path == "inside.py"
    assert findings[0].line == 3
    assert findings[0].kind == "semgrep"


def test_analysis_scope_requires_exact_range_for_changed_scan():
    with pytest.raises(ValidationError):
        AnalysisScope(mode="committed_range")
    with pytest.raises(ValidationError):
        AnalysisScope(
            mode="staged",
            git_range=GitRange(start_sha="a" * 40, end_sha="b" * 40),
        )


def test_mcp_call_plan_requires_allowlisted_probe_tool():
    with pytest.raises(ValueError, match="required_tools"):
        McpCallPlan(required_tools=frozenset({"known"}), probe_tool="unknown", arguments={})


def test_grafana_and_docker_hub_policies_exclude_write_tools():
    assert GRAFANA_READ_ONLY_TOOLS
    assert GRAFANA_BLOCKED_TOOLS
    assert GRAFANA_READ_ONLY_TOOLS.isdisjoint(GRAFANA_BLOCKED_TOOLS)
    assert DOCKER_HUB_READ_ONLY_TOOLS
    assert DOCKER_HUB_BLOCKED_TOOLS
    assert DOCKER_HUB_READ_ONLY_TOOLS.isdisjoint(DOCKER_HUB_BLOCKED_TOOLS)


def test_credential_ref_contains_only_provenance_not_secret(
    monkeypatch, tmp_path: Path
):
    token = "fixture-context7-token"
    monkeypatch.setenv("CONTEXT7_API_KEY", token)
    for variable in (
        "AZURPILOT_USER_CONFIG",
        "AZURPILOT_MACHINE_CONFIG",
        "AZURPILOT_CONFIG_FILE",
    ):
        monkeypatch.delenv(variable, raising=False)
    config = IntegrationConfig()
    record = Context7Adapter().status(tmp_path, config)
    serialized = record.model_dump_json()

    assert record.evidence.credential.configured is True
    assert record.evidence.credential.source is CredentialSource.ENVIRONMENT
    assert token not in serialized
    assert "CONTEXT7_API_KEY" in serialized


def test_config_rejects_unapproved_credential_reference(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "integrations.toml"
    config_path.write_text(
        "[integrations.context7]\ncredential_env = 'UNAPPROVED_SECRET'\n",
        encoding="utf-8",
    )
    for variable in (
        "AZURPILOT_USER_CONFIG",
        "AZURPILOT_MACHINE_CONFIG",
        "AZURPILOT_CONFIG_FILE",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("AZURPILOT_CONFIG_FILE", str(config_path))

    with pytest.raises(ToolingError, match="credential_env"):
        load_integration_config(tmp_path)


def test_config_ignores_retired_shared_service_environment_overrides(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("AZURPILOT_GRAFANA_URL", "http://192.0.2.10:8777/mcp")
    monkeypatch.setenv(
        "AZURPILOT_GRAFANA_CREDENTIAL_ENV", "GRAFANA_SERVICE_ACCOUNT_TOKEN"
    )
    monkeypatch.setenv("AZURPILOT_DOCKER_HUB_USERNAME_ENV", "DOCKERHUB_USERNAME")

    config = load_integration_config(tmp_path)

    assert config.provider("grafana")["endpoint"] == SHARED_MCP_ENDPOINTS["grafana"]
    assert "credential_env" not in config.provider("grafana")
    assert "username_env" not in config.provider("docker-hub")


@pytest.mark.parametrize(
    ("provider", "credential_env"),
    [
        ("docker-hub", "GRAFANA_SERVICE_ACCOUNT_TOKEN"),
        ("context7", "DOCKERHUB_PAT"),
    ],
)
def test_config_rejects_cross_provider_credential_reference(
    provider: str, credential_env: str, tmp_path: Path, monkeypatch
):
    variable = {
        "docker-hub": "AZURPILOT_DOCKER_HUB_CREDENTIAL_ENV",
        "context7": "AZURPILOT_CONTEXT7_CREDENTIAL_ENV",
    }[provider]
    monkeypatch.setenv(variable, credential_env)

    with pytest.raises(ToolingError, match="credential_env"):
        load_integration_config(tmp_path)


def test_config_rejects_file_reference_as_credential_value_name(
    tmp_path: Path, monkeypatch
):
    config_path = tmp_path / "integrations.toml"
    config_path.write_text(
        "[integrations.grafana]\n"
        "credential_env = 'GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE'\n",
        encoding="utf-8",
    )
    for variable in (
        "AZURPILOT_USER_CONFIG",
        "AZURPILOT_MACHINE_CONFIG",
        "AZURPILOT_CONFIG_FILE",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("AZURPILOT_CONFIG_FILE", str(config_path))

    with pytest.raises(ToolingError, match="credential_env"):
        load_integration_config(tmp_path)


def test_validated_user_config_overrides_repository_registration(tmp_path: Path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.context7_direct]\nurl = "https://mcp.context7.com/mcp"\n',
        encoding="utf-8",
    )
    config_path = tmp_path / "integrations.toml"
    config_path.write_text(
        '[integrations.context7]\nendpoint = "https://context7.example.test/mcp"\n',
        encoding="utf-8",
    )
    for variable in (
        "AZURPILOT_USER_CONFIG",
        "AZURPILOT_MACHINE_CONFIG",
        "AZURPILOT_CONFIG_FILE",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("AZURPILOT_CONFIG_FILE", str(config_path))

    config = load_integration_config(tmp_path)

    assert config.provider("context7")["endpoint"] == "https://context7.example.test/mcp"
    assert config.source("context7") == "explicit_config"


def test_config_priority_is_machine_then_user_then_explicit(
    tmp_path: Path, monkeypatch
):
    machine_path = tmp_path / "machine.toml"
    user_path = tmp_path / "user.toml"
    explicit_path = tmp_path / "explicit.toml"
    for path, endpoint in (
        (machine_path, "https://machine.example.test/mcp"),
        (user_path, "https://user.example.test/mcp"),
        (explicit_path, "https://explicit.example.test/mcp"),
    ):
        path.write_text(
            f'[integrations.context7]\nendpoint = "{endpoint}"\n',
            encoding="utf-8",
        )
    monkeypatch.setenv("AZURPILOT_MACHINE_CONFIG", str(machine_path))
    monkeypatch.setenv("AZURPILOT_USER_CONFIG", str(user_path))
    monkeypatch.setenv("AZURPILOT_CONFIG_FILE", str(explicit_path))

    assert load_integration_config(tmp_path).provider("context7")["endpoint"] == (
        "https://explicit.example.test/mcp"
    )

    monkeypatch.delenv("AZURPILOT_CONFIG_FILE")
    assert load_integration_config(tmp_path).provider("context7")["endpoint"] == (
        "https://user.example.test/mcp"
    )

    monkeypatch.delenv("AZURPILOT_USER_CONFIG")
    assert load_integration_config(tmp_path).provider("context7")["endpoint"] == (
        "https://machine.example.test/mcp"
    )


def test_repository_registration_rejects_untrusted_provider_identity(
    tmp_path: Path, monkeypatch
):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.context7_direct]\nurl = "https://attacker.example.test/mcp"\n',
        encoding="utf-8",
    )
    for variable in (
        "AZURPILOT_USER_CONFIG",
        "AZURPILOT_MACHINE_CONFIG",
        "AZURPILOT_CONFIG_FILE",
        "AZURPILOT_CONTEXT7_ENDPOINT",
    ):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(ToolingError, match="штатному direct route"):
        load_integration_config(tmp_path)


def test_cli_exposes_typed_integration_leaves_without_coderabbit():
    parser = build_parser()
    status_args = parser.parse_args(["integrations", "status", "--json"])
    paths_args = parser.parse_args(
        ["integrations", "semgrep", "scan", "--paths", "azurpilot/cli.py"]
    )
    scan_args = parser.parse_args(
        ["integrations", "semgrep", "scan", "--changed", "--base", "a" * 40]
    )

    assert status_args.integration_target == "status"
    assert paths_args.paths == ["azurpilot/cli.py"]
    assert scan_args.changed is True

    with pytest.raises(CliInvocationError):
        parser.parse_args(["integrations", "coderabbit", "status"])


def test_status_one_uses_generic_integration_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        IntegrationService,
        "resolve_root",
        lambda _self, _root=None: tmp_path,
    )

    result = IntegrationService().status_one(IntegrationName.SEMGREP, tmp_path)

    assert result.details is not None
    assert result.details.target is IntegrationName.SEMGREP
    assert result.details.integrations[0].name is IntegrationName.SEMGREP


def test_human_integration_finding_uses_provider_neutral_recommendation_label() -> None:
    record = IntegrationRecord(
        name=IntegrationName.SEMGREP,
        state=IntegrationState.READY,
        reason_code="SEMGREP_TEST_READY",
        message="Semgrep готов.",
        evidence=IntegrationEvidence(route="direct_local_cli"),
    )
    finding = IntegrationFinding(
        kind="semgrep",
        identifier="python.test.rule",
        path="module/example.py",
        line=3,
        severity="minor",
        message="Тестовое замечание.",
        resolution="Исправить тестовое замечание.",
    )
    result = ToolingResult(
        ok=True,
        code=ResultCode.OK,
        state=OperationState.READY,
        message="Проверка завершена.",
        details=IntegrationDetails(
            action="scan",
            integrations=(record,),
            target=IntegrationName.SEMGREP,
            findings=(finding,),
        ),
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    _render_human(
        result,
        stdout,
        stderr,
        no_color=True,
        verbose=False,
    )

    rendered = stdout.getvalue()
    assert "Рекомендация" in rendered
    assert "Исправить тестовое замечание." in rendered
    assert "Рекомендация CodeRabbit" not in rendered


def test_cli_rejects_ambiguous_semgrep_scope():
    parser = build_parser()
    with pytest.raises(CliInvocationError, match="not allowed with argument"):
        parser.parse_args(
            [
                "integrations",
                "semgrep",
                "scan",
                "--staged",
                "--paths",
                "azurpilot/cli.py",
            ]
        )


def test_integration_finding_rejects_reversed_line_range():
    with pytest.raises(ValueError, match="line_end"):
        IntegrationFinding(
            kind="semgrep",
            identifier="finding",
            path="module/example.py",
            line=120,
            line_end=10,
            severity="minor",
            message="Некорректный диапазон.",
        )
