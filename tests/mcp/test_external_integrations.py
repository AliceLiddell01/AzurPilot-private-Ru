from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from azurpilot.cli import CliInvocationError, build_parser
from azurpilot.integrations import IntegrationRegistry, coderabbit
from azurpilot.integrations.adapters import (
    DOCKER_HUB_BLOCKED_TOOLS,
    DOCKER_HUB_READ_ONLY_TOOLS,
    GRAFANA_BLOCKED_TOOLS,
    GRAFANA_ENABLED_TOOL_CATEGORIES,
    GRAFANA_EXPECTED_TOOL_NAMES,
    GRAFANA_READ_ONLY_TOOLS,
    GRAFANA_REQUIRED_READ_ONLY_TOOLS,
    GRAFANA_TEMPO_READ_ONLY_TOOLS,
    Context7Adapter,
    GrafanaAdapter,
    SemgrepAdapter,
    _credential,
    _discover_grafana_settings,
)
from azurpilot.integrations.config import (
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
    INTEGRATION_CREDENTIAL_ENVIRONMENT_KEYS,
    INTEGRATION_CREDENTIAL_FILE_ENVIRONMENT_KEYS,
)


@pytest.fixture(autouse=True)
def isolate_integration_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не позволять реальным credentials машины влиять на integration tests."""

    for variable in (
        *INTEGRATION_CREDENTIAL_ENVIRONMENT_KEYS,
        *INTEGRATION_CREDENTIAL_FILE_ENVIRONMENT_KEYS,
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


def test_coderabbit_triage_rejects_legacy_or_untyped_rejection():
    common = {
        "reviewed_head": "a" * 40,
        "affected_code": "affected implementation",
        "call_sites": "nearest call sites",
        "nearest_tests": "nearest tests",
        "relevant_contracts": "repository contract",
        "claimed_impact": "independent impact analysis",
        "decision_reason": "Detailed independent decision based on the contract.",
        "change_summary": "The applicable remediation is tracked for this head.",
    }
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

    rejected = CodeRabbitFindingTriage(
        disposition=FindingDisposition.FALSE_POSITIVE,
        conflict_kind="repository_contract_conflict",
        authoritative_source=".codex/context/GIT-WORKFLOW.md",
        **common,
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


def test_grafana_file_credential_uses_direct_container_env(
    tmp_path: Path,
):
    token = "fixture-grafana-token"
    credential_file = tmp_path / "grafana-token"
    credential_file.write_text(token + "\n", encoding="utf-8")
    settings = {
        "endpoint": "http://host.docker.internal:3000",
        "command": "docker",
        "image": "mcp/grafana@sha256:" + "a" * 64,
        "credential_env": "GRAFANA_SERVICE_ACCOUNT_TOKEN",
        "credential_file": str(credential_file),
    }

    adapter = GrafanaAdapter()
    command = adapter.build_command(
        tmp_path,
        IntegrationConfig(values={"grafana": settings}),
    )

    assert command is not None
    _executable, args, environment = command
    assert args[:3] == ("run", "--rm", "-i")
    assert "pass" not in args
    assert "docker pass" not in " ".join(args)
    assert "GRAFANA_SERVICE_ACCOUNT_TOKEN" in args
    assert environment["GRAFANA_SERVICE_ACCOUNT_TOKEN"] == token
    assert token not in " ".join(args)
    assert "-disable-write" in args
    assert "-disable-api" in args
    assert "-disable-query" not in args
    assert "-disable-proxied" not in args
    enabled_tools_index = args.index("-enabled-tools")
    assert args[enabled_tools_index + 1] == GRAFANA_ENABLED_TOOL_CATEGORIES
    assert GRAFANA_REQUIRED_READ_ONLY_TOOLS <= GRAFANA_READ_ONLY_TOOLS
    assert GRAFANA_TEMPO_READ_ONLY_TOOLS <= GRAFANA_READ_ONLY_TOOLS


def test_grafana_tool_catalog_is_exact_and_fail_closed():
    plan = GrafanaAdapter.plan
    expected = tuple(sorted(GRAFANA_EXPECTED_TOOL_NAMES))

    assert validate_tool_catalog(plan, expected) is None

    missing_tempo = tuple(
        name for name in expected if name != "tempo_traceql-search"
    )
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


def test_grafana_discovery_prefers_confirmed_compose_network_route(monkeypatch):
    monkeypatch.setattr(
        "azurpilot.integrations.adapters._executable", lambda _command: "docker"
    )
    inspect_payload = (
        '{"com.docker.compose.project.config_files":"C:/repo/infrastructure/observability/compose.yaml",'
        '"com.docker.compose.project.working_dir":"C:/repo/infrastructure/observability"}\t'
        '{"3000/tcp":[{"HostPort":"4310"}]}\t'
        '{"observability_default":{}}\n'
    )

    def fake_docker(_root, _executable, arguments):
        if arguments[0] == "ps":
            return "grafana-id\n"
        assert arguments[0:2] == ("inspect", "--format")
        return inspect_payload

    monkeypatch.setattr("azurpilot.integrations.adapters._docker_readonly", fake_docker)
    settings, code = _discover_grafana_settings(Path("C:/repo"), {})

    assert code == "GRAFANA_ENDPOINT_DISCOVERED"
    assert settings["endpoint"] == "http://grafana:3000"
    assert settings["network"] == "observability_default"


def test_grafana_discovery_rejects_ambiguous_published_routes(monkeypatch):
    monkeypatch.setattr(
        "azurpilot.integrations.adapters._executable", lambda _command: "docker"
    )
    inspect_payload = (
        '{"com.docker.compose.project.config_files":"C:/repo/compose.yaml"}\t'
        '{"3000/tcp":[{"HostPort":"4310"},{"HostPort":"4311"}]}\t{}\n'
    )

    def fake_docker(_root, _executable, arguments):
        return "grafana-id\n" if arguments[0] == "ps" else inspect_payload

    monkeypatch.setattr("azurpilot.integrations.adapters._docker_readonly", fake_docker)
    settings, code = _discover_grafana_settings(Path("C:/repo"), {})

    assert settings == {}
    assert code == "GRAFANA_ENDPOINT_AMBIGUOUS"


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
        ["integrations", "coderabbit", "review", "--base", "b" * 40]
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
    assert error.retry_source == "provider"
    retry_at, source = coderabbit._parse_provider_retry_metadata(
        {"metadata": {"retry_after_seconds": 120}},
        now=coderabbit.datetime(2026, 9, 16, tzinfo=coderabbit.UTC),
    )
    assert retry_at == "2026-09-16T00:02:00+00:00"
    assert source == "provider"
    unknown, unknown_source = coderabbit._parse_provider_retry_metadata(
        {"metadata": {"retry_after_seconds": 0}},
        now=coderabbit.datetime(2026, 9, 16, tzinfo=coderabbit.UTC),
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
