from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from azurpilot.cli import CliInvocationError, build_parser
from azurpilot.integrations import IntegrationRegistry, coderabbit
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
from azurpilot.integrations.service import ADAPTER_ORDER, AdapterOutcome
from azurpilot.tooling.contracts import (
    AnalysisScope,
    CodeRabbitFindingTriage,
    FindingDisposition,
    GitRange,
)
from azurpilot.tooling.errors import ToolingError
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


def test_registry_is_closed_to_exactly_six_typed_families():
    registry = IntegrationRegistry()

    assert tuple(adapter.name for adapter in registry.adapters) == (
        IntegrationName.CODERABBIT,
        IntegrationName.SEMGREP,
        IntegrationName.GRAFANA,
        IntegrationName.CONTEXT7,
        IntegrationName.DOCKER_DOCS,
        IntegrationName.DOCKER_HUB,
    )


def test_grafana_defaults_use_only_direct_credential_boundaries():
    settings = IntegrationConfig().provider("grafana")

    assert settings["credential_env"] == "GRAFANA_SERVICE_ACCOUNT_TOKEN"
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
    assert config.provider("grafana")["credential_env"] == (
        "GRAFANA_SERVICE_ACCOUNT_TOKEN"
    )
    assert config.provider("docker-hub")["credential_env"] == "DOCKERHUB_PAT"


def test_coderabbit_defaults_use_only_native_host_route():
    settings = IntegrationConfig().provider("coderabbit")

    assert settings["route"] == "direct_native_agent"
    assert "wsl_distribution" not in settings
    assert "review_clone" not in settings
    assert "command" not in settings


def test_coderabbit_executable_validation_follows_host_native_name():
    assert (
        _validate_value(
            "coderabbit", "executable", "coderabbit", host_os="posix"
        )
        == "coderabbit"
    )
    assert (
        _validate_value(
            "coderabbit", "executable", "coderabbit.exe", host_os="nt"
        )
        == "coderabbit.exe"
    )
    with pytest.raises(ToolingError):
        _validate_value("coderabbit", "executable", "coderabbit.exe", host_os="posix")
    with pytest.raises(ToolingError):
        _validate_value("coderabbit", "executable", "coderabbit", host_os="unsupported")


def test_agent_ndjson_parses_status_finding_and_complete():
    parsed = coderabbit.parse_agent_ndjson(
        [
            json.dumps({"type": "review_context"}),
            json.dumps({"type": "status", "message": "running"}),
            json.dumps(
                {
                    "type": "finding",
                    "finding": {
                        "path": "azurpilot/integrations/service.py",
                        "severity": "major",
                        "comment": "Содержательное замечание о границе.",
                        "classification": "confirmed",
                    },
                }
            ),
            json.dumps({"type": "complete"}),
        ]
    )

    assert parsed.complete is True
    assert len(parsed.findings) == 1
    assert parsed.findings[0].disposition is None
    assert parsed.findings[0].triage is None
    assert parsed.findings[0].path.endswith("service.py")
    assert "Содержательное замечание" in parsed.findings[0].impact
    assert parsed.unknown_events == ("review_context", "status")


def test_agent_ndjson_accepts_complete_finding_count():
    parsed = coderabbit.parse_agent_ndjson(
        [
            json.dumps(
                {
                    "type": "finding",
                    "finding": {
                        "path": "azurpilot/integrations/service.py",
                        "severity": "major",
                        "message": "Проверить границу.",
                    },
                }
            ),
            json.dumps({"type": "complete", "findings": 1}),
        ]
    )

    assert parsed.complete is True
    assert len(parsed.findings) == 1


def test_agent_ndjson_preserves_top_level_finding_comment():
    parsed = coderabbit.parse_agent_ndjson(
        [
            json.dumps(
                {
                    "type": "finding",
                    "comment": "Комментарий находится на уровне события.",
                    "finding": {
                        "path": "azurpilot/integrations/coderabbit.py",
                        "severity": "minor",
                    },
                }
            ),
            json.dumps({"type": "complete", "findings": 1}),
        ]
    )

    assert "уровне события" in parsed.findings[0].impact


def test_agent_ndjson_preserves_official_codegen_instructions_shape():
    parsed = coderabbit.parse_agent_ndjson(
        [
            json.dumps(
                {
                    "type": "finding",
                    "finding": {
                        "fileName": "azurpilot/integrations/coderabbit.py",
                        "severity": "trivial",
                        "codegenInstructions": "Сделайте bounded typed mapping и добавьте regression test.",
                    },
                }
            ),
            json.dumps({"type": "complete", "findings": 1}),
        ]
    )

    finding = parsed.findings[0]
    assert finding.path.endswith("coderabbit.py")
    assert finding.codegen_instructions == finding.resolution
    assert "regression test" in finding.impact
    assert finding.disposition is None


def test_agent_ndjson_preserves_official_suggestions_and_comment_fallback():
    parsed = coderabbit.parse_agent_ndjson(
        [
            json.dumps(
                {
                    "type": "finding",
                    "finding": {
                        "fileName": "azurpilot/tooling/contracts.py",
                        "severity": "minor",
                        "comment": "Проверьте typed contract перед сериализацией.",
                        "suggestions": [
                            "Добавьте проверку schema.",
                            {"text": "Добавьте тест на invalid state."},
                        ],
                    },
                }
            ),
            json.dumps({"type": "complete", "findings": 1}),
        ]
    )

    finding = parsed.findings[0]
    assert finding.codegen_instructions is None
    assert finding.resolution == finding.impact
    assert finding.suggestions == (
        "Добавьте проверку schema.",
        "Добавьте тест на invalid state.",
    )


def test_agent_ndjson_rejects_incomplete_mixed_findings_without_budget_claim():
    lines = [
        json.dumps(
            {
                "type": "finding",
                "finding": {
                    "fileName": "azurpilot/tooling/contracts.py",
                    "severity": "major",
                    "comment": "Содержательный provider claim.",
                },
            }
        ),
        json.dumps(
            {
                "type": "finding",
                "finding": {
                    "fileName": "azurpilot/tooling/contracts.py",
                    "severity": "minor",
                },
            }
        ),
        json.dumps({"type": "complete", "findings": 2}),
    ]

    with pytest.raises(coderabbit.CodeRabbitStreamError, match="CODERABBIT_FINDING_INCOMPLETE"):
        coderabbit.parse_agent_ndjson(lines)


def _coderabbit_triage_common() -> dict[str, str]:
    return {
        "reviewed_head": "a" * 40,
        "affected_code": "затронутая реализация",
        "call_sites": "ближайшие call sites",
        "nearest_tests": "ближайшие тесты",
        "relevant_contracts": "контракт репозитория",
        "claimed_impact": "независимый анализ влияния",
        "decision_reason": "Решение основано на независимой проверке контракта.",
        "change_summary": "Применимое исправление отслеживается для этого head.",
    }


def test_coderabbit_triage_rejects_legacy_or_untyped_rejection():
    common = _coderabbit_triage_common()
    with pytest.raises(ValidationError):
        CodeRabbitFindingTriage(
            disposition=FindingDisposition.FALSE_POSITIVE,
            **common,
        )
    with pytest.raises(ValidationError):
        CodeRabbitFindingTriage(
            disposition="insufficient evidence",
            **common,
        )


def test_coderabbit_triage_accepts_typed_conflict_rejection():
    rejected = CodeRabbitFindingTriage(
        disposition=FindingDisposition.FALSE_POSITIVE,
        conflict_kind="repository_contract_conflict",
        authoritative_source=".codex/context/GIT-WORKFLOW.md",
        **_coderabbit_triage_common(),
    )
    assert rejected.conflict_kind.value == "repository_contract_conflict"


def test_agent_ndjson_preserves_coderabbit_issue_and_suggested_fix_text():
    parsed = coderabbit.parse_agent_ndjson(
        [
            json.dumps(
                {
                    "type": "finding",
                    "finding": {
                        "fileName": "azurpilot/integrations/coderabbit.py",
                        "severity": "major",
                        "issue": "Провайдерский issue должен быть виден оператору.",
                        "suggested_fix": "Покажите bounded summary после complete.",
                    },
                }
            ),
            json.dumps({"type": "complete", "findings": 1}),
        ]
    )

    finding = parsed.findings[0]
    assert "виден оператору" in finding.impact
    assert "bounded summary" in finding.resolution
    assert "требует независимой проверки" not in finding.impact


def test_provider_findings_output_preserves_full_comment_and_location():
    parsed = coderabbit.parse_provider_findings_output(
        """
  major [Functional Correctness]
  → dev_tools/observability_mcp.py:74-77

  Не изменяйте аргументы вызова функцией санитизации вывода.

  Отклоняйте такие аргументы fail-closed вместо молчаливой подмены.


  🔒 Предлагаемое исправление

  result != dict(arguments)
────────────────────────────────────────────────────────────────────────
"""
    )

    assert len(parsed) == 1
    assert parsed[0].severity.value == "major"
    assert parsed[0].path == "dev_tools/observability_mcp.py"
    assert parsed[0].title == "Functional Correctness"
    assert parsed[0].line == 74
    assert parsed[0].line_end == 77
    assert "Отклоняйте такие аргументы" in parsed[0].impact
    assert "result != dict(arguments)" in parsed[0].resolution
    assert parsed[0].impact != "CodeRabbit finding требует независимой проверки."


def test_agent_ndjson_unknown_event_is_diagnostic_not_finding():
    parsed = coderabbit.parse_agent_ndjson(
        [json.dumps({"type": "future_status"}), json.dumps({"type": "complete"})]
    )

    assert parsed.findings == ()
    assert parsed.unknown_events == ("future_status",)


def test_agent_ndjson_rejects_malformed_truncated_and_rate_limited_streams():
    with pytest.raises(coderabbit.CodeRabbitStreamError, match="CODERABBIT_NDJSON_INVALID"):
        coderabbit.parse_agent_ndjson(["not-json"])
    with pytest.raises(coderabbit.CodeRabbitStreamError, match="CODERABBIT_STREAM_TRUNCATED"):
        coderabbit.parse_agent_ndjson([json.dumps({"type": "status"})])
    with pytest.raises(coderabbit.CodeRabbitStreamError, match="CODERABBIT_RATE_LIMITED"):
        coderabbit.parse_agent_ndjson([json.dumps({"type": "error", "code": "429"})])


def test_agent_ndjson_does_not_classify_unrelated_rate_text_as_rate_limit():
    with pytest.raises(coderabbit.CodeRabbitStreamError) as error:
        coderabbit.parse_agent_ndjson(
            [json.dumps({"type": "error", "message": "rate window is unavailable"})]
        )

    assert error.value.code == "CODERABBIT_AGENT_ERROR"
    assert error.value.rate_limited is False


def test_agent_ndjson_rejects_unsafe_finding_path():
    with pytest.raises(coderabbit.CodeRabbitStreamError, match="CODERABBIT_FINDING_PATH_INVALID"):
        coderabbit.parse_agent_ndjson(
            [
                json.dumps({"type": "finding", "path": "../outside.py"}),
                json.dumps({"type": "complete"}),
            ]
        )
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


@pytest.mark.parametrize(
    ("iterations", "terminal", "allowed"),
    [(0, False, True), (1, False, True), (2, False, True), (3, False, False), (0, True, False)],
)
def test_coderabbit_iteration_policy_is_bounded(iterations, terminal, allowed):
    assert coderabbit.review_iteration_allowed(iterations, terminal=terminal) is allowed


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


@pytest.mark.parametrize(
    ("provider", "credential_env"),
    [
        ("grafana", "GRAFANA_SERVICE_ACCOUNT_TOKEN"),
        ("docker-hub", "DOCKERHUB_PAT"),
        ("context7", "CONTEXT7_API_KEY"),
    ],
)
def test_config_accepts_provider_specific_credential_reference(
    provider: str, credential_env: str, tmp_path: Path, monkeypatch
):
    variable = {
        "grafana": "AZURPILOT_GRAFANA_CREDENTIAL_ENV",
        "docker-hub": "AZURPILOT_DOCKER_HUB_CREDENTIAL_ENV",
        "context7": "AZURPILOT_CONTEXT7_CREDENTIAL_ENV",
    }[provider]
    monkeypatch.setenv(variable, credential_env)

    config = load_integration_config(tmp_path)

    assert config.provider(provider)["credential_env"] == credential_env


@pytest.mark.parametrize(
    ("provider", "credential_env"),
    [
        ("grafana", "DOCKERHUB_PAT"),
        ("grafana", "CONTEXT7_API_KEY"),
        ("docker-hub", "GRAFANA_SERVICE_ACCOUNT_TOKEN"),
        ("context7", "DOCKERHUB_PAT"),
    ],
)
def test_config_rejects_cross_provider_credential_reference(
    provider: str, credential_env: str, tmp_path: Path, monkeypatch
):
    variable = {
        "grafana": "AZURPILOT_GRAFANA_CREDENTIAL_ENV",
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


def test_cli_exposes_typed_integration_leaves():
    parser = build_parser()
    status_args = parser.parse_args(["integrations", "status", "--json"])
    paths_args = parser.parse_args(
        ["integrations", "semgrep", "scan", "--paths", "azurpilot/cli.py"]
    )
    scan_args = parser.parse_args(
        ["integrations", "semgrep", "scan", "--changed", "--base", "a" * 40]
    )
    review_args = parser.parse_args(
        [
            "integrations",
            "coderabbit",
            "review",
            "--base",
            "b" * 40,
            "--task-id",
            "test-review-task",
        ]
    )
    cycle_args = parser.parse_args(
        ["integrations", "coderabbit", "cycle", "start", "--base", "c" * 40]
    )
    config_args = parser.parse_args(
        ["integrations", "coderabbit", "config", "validate", "--json"]
    )

    assert status_args.integration_target == "status"
    assert paths_args.paths == ["azurpilot/cli.py"]
    assert scan_args.changed is True
    assert review_args.base == "b" * 40
    assert review_args.task_id == "test-review-task"
    assert cycle_args.integration_action == "cycle"
    assert cycle_args.coderabbit_cycle_action == "start"
    assert cycle_args.base == "c" * 40
    assert config_args.integration_action == "config"
    assert config_args.coderabbit_config_action == "validate"


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
            kind="coderabbit",
            identifier="finding",
            path="module/example.py",
            line=120,
            line_end=10,
            severity="minor",
            message="Некорректный диапазон.",
        )


def test_coderabbit_rate_limit_metadata_is_bounded_and_typed():
    error = coderabbit.CodeRabbitStreamError(
        "CODERABBIT_RATE_LIMITED",
        rate_limited=True,
        retry_not_before="2026-09-16T12:00:00+00:00",
        retry_source="provider",
    )
    assert error.rate_limited is True
    assert error.retry_not_before == "2026-09-16T12:00:00+00:00"
    assert error.retry_source == "provider"
    retry_at, source = coderabbit._parse_provider_retry_metadata(
        {"metadata": {"retry_after_seconds": 120}},
        now=datetime(2026, 9, 16, tzinfo=UTC),
    )
    assert retry_at == "2026-09-16T00:02:00+00:00"
    assert source == "provider"
    stale, stale_source = coderabbit._parse_provider_retry_metadata(
        {"metadata": {"retry_at": "2026-09-15T23:59:59+00:00"}},
        now=datetime(2026, 9, 16, tzinfo=UTC),
    )
    assert stale is None
    assert stale_source == "unknown"
    unknown, unknown_source = coderabbit._parse_provider_retry_metadata(
        {"metadata": {"retry_after_seconds": 0}},
        now=datetime(2026, 9, 16, tzinfo=UTC),
    )
    assert unknown is None
    assert unknown_source == "unknown"


def test_coderabbit_provider_retry_hint_is_preserved_without_raw_payload():
    with pytest.raises(coderabbit.CodeRabbitStreamError) as caught:
        coderabbit.parse_agent_ndjson(
            [
                json.dumps(
                    {
                        "type": "error",
                        "code": "429",
                        "retry_after_seconds": 120,
                        "secret": "must-not-persist",
                    }
                )
            ]
        )
    error = caught.value
    assert error.rate_limited is True
    assert error.retry_source == "provider"
    assert error.retry_not_before is not None
    assert "secret" not in str(error)
