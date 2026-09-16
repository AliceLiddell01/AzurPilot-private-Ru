from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from azurpilot.cli import CliInvocationError, build_parser
from azurpilot.integrations import IntegrationRegistry, coderabbit
from azurpilot.integrations.adapters import (
    DOCKER_HUB_BLOCKED_TOOLS,
    DOCKER_HUB_READ_ONLY_TOOLS,
    GRAFANA_BLOCKED_TOOLS,
    GRAFANA_READ_ONLY_TOOLS,
    Context7Adapter,
    SemgrepAdapter,
)
from azurpilot.integrations.config import IntegrationConfig, load_integration_config
from azurpilot.integrations.contracts import (
    CredentialSource,
    IntegrationName,
    IntegrationState,
)
from azurpilot.integrations.mcp_client import McpCallPlan
from azurpilot.tooling.contracts import AnalysisScope, FindingDisposition, GitRange
from azurpilot.tooling.errors import ToolingError


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


@pytest.mark.parametrize(
    ("inventory", "expected_state", "expected_code"),
    [
        ("NAME STATE VERSION\n", IntegrationState.NOT_CONFIGURED, "CODERABBIT_WSL_DISTRIBUTION_NOT_CONFIGURED"),
        ("ReviewLinux Running 1\n", IntegrationState.INCOMPATIBLE, "CODERABBIT_WSL2_REQUIRED"),
        (
            "ReviewLinux Running 2\nOtherLinux Stopped 2\n",
            IntegrationState.INCOMPATIBLE,
            "CODERABBIT_WSL_DISTRIBUTION_AMBIGUOUS",
        ),
        ("ReviewLinux Running 2\n", IntegrationState.READY, "CODERABBIT_WSL_DISTRIBUTION_SELECTED"),
    ],
)
def test_wsl_selection_is_bounded_and_fail_closed(
    inventory: str, expected_state: IntegrationState, expected_code: str
):
    selection = coderabbit.select_wsl_distribution(inventory)

    assert selection.state is expected_state
    assert selection.reason_code == expected_code


def test_wsl_selection_uses_configured_exact_name_without_aliasing():
    inventory = "ReviewLinux Running 2\nOtherLinux Running 2\n"

    selected = coderabbit.select_wsl_distribution(inventory, configured="OtherLinux")
    missing = coderabbit.select_wsl_distribution(inventory, configured="otherlinux")

    assert selected.distribution == "OtherLinux"
    assert selected.state is IntegrationState.READY
    assert missing.state is IntegrationState.NOT_CONFIGURED


def test_wsl_parser_discards_headers_and_invalid_names():
    entries = coderabbit.parse_wsl_verbose(
        "NAME STATE VERSION\n* ReviewLinux Running 2\nInvalid Name Running 2\n"
    )

    assert [(item.name, item.version) for item in entries] == [("ReviewLinux", 2)]


def test_wsl_inventory_cross_checks_quiet_and_verbose(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, ...]] = []

    class Runner:
        def run(self, spec):
            calls.append(spec.argv)
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "ReviewLinux\n"
                    if spec.argv[-1] == "--quiet"
                    else "NAME STATE VERSION\n* ReviewLinux Running 2\n"
                ),
                stderr="",
                stdout_truncated=False,
                stderr_truncated=False,
                timed_out=False,
            )

    monkeypatch.setattr(coderabbit, "StructuredProcessRunner", Runner)

    entries, error_code = coderabbit.CodeRabbitAdapter._wsl_inventory(
        tmp_path, "wsl.exe"
    )

    assert error_code is None
    assert [(item.name, item.version) for item in entries] == [("ReviewLinux", 2)]
    assert calls == [("--list", "--quiet"), ("--list", "--verbose")]


def test_wsl_implementation_has_no_current_machine_name_dependency():
    source = inspect.getsource(coderabbit).casefold()
    current_machine_like_name = "arch" + "linux"

    assert current_machine_like_name not in source


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
                        "message": "Проверить границу.",
                        "classification": "confirmed",
                    },
                }
            ),
            json.dumps({"type": "complete"}),
        ]
    )

    assert parsed.complete is True
    assert len(parsed.findings) == 1
    assert parsed.findings[0].disposition is FindingDisposition.CONFIRMED
    assert parsed.findings[0].path.endswith("service.py")
    assert parsed.unknown_events == ("review_context", "status")


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


def test_wsl_runtime_preserves_bounded_output_flags(tmp_path: Path):
    runtime = coderabbit._WslRuntime(
        tmp_path,
        "wsl.exe",
        distro="ReviewLinux",
        user="reviewer",
        home="/home/reviewer",
        clone="/home/reviewer/review",
        repository_identity="alice/example",
        coderabbit_executable="/usr/local/bin/coderabbit",
        coderabbit_command="/usr/local/bin/coderabbit",
    )

    class Runner:
        def run(self, _spec):
            return SimpleNamespace(
                returncode=0,
                stdout='{"type":"complete"}\n',
                stderr="",
                stdout_truncated=True,
                stderr_truncated=False,
                timed_out=False,
            )

    runtime.runner = Runner()

    result = runtime.run(("--version",))

    assert result.stdout_truncated is True
    assert result.stderr_truncated is False


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
    assert GRAFANA_READ_ONLY_TOOLS.isdisjoint(GRAFANA_BLOCKED_TOOLS)
    assert DOCKER_HUB_READ_ONLY_TOOLS.isdisjoint(DOCKER_HUB_BLOCKED_TOOLS)
    assert {"createRepository", "updateRepositoryInfo", "deleteRepository"} <= DOCKER_HUB_BLOCKED_TOOLS
    assert "update_dashboard" in GRAFANA_BLOCKED_TOOLS


def test_credential_ref_contains_only_provenance_not_secret(monkeypatch):
    token = "fixture-context7-token"
    monkeypatch.setenv("CONTEXT7_API_KEY", token)
    config = IntegrationConfig()
    record = Context7Adapter().status(Path.cwd(), config)
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

    assert status_args.integration_target == "status"
    assert paths_args.paths == ["azurpilot/cli.py"]
    assert scan_args.changed is True
    assert review_args.base == "b" * 40


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


def test_coderabbit_state_persists_recovery_provenance_without_secret_values(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    adapter = coderabbit.CodeRabbitAdapter()
    repository_root = tmp_path / "checkout"
    repository_root.mkdir()
    base_sha = "a" * 40
    head_sha = "b" * 40

    adapter._save_state(
        repository_root,
        iterations=1,
        head=head_sha,
        terminal=False,
        base_sha=base_sha,
        repository_identity="alice/example",
        attempt=2,
        operation_id="coderabbit-0123456789abcdef",
        started_at="2026-09-16T00:00:00+00:00",
        provider_state="reviewing",
        active=True,
        complete_received=False,
        last_event_type="review_start",
    )

    state = adapter._load_review_state(repository_root)
    serialized = next((tmp_path / "state").rglob("coderabbit-review.json")).read_text(
        encoding="utf-8"
    )

    assert state["operation_id"] == "coderabbit-0123456789abcdef"
    assert state["base_sha"] == base_sha
    assert state["last_head"] == head_sha
    assert state["reviewed_head"] is None
    assert "token" not in serialized.casefold()


def test_direct_status_has_explicit_non_ready_states_without_mutation(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
    result = Context7Adapter().status(tmp_path, IntegrationConfig())

    assert result.state is IntegrationState.READY
    assert result.reason_code == "INTEGRATION_ENDPOINT_CONFIGURED"
    assert result.evidence.credential.name == "CONTEXT7_API_KEY"
    assert result.evidence.authenticated is None
