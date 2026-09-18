from __future__ import annotations

import asyncio
import inspect
import json
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from azurpilot.cli import CliInvocationError, _render_human, build_parser
from azurpilot.integrations import IntegrationRegistry, coderabbit
from azurpilot.integrations.adapters import (
    DOCKER_HUB_BLOCKED_TOOLS,
    DOCKER_HUB_READ_ONLY_TOOLS,
    GRAFANA_BLOCKED_TOOLS,
    GRAFANA_ENABLED_TOOL_CATEGORIES,
    GRAFANA_EXPECTED_TOOL_NAMES,
    GRAFANA_READ_ONLY_TOOLS,
    GRAFANA_TEMPO_READ_ONLY_TOOLS,
    Context7Adapter,
    GrafanaAdapter,
    SemgrepAdapter,
    _credential,
    _discover_grafana_settings,
)
from azurpilot.integrations.config import IntegrationConfig, load_integration_config
from azurpilot.integrations.contracts import (
    CodeRabbitCycleSummary,
    CredentialSource,
    IntegrationDetails,
    IntegrationEvidence,
    IntegrationEvidenceBundle,
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
from azurpilot.tooling.contracts import (
    AnalysisScope,
    FindingDisposition,
    GitRange,
    OperationState,
    ResultCode,
    ToolingResult,
)
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import StateLayout
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
    assert parsed.findings[0].disposition is FindingDisposition.CONFIRMED
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


def test_canonical_clone_discovery_ignores_unrelated_dirty_repository(
    monkeypatch, tmp_path: Path
):
    distro = coderabbit.WslDistribution("ReviewLinux", "Running", 2)

    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_wsl_identity",
        staticmethod(lambda *_args: (("reviewer", "/home/reviewer"), None)),
    )

    def fake_run(_root, _executable, _distro, arguments, **_kwargs):
        if arguments[:4] == ("--exec", "find", "/home/reviewer", "-maxdepth"):
            stdout = "/home/reviewer/orphan/.git\n/home/reviewer/canonical/.git\n"
            return SimpleNamespace(
                returncode=0,
                stdout=stdout,
                stderr="",
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        if arguments[-2:] == ("rev-parse", "--show-toplevel"):
            clone = arguments[3]
            return SimpleNamespace(
                returncode=0 if clone.endswith("canonical") else 1,
                stdout=clone if clone.endswith("canonical") else "",
                stderr="",
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        if arguments[-3:] == ("remote", "get-url", "origin"):
            clone = arguments[3]
            return SimpleNamespace(
                returncode=0 if clone.endswith("canonical") else 1,
                stdout="https://github.com/AliceLiddell01/AzurPilot-private-Ru.git"
                if clone.endswith("canonical")
                else "",
                stderr="",
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(coderabbit.CodeRabbitAdapter, "_wsl_run", staticmethod(fake_run))

    clones, error_code = coderabbit.CodeRabbitAdapter._discover_review_clones(
        tmp_path,
        "wsl.exe",
        distro,
        "hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert error_code is None
    assert clones == ("/home/reviewer/canonical",)


def test_canonical_clone_discovery_is_order_independent(
    monkeypatch, tmp_path: Path
):
    distro = coderabbit.WslDistribution("ReviewLinux", "Running", 2)
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_wsl_identity",
        staticmethod(lambda *_args: (("reviewer", "/home/reviewer"), None)),
    )
    state = {"reverse": False}

    def fake_run(_root, _executable, _distro, arguments, **_kwargs):
        if arguments[:4] == ("--exec", "find", "/home/reviewer", "-maxdepth"):
            candidates = (
                "/home/reviewer/second/.git\n/home/reviewer/first/.git\n"
                if state["reverse"]
                else "/home/reviewer/first/.git\n/home/reviewer/second/.git\n"
            )
            return SimpleNamespace(
                returncode=0,
                stdout=candidates,
                stderr="",
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        clone = arguments[3]
        if arguments[-2:] == ("rev-parse", "--show-toplevel"):
            output = clone
        elif arguments[-3:] == ("remote", "get-url", "origin"):
            output = "https://github.com/AliceLiddell01/AzurPilot-private-Ru.git"
        else:
            raise AssertionError(arguments)
        return SimpleNamespace(
            returncode=0,
            stdout=output,
            stderr="",
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(coderabbit.CodeRabbitAdapter, "_wsl_run", staticmethod(fake_run))
    first, first_error = coderabbit.CodeRabbitAdapter._discover_review_clones(
        tmp_path,
        "wsl.exe",
        distro,
        "hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )
    state["reverse"] = True
    second, second_error = coderabbit.CodeRabbitAdapter._discover_review_clones(
        tmp_path,
        "wsl.exe",
        distro,
        "hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert first_error is None
    assert second_error is None
    assert first == second == (
        "/home/reviewer/first",
        "/home/reviewer/second",
    )


def test_review_state_classification_does_not_treat_incomplete_as_fresh():
    classify = coderabbit.CodeRabbitAdapter._review_state_classification

    assert classify({}) == "fresh"
    assert classify({"active": True, "operation_id": "coderabbit-1"}) == "active"
    assert classify(
        {
            "active": False,
            "operation_id": "coderabbit-1",
            "provider_state": "timeout",
            "complete_received": False,
        }
    ) == "incomplete_known_failure"
    assert classify(
        {
            "active": False,
            "operation_id": "coderabbit-1",
            "provider_state": "interrupted",
            "complete_received": False,
        }
    ) == "incomplete_unknown"
    assert classify(
        {
            "active": False,
            "operation_id": "coderabbit-1",
            "provider_state": "complete",
            "complete_received": True,
            "terminal": False,
        }
    ) == "complete_non_terminal"
    assert classify(
        {
            "active": False,
            "operation_id": "coderabbit-1",
            "provider_state": "complete",
            "complete_received": True,
            "terminal": True,
        }
    ) == "complete_terminal"
    assert classify(
        {
            "active": False,
            "operation_id": "coderabbit-1",
            "provider_state": "rate_limited",
            "complete_received": False,
        }
    ) == "rate_limited"


def test_clean_stale_canonical_clone_is_prepared_without_destructive_git(
    tmp_path: Path,
):
    old_head = "b" * 40
    target_head = "a" * 40

    def result(
        *, stdout: str = "", returncode: int = 0
    ) -> coderabbit._WslCommandResult:
        return coderabbit._WslCommandResult(
            returncode=returncode,
            stdout=stdout,
            stderr="",
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
        )

    class Runtime:
        clone = "/home/reviewer/canonical"

        def __init__(self):
            self.current_head = old_head
            self.calls: list[tuple[str, ...]] = []

        def git(self, *arguments, timeout=30):
            del timeout
            self.calls.append(arguments)
            if arguments == ("rev-parse", "--show-toplevel"):
                return result(stdout=self.clone)
            if arguments == ("remote", "get-url", "origin"):
                return result(
                    stdout="https://github.com/AliceLiddell01/AzurPilot-private-Ru.git"
                )
            if arguments == ("status", "--porcelain=v1"):
                return result()
            if arguments == ("branch", "--show-current"):
                return result()
            if arguments == ("rev-parse", "HEAD"):
                return result(stdout=self.current_head)
            if arguments[:3] == ("fetch", "--no-tags", "origin"):
                return result()
            if arguments[:2] == ("cat-file", "-e"):
                return result()
            if arguments[:2] == ("checkout", "--detach"):
                self.current_head = arguments[2]
                return result()
            raise AssertionError(arguments)

    runtime = Runtime()
    ready, reason = coderabbit.CodeRabbitAdapter()._prepare_clone(
        runtime, tmp_path, expected_head=target_head,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert ready is True
    assert reason == "CODERABBIT_REVIEW_CLONE_READY"
    assert ("fetch", "--no-tags", "origin", target_head) in runtime.calls
    assert ("checkout", "--detach", target_head) in runtime.calls
    assert not any(
        argument in {"reset", "clean"}
        for call in runtime.calls
        for argument in call
    )


def test_coderabbit_linux_executable_deduplicates_same_cr_symlink():
    class Runtime:
        def command(self, command, *arguments, timeout=30):
            del timeout
            if command == "which":
                path = "/home/reviewer/.local/bin/coderabbit" if arguments[0] == "coderabbit" else "/home/reviewer/.local/bin/cr"
                return coderabbit._WslCommandResult(0, path + "\n", "", False, False, False)
            if command == "realpath":
                return coderabbit._WslCommandResult(
                    0, "/home/reviewer/.local/bin/coderabbit\n", "", False, False, False
                )
            if command == "test":
                return coderabbit._WslCommandResult(0, "", "", False, False, False)
            if command == "file":
                return coderabbit._WslCommandResult(
                    0, "ELF 64-bit LSB executable\n", "", False, False, False
                )
            raise AssertionError(command)

    executable, error_code = coderabbit.CodeRabbitAdapter._linux_executable(
        Runtime(), None
    )

    assert error_code is None
    assert executable == "/home/reviewer/.local/bin/coderabbit"


def test_coderabbit_linux_executable_rejects_distinct_or_windows_candidates():
    class Runtime:
        def command(self, command, *arguments, timeout=30):
            del timeout
            if command == "which":
                path = (
                    "/home/reviewer/.local/bin/coderabbit"
                    if arguments[0] == "coderabbit"
                    else "/opt/bin/cr"
                )
                return coderabbit._WslCommandResult(0, path + "\n", "", False, False, False)
            if command == "realpath":
                return coderabbit._WslCommandResult(
                    0, arguments[0] + "\n", "", False, False, False
                )
            if command == "test":
                return coderabbit._WslCommandResult(0, "", "", False, False, False)
            if command == "file":
                return coderabbit._WslCommandResult(
                    0, "ELF 64-bit executable\n", "", False, False, False
                )
            raise AssertionError(command)

    executable, error_code = coderabbit.CodeRabbitAdapter._linux_executable(
        Runtime(), None
    )
    assert executable is None
    assert error_code == "CODERABBIT_EXECUTABLE_AMBIGUOUS"

    executable, error_code = coderabbit.CodeRabbitAdapter._linux_executable(
        Runtime(), "/opt/bin/coderabbit.exe"
    )
    assert executable is None
    assert error_code == "CODERABBIT_EXECUTABLE_NOT_LINUX_NATIVE"


def test_coderabbit_path_discovery_derives_user_local_dirs_without_shell(
    monkeypatch, tmp_path: Path
):
    calls: list[tuple[str, ...]] = []

    def fake_run(_root, _executable, _distro, arguments, **_kwargs):
        calls.append(arguments)
        return coderabbit._WslCommandResult(
            0, "/usr/bin:/bin\n", "", False, False, False
        )

    monkeypatch.setattr(coderabbit.CodeRabbitAdapter, "_wsl_run", staticmethod(fake_run))
    path, error_code = coderabbit.CodeRabbitAdapter._wsl_path(
        tmp_path,
        "wsl.exe",
        "ReviewLinux",
        user="reviewer",
        home="/home/reviewer",
    )

    assert error_code is None
    assert path == "/usr/bin:/bin:/home/reviewer/.local/bin:/home/reviewer/bin:/home/reviewer/.cargo/bin"
    assert calls == [("--exec", "printenv", "PATH")]


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
    assert "-disable-proxied" not in args
    enabled_tools_index = args.index("-enabled-tools")
    assert args[enabled_tools_index + 1] == GRAFANA_ENABLED_TOOL_CATEGORIES
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


def test_http_probe_uses_file_credential_value(monkeypatch, tmp_path: Path):
    token = "fixture-http-token"
    credential_file = tmp_path / "http-token"
    credential_file.write_text(token + "\n", encoding="utf-8")
    config = IntegrationConfig(
        values={
            "context7": {
                "endpoint": "https://context7.example.test/mcp",
                "credential_env": "GRAFANA_SERVICE_ACCOUNT_TOKEN",
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


def test_grafana_discovery_prefers_confirmed_compose_network_route(monkeypatch, tmp_path: Path):
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


def test_grafana_discovery_rejects_ambiguous_published_routes(monkeypatch, tmp_path: Path):
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

    assert status_args.integration_target == "status"
    assert paths_args.paths == ["azurpilot/cli.py"]
    assert scan_args.changed is True
    assert review_args.base == "b" * 40
    assert cycle_args.integration_action == "cycle"
    assert cycle_args.coderabbit_cycle_action == "start"
    assert cycle_args.base == "c" * 40


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


def _install_fake_coderabbit_runtime(monkeypatch, outputs):
    class FakeRuntime:
        coderabbit_command = "coderabbit"

        def __init__(self):
            self.calls = 0

        def command(self, *_args, **_kwargs):
            self.calls += 1
            if not outputs:
                raise AssertionError(
                    "Провайдер вызван чаще, чем задано подготовленных выходов."
                )
            return outputs.pop(0)

    runtime = FakeRuntime()
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_configured_runtime",
        staticmethod(lambda _root, _settings: (runtime, None)),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_expected_repository",
        staticmethod(lambda _root, _settings: "hosted:github.com/alice/example"),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_prepare_clone",
        lambda _self, *_args, **_kwargs: (True, "CODERABBIT_REVIEW_CLONE_READY"),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_runtime_preflight",
        lambda _self, *_args: (IntegrationState.READY, "CODERABBIT_RUNTIME_READY", ()),
    )
    return runtime


def _complete_result(*, finding: bool = True):
    lines = []
    if finding:
        lines.append(
            json.dumps(
                {
                    "type": "finding",
                    "finding": {
                        "path": "azurpilot/integrations/coderabbit.py",
                        "severity": "major",
                        "comment": "Проверить bounded lifecycle.",
                        "classification": "confirmed",
                    },
                }
            )
        )
    lines.append(json.dumps({"type": "complete"}))
    return coderabbit._WslCommandResult(0, "\n".join(lines) + "\n", "", False, False, False)


def test_coderabbit_cycles_reset_only_explicitly_and_keep_bounded_history(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    base_sha = "a" * 40
    first_head = "b" * 40
    second_head = "c" * 40
    third_head = "d" * 40
    outputs = [
        _complete_result(),
        _complete_result(),
        _complete_result(finding=False),
        _complete_result(),
    ]
    runtime = _install_fake_coderabbit_runtime(monkeypatch, outputs)
    adapter = coderabbit.CodeRabbitAdapter()
    config = IntegrationConfig()

    first = adapter.review(tmp_path / "checkout", config, base_sha=base_sha, head_sha=first_head)
    assert first.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    state = adapter._load_review_state(tmp_path / "checkout")
    assert state["substantive_iterations"] == 1
    assert state["current_cycle_id"].startswith("coderabbit-cycle-")

    second = adapter.review(tmp_path / "checkout", config, base_sha=base_sha, head_sha=second_head)
    assert second.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    assert adapter._load_review_state(tmp_path / "checkout")["substantive_iterations"] == 2

    third = adapter.review(tmp_path / "checkout", config, base_sha=base_sha, head_sha=third_head)
    assert third.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    state = adapter._load_review_state(tmp_path / "checkout")
    assert state["substantive_iterations"] == 3
    assert state["terminal"] is True

    fourth = adapter.review(tmp_path / "checkout", config, base_sha=base_sha, head_sha="e" * 40)
    assert fourth.record.reason_code == "CODERABBIT_REVIEW_ITERATION_BUDGET_EXHAUSTED"
    assert runtime.calls == 3

    started = adapter.start_cycle(tmp_path / "checkout", config, base_sha=base_sha)
    assert started.record.reason_code == "CODERABBIT_REVIEW_CYCLE_STARTED"
    state = adapter._load_review_state(tmp_path / "checkout")
    assert state["substantive_iterations"] == 0
    assert state["terminal"] is False
    assert len(state["previous_cycles"]) == 1
    assert state["previous_cycles"][0]["substantive_iterations"] == 3


def test_coderabbit_review_emits_bounded_heartbeat_without_retrying_provider(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(coderabbit, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    runtime = _install_fake_coderabbit_runtime(monkeypatch, [_complete_result(finding=False)])
    original_command = runtime.command

    def slow_command(*args, **kwargs):
        time.sleep(0.04)
        return original_command(*args, **kwargs)

    runtime.command = slow_command
    events: list[coderabbit.CodeRabbitProgress] = []
    result = coderabbit.CodeRabbitAdapter().review(
        tmp_path / "checkout",
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        progress_callback=events.append,
    )

    assert result.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    assert any(event.phase == "provider_running" for event in events)
    assert events[0].phase == "preflight"
    assert any(event.phase == "provider_started" for event in events)
    assert events[-1].phase == "complete"
    assert runtime.calls == 1


def test_coderabbit_rate_limit_is_temporary_and_does_not_consume_cycle_budget(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    base_sha = "a" * 40
    head_sha = "b" * 40
    rate_result = coderabbit._WslCommandResult(
        1, "", "429 rate limit", False, False, False
    )
    outputs = [rate_result, _complete_result(finding=False)]
    runtime = _install_fake_coderabbit_runtime(monkeypatch, outputs)
    adapter = coderabbit.CodeRabbitAdapter()
    config = IntegrationConfig()
    first = adapter.review(tmp_path / "checkout", config, base_sha=base_sha, head_sha=head_sha)
    assert first.record.reason_code == "CODERABBIT_RATE_LIMITED"
    state = adapter._load_review_state(tmp_path / "checkout")
    assert state["substantive_iterations"] == 0
    assert state["provider_state"] == "rate_limited_waiting"
    assert state["retry_source"] == "estimated"
    assert state["retry_not_before"]
    cycle_reset = adapter.start_cycle(tmp_path / "checkout", config, base_sha=base_sha)
    assert cycle_reset.record.reason_code == "CODERABBIT_RATE_LIMITED"

    blocked = adapter.review(tmp_path / "checkout", config, base_sha=base_sha, head_sha=head_sha)
    assert blocked.record.reason_code == "CODERABBIT_RATE_LIMITED"
    assert runtime.calls == 1

    adapter._save_state(
        tmp_path / "checkout",
        iterations=0,
        head=head_sha,
        terminal=False,
        base_sha=base_sha,
        repository_identity="hosted:github.com/alice/example",
        attempt=1,
        operation_id="coderabbit-rate-limit",
        started_at="2026-09-16T00:00:00+00:00",
        provider_state="rate_limited_waiting",
        active=False,
        complete_received=False,
        last_event_type="error",
        rate_limited_at="2026-09-16T00:00:00+00:00",
        retry_not_before="2020-01-01T00:00:00+00:00",
        retry_source="provider",
    )
    recovered = adapter.review(tmp_path / "checkout", config, base_sha=base_sha, head_sha=head_sha)
    assert recovered.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    assert adapter._load_review_state(tmp_path / "checkout")["substantive_iterations"] == 1
    assert runtime.calls == 2


def test_coderabbit_rate_limit_after_two_reviews_keeps_two_of_three(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    base_sha = "a" * 40
    head_sha = "b" * 40
    outputs = [_complete_result(), _complete_result(), coderabbit._WslCommandResult(1, "", "429", False, False, False)]
    runtime = _install_fake_coderabbit_runtime(monkeypatch, outputs)
    adapter = coderabbit.CodeRabbitAdapter()
    root = tmp_path / "checkout"
    config = IntegrationConfig()
    adapter.review(root, config, base_sha=base_sha, head_sha=head_sha)
    adapter.review(root, config, base_sha=base_sha, head_sha="c" * 40)
    result = adapter.review(root, config, base_sha=base_sha, head_sha="d" * 40)
    assert result.record.reason_code == "CODERABBIT_RATE_LIMITED"
    assert adapter._load_review_state(root)["substantive_iterations"] == 2
    assert runtime.calls == 3


def test_coderabbit_only_complete_result_consumes_substantive_budget(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    output = coderabbit._WslCommandResult(
        0, json.dumps({"type": "status"}) + "\n", "", False, False, False
    )
    _install_fake_coderabbit_runtime(monkeypatch, [output])
    adapter = coderabbit.CodeRabbitAdapter()
    root = tmp_path / "checkout"
    result = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    assert result.record.reason_code == "CODERABBIT_STREAM_TRUNCATED"
    state = adapter._load_review_state(root)
    assert state["substantive_iterations"] == 0
    assert state["complete_received"] is False


def test_coderabbit_fresh_state_has_zero_of_three_and_typed_diagnostics(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    adapter = coderabbit.CodeRabbitAdapter()
    state = adapter._load_review_state(tmp_path / "checkout")
    assert state["current_cycle_id"] == "not-started"
    assert state["substantive_iterations"] == 0
    diagnostics = adapter._review_state_diagnostics(state)
    assert "substantive_iterations=0/3" in diagnostics
    assert any(item.startswith("provider_state=") for item in diagnostics)


def test_cli_renders_coderabbit_cycle_and_all_findings_in_rich_and_json():
    pytest.importorskip("rich")
    record = IntegrationRecord(
        name=IntegrationName.CODERABBIT,
        state=IntegrationState.READY,
        reason_code="CODERABBIT_REVIEW_COMPLETE",
        message="CodeRabbit review завершён; findings: 1.",
        evidence=IntegrationEvidence(route="direct"),
    )
    details = IntegrationDetails(
        action="review",
        integrations=(record,),
        target=IntegrationName.CODERABBIT,
        findings=(
            IntegrationFinding(
                kind="coderabbit",
                identifier="azurpilot/integrations/coderabbit.py",
                path="azurpilot/integrations/coderabbit.py",
                severity="major",
                message="Покажите оператору полный bounded finding.",
                disposition="confirmed",
                resolution="Добавить Rich и JSON summary.",
            ),
        ),
        coderabbit_cycle=CodeRabbitCycleSummary(
            cycle_id="coderabbit-cycle-0123456789abcdef",
            cycle_status="complete",
            substantive_iterations=1,
            provider_state="complete",
            last_reviewed_head="b" * 40,
            previous_cycles_retained=1,
            findings_count=1,
            terminal=False,
            active=False,
        ),
    )
    result = ToolingResult(
        ok=True,
        code=ResultCode.OK,
        state=OperationState.READY,
        message="Проверка прямых внешних интеграций (review) пройдена.",
        details=details,
        evidence=IntegrationEvidenceBundle(generated_at="2026-09-16T00:00:00Z"),
    )
    stdout = StringIO()
    stderr = StringIO()

    _render_human(result, stdout, stderr, no_color=True, verbose=False)

    rendered = stdout.getvalue()
    assert "Цикл ревью CodeRabbit" in rendered
    assert "Сводка замечаний CodeRabbit" in rendered
    assert "бюджет" in rendered
    assert "высокий" in rendered
    assert "bounded" in rendered
    payload = result.model_dump_json()
    assert '"coderabbit_cycle"' in payload
    assert "полный bounded finding" in payload


def test_coderabbit_cycle_start_rejects_active_and_unknown_incomplete_state(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    adapter = coderabbit.CodeRabbitAdapter()
    config = IntegrationConfig()
    root = tmp_path / "checkout"
    base_sha = "a" * 40
    head_sha = "b" * 40
    for provider_state, active in (("reviewing", True), ("interrupted", False)):
        adapter._save_state(
            root,
            iterations=0,
            head=head_sha,
            terminal=False,
            base_sha=base_sha,
            repository_identity="hosted:github.com/alice/example",
            attempt=1,
            operation_id="coderabbit-active",
            started_at="2026-09-16T00:00:00+00:00",
            provider_state=provider_state,
            active=active,
            complete_received=False,
            last_event_type="review_start",
        )
        result = adapter.start_cycle(root, config, base_sha=base_sha)
        assert result.record.reason_code == "CODERABBIT_REVIEW_CYCLE_START_FORBIDDEN"
        assert result.record.state is IntegrationState.INCOMPATIBLE


def test_coderabbit_rate_limit_metadata_is_bounded_and_typed():
    error = coderabbit.CodeRabbitStreamError(
        "CODERABBIT_RATE_LIMITED",
        rate_limited=True,
        retry_not_before="2026-09-16T12:00:00+00:00",
        retry_source="provider",
    )
    assert error.retry_source == "provider"
    estimated, source = coderabbit._estimated_retry_metadata(
        now=coderabbit.datetime(2026, 9, 16, tzinfo=coderabbit.UTC)
    )
    assert estimated.endswith("+00:00")
    assert source == "estimated"


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


def test_coderabbit_legacy_state_migrates_and_corrupt_state_fails_closed(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    layout = StateLayout.for_repository(root)
    layout.ensure()
    path = layout.path("coderabbit-review.json")
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "iterations": 2,
                "terminal": False,
                "active": False,
                "operation_id": "coderabbit-legacy",
                "started_at": "2026-09-16T00:00:00+00:00",
                "repository_identity": "hosted:github.com/alice/example",
                "base_sha": "a" * 40,
                "last_head": "b" * 40,
                "provider_state": "complete",
                "complete_received": True,
                "reviewed_head": "b" * 40,
                "findings_count": 1,
                "attempt": 2,
            }
        ),
        encoding="utf-8",
    )
    adapter = coderabbit.CodeRabbitAdapter()
    state = adapter._load_review_state(root)
    assert state["schema_version"] == coderabbit.REVIEW_STATE_SCHEMA_VERSION
    assert state["substantive_iterations"] == 2
    assert state["current_cycle_id"].startswith("legacy-coderabbit-")
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2

    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ToolingError, match="повреждено"):
        adapter._load_review_state(root)


def test_coderabbit_state_normalization_validates_before_writing_derived_fields(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    layout = StateLayout.for_repository(root)
    layout.ensure()
    path = layout.path("coderabbit-review.json")
    payload = coderabbit._default_review_state()
    payload.update(
        {
            "current_cycle_id": "legacy-coderabbit-0123456789abcdef",
            "provider_state": "rate_limited_waiting",
            "rate_limited_at": "2026-09-16T00:00:00+00:00",
            "findings_count": 129,
        }
    )
    original = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ToolingError, match="Счётчик findings"):
        coderabbit.CodeRabbitAdapter()._load_review_state(root)

    assert path.read_text(encoding="utf-8") == original


def test_coderabbit_cycle_reset_preserves_provider_quota_metadata(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    adapter = coderabbit.CodeRabbitAdapter()
    config = IntegrationConfig()
    root = tmp_path / "checkout"
    base_sha = "a" * 40
    head_sha = "b" * 40
    quota = {
        "state": "provider_observed",
        "rate_limited_at": "2026-09-16T00:00:00+00:00",
        "retry_not_before": "2026-09-16T01:00:00+00:00",
        "retry_source": "provider",
    }
    adapter._save_state(
        root,
        iterations=1,
        head=head_sha,
        terminal=False,
        base_sha=base_sha,
        repository_identity="hosted:github.com/alice/example",
        attempt=1,
        operation_id="coderabbit-complete",
        started_at="2026-09-16T00:00:00+00:00",
        provider_state="complete",
        active=False,
        complete_received=True,
        last_event_type="complete",
        findings_count=1,
        reviewed_head=head_sha,
        provider_quota=quota,
    )
    result = adapter.start_cycle(root, config, base_sha=base_sha)
    assert result.record.reason_code == "CODERABBIT_REVIEW_CYCLE_STARTED"
    assert adapter._load_review_state(root)["provider_quota"] == quota


def test_direct_status_is_ready_without_credential_and_without_mutation(
    monkeypatch, tmp_path: Path
):
    monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
    for variable in (
        "AZURPILOT_USER_CONFIG",
        "AZURPILOT_MACHINE_CONFIG",
        "AZURPILOT_CONFIG_FILE",
        "AZURPILOT_CONTEXT7_ENDPOINT",
    ):
        monkeypatch.delenv(variable, raising=False)
    result = Context7Adapter().status(tmp_path, IntegrationConfig())

    assert result.state is IntegrationState.READY
    assert result.reason_code == "INTEGRATION_ENDPOINT_CONFIGURED"
    assert result.evidence.credential.name == "CONTEXT7_API_KEY"
    assert result.evidence.authenticated is None
