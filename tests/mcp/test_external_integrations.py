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
    GRAFANA_REQUIRED_READ_ONLY_TOOLS,
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
from azurpilot.integrations.service import ADAPTER_ORDER, AdapterOutcome
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


def test_persistent_clone_is_validated_without_detaching_or_rewriting_git(
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
            raise AssertionError(arguments)

    runtime = Runtime()
    ready, reason = coderabbit.CodeRabbitAdapter()._prepare_clone(
        runtime, tmp_path, expected_head=target_head,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert ready is True
    assert reason == "CODERABBIT_MANAGED_CLONE_READY"
    assert not any(target_head in call for call in runtime.calls)
    assert not any(
        argument in {"reset", "clean", "checkout", "switch"}
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


def test_coderabbit_recovery_closes_stale_active_state_without_iteration(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    adapter = coderabbit.CodeRabbitAdapter()
    root = tmp_path / "checkout"
    root.mkdir()
    base_sha, head_sha = "a" * 40, "b" * 40
    adapter._save_state(root, iterations=0, head=head_sha, terminal=False, base_sha=base_sha,
                        repository_identity="alice/example", attempt=1, operation_id="coderabbit-0123456789abcdef",
                        started_at="2026-09-16T00:00:00+00:00", provider_state="reviewing", active=True,
                        complete_received=False, last_event_type="review_start")
    class Runtime:
        def command(self, *args, **kwargs):
            assert args[:3] == ("pgrep", "-x", "coderabbit")
            return coderabbit._WslCommandResult(1, "", "", False, False, False)
    monkeypatch.setattr(adapter, "_configured_runtime", lambda *_: (Runtime(), None))
    monkeypatch.setattr(adapter, "_settings", lambda _: {})
    outcome = adapter.recover_interrupted_review(root, IntegrationConfig())
    assert outcome.record.reason_code == "CODERABBIT_REVIEW_RECOVERED"
    state = adapter._load_review_state(root)
    assert state["active"] is False
    assert state["complete_received"] is False
    assert state["reviewed_head"] is None
    assert state["substantive_iterations"] == 0
    assert state["previous_cycles"][-1]["terminal_reason"] == "external_interruption_recovered"


def _install_fake_coderabbit_runtime(monkeypatch, outputs):
    class FakeRuntime:
        coderabbit_command = "coderabbit"

        def __init__(self):
            self.calls = 0

        def command(self, *args, **_kwargs):
            # Предварительная проверка возможности не считается содержательным вызовом.
            if "--agent" in args:
                self.calls += 1
                if not outputs:
                    raise AssertionError(
                        "Провайдер вызван чаще, чем задано подготовленных выходов."
                    )
                return outputs.pop(0)
            if args[-1:] == ("--help",):
                return coderabbit._WslCommandResult(
                    0,
                    "review findings --agent --committed --base-commit\n",
                    "",
                    False,
                    False,
                    False,
                )
            raise AssertionError(f"Неожиданная команда capability: {args!r}")

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


def test_coderabbit_findings_persist_and_retrieve_after_new_adapter(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    base_sha = "a" * 40
    head_sha = "b" * 40
    _install_fake_coderabbit_runtime(monkeypatch, [_complete_result()])
    root = tmp_path / "checkout"
    config = IntegrationConfig()
    adapter = coderabbit.CodeRabbitAdapter()

    reviewed = adapter.review(root, config, base_sha=base_sha, head_sha=head_sha)
    assert len(reviewed.findings) == 1
    state = adapter._load_review_state(root)
    assert len(state["findings"]) == 1
    assert "comment" not in json.dumps(state["findings"], ensure_ascii=False)
    assert state["substantive_iterations"] == 1

    fresh = coderabbit.CodeRabbitAdapter()
    retrieved = fresh.findings(root, config, base_sha=base_sha, head_sha=head_sha)
    assert retrieved.record.reason_code == "CODERABBIT_REVIEW_FINDINGS_READY"
    assert retrieved.findings == reviewed.findings
    assert fresh._load_review_state(root)["substantive_iterations"] == 1


def test_coderabbit_findings_distinguishes_unavailable_capability(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    adapter = coderabbit.CodeRabbitAdapter()
    config = IntegrationConfig()
    base_sha = "a" * 40
    head_sha = "b" * 40
    adapter._save_state(
        root,
        iterations=1,
        head=head_sha,
        terminal=False,
        base_sha=base_sha,
        repository_identity="hosted:github.com/alice/example",
        attempt=1,
        operation_id="coderabbit-legacy",
        started_at="2026-09-16T00:00:00+00:00",
        provider_state="complete",
        active=False,
        complete_received=True,
        last_event_type="complete",
        findings_count=1,
        reviewed_head=head_sha,
    )
    monkeypatch.setattr(
        adapter,
        "_configured_runtime",
        lambda _root, _settings: (None, "CODERABBIT_WSL_UNAVAILABLE"),
    )
    result = adapter.findings(root, config, base_sha=base_sha, head_sha=head_sha)
    assert result.record.reason_code == "CODERABBIT_REVIEW_FINDINGS_CAPABILITY_UNAVAILABLE"
    assert result.findings == ()
    assert adapter._load_review_state(root)["substantive_iterations"] == 1


def test_coderabbit_findings_rejects_head_and_digest_mismatch(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    base_sha = "a" * 40
    head_sha = "b" * 40
    _install_fake_coderabbit_runtime(monkeypatch, [_complete_result()])
    root = tmp_path / "checkout"
    config = IntegrationConfig()
    adapter = coderabbit.CodeRabbitAdapter()
    adapter.review(root, config, base_sha=base_sha, head_sha=head_sha)

    mismatch = adapter.findings(root, config, base_sha=base_sha, head_sha="c" * 40)
    assert mismatch.record.reason_code == "CODERABBIT_REVIEW_FINDINGS_HEAD_MISMATCH"
    assert mismatch.findings == ()

    path = StateLayout.for_repository(root).path("coderabbit-review.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["findings_digest"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ToolingError, match="Digest findings"):
        adapter._load_review_state(root)


def test_coderabbit_zero_findings_is_distinct_from_missing_evidence(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    base_sha = "a" * 40
    head_sha = "b" * 40
    _install_fake_coderabbit_runtime(monkeypatch, [_complete_result(finding=False)])
    root = tmp_path / "checkout"
    config = IntegrationConfig()
    adapter = coderabbit.CodeRabbitAdapter()
    adapter.review(root, config, base_sha=base_sha, head_sha=head_sha)

    retrieved = coderabbit.CodeRabbitAdapter().findings(
        root, config, base_sha=base_sha, head_sha=head_sha
    )
    assert retrieved.record.reason_code == "CODERABBIT_REVIEW_FINDINGS_READY"
    assert retrieved.findings == ()
    assert retrieved.coderabbit_cycle is not None
    assert retrieved.coderabbit_cycle.findings_count == 0


def test_coderabbit_findings_cli_requires_exact_references():
    parser = build_parser()
    args = parser.parse_args(["integrations", "coderabbit", "findings", "--base", "a" * 40, "--head", "b" * 40])
    assert args.base == "a" * 40
    assert args.head == "b" * 40


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


def test_coderabbit_cycle_start_accepts_new_base_after_completed_cycle(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    adapter = coderabbit.CodeRabbitAdapter()
    config = IntegrationConfig()
    root = tmp_path / "checkout"
    old_base = "a" * 40
    new_base = "b" * 40
    adapter._save_state(
        root,
        iterations=1,
        head="c" * 40,
        terminal=False,
        base_sha=old_base,
        repository_identity="hosted:github.com/alice/example",
        attempt=1,
        operation_id="coderabbit-complete",
        started_at="2026-09-16T00:00:00+00:00",
        provider_state="complete",
        active=False,
        complete_received=True,
        last_event_type="complete",
        findings_count=1,
        reviewed_head="c" * 40,
    )

    result = adapter.start_cycle(root, config, base_sha=new_base)

    assert result.record.reason_code == "CODERABBIT_REVIEW_CYCLE_STARTED"
    state = adapter._load_review_state(root)
    assert state["base_sha"] == new_base
    assert state["substantive_iterations"] == 0
    assert state["previous_cycles"][-1]["base_sha"] == old_base


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


def test_coderabbit_rate_limit_does_not_create_synthetic_retry_time(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    runtime = _install_fake_coderabbit_runtime(
        monkeypatch,
        [coderabbit._WslCommandResult(1, "", "429 rate limit", False, False, False)],
    )
    result = coderabbit.CodeRabbitAdapter().review(
        tmp_path / "checkout", IntegrationConfig(), base_sha="a" * 40, head_sha="b" * 40
    )
    assert result.record.reason_code == "CODERABBIT_RATE_LIMITED"
    state = coderabbit.CodeRabbitAdapter()._load_review_state(tmp_path / "checkout")
    assert state["retry_not_before"] is None
    assert state["retry_source"] == "unknown"
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
    assert state["retry_source"] == "unknown"
    assert state["retry_not_before"] is None
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
    assert "Замечание 1" in rendered
    assert "Рекомендация CodeRabbit" in rendered
    assert "Независимая классификация" in rendered
    assert "Принятое решение" in rendered
    assert "принято" in rendered
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


def _managed_clone_result(
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


class _ManagedCloneRuntime:
    """Минимальный typed fake для проверки non-destructive Git state machine."""

    distro = "ReviewLinux"
    user = "reviewer"
    home = "/home/reviewer"
    clone = "/home/reviewer/canonical"

    def __init__(
        self,
        *,
        current_branch: str = "personal/stable",
        status: str = "",
        counts: str = "0 0",
        head: str = "a" * 40,
        remote_head: str = "a" * 40,
        upstream: str = "origin/personal/stable",
        remote_url: str = "https://github.com/AliceLiddell01/AzurPilot-private-Ru.git",
    ) -> None:
        self.current_branch = current_branch
        self.status = status
        self.counts = counts
        self.head = head
        self.remote_head = remote_head
        self.upstream = upstream
        self.remote_url = remote_url
        self.after_merge = False
        self.calls: list[tuple[str, ...]] = []

    def git(self, *arguments: str, timeout: float = 30.0):
        del timeout
        self.calls.append(arguments)
        if arguments == ("fetch", "--no-tags", "--prune", "origin"):
            return _managed_clone_result()
        if arguments == ("rev-parse", "--show-toplevel"):
            return _managed_clone_result(stdout=self.clone)
        if arguments == ("remote", "get-url", "origin"):
            return _managed_clone_result(stdout=self.remote_url)
        if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
            return _managed_clone_result(stdout=self.status)
        if arguments == ("status", "--porcelain=v1"):
            return _managed_clone_result(stdout=self.status)
        if arguments == ("rev-parse", "HEAD"):
            head = self.remote_head if self.after_merge else self.head
            return _managed_clone_result(stdout=head)
        if arguments == ("branch", "--show-current"):
            return _managed_clone_result(stdout=self.current_branch)
        if arguments == (
            "rev-parse",
            "refs/remotes/origin/personal/stable",
        ):
            return _managed_clone_result(stdout=self.remote_head)
        if arguments == (
            "rev-list",
            "--left-right",
            "--count",
            "HEAD...refs/remotes/origin/personal/stable",
        ):
            counts = "0 0" if self.after_merge else self.counts
            return _managed_clone_result(stdout=counts)
        if arguments == (
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ):
            return _managed_clone_result(stdout=self.upstream)
        if arguments == (
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/personal/stable",
        ):
            return _managed_clone_result(returncode=1)
        if arguments == ("switch", "personal/stable") or arguments == (
            "switch",
            "--create",
            "personal/stable",
            "--track",
            "origin/personal/stable",
        ):
            self.current_branch = "personal/stable"
            return _managed_clone_result()
        if arguments == (
            "branch",
            "--set-upstream-to",
            "origin/personal/stable",
            "personal/stable",
        ):
            self.upstream = "origin/personal/stable"
            return _managed_clone_result()
        if arguments == ("merge", "--ff-only", "origin/personal/stable"):
            self.after_merge = True
            return _managed_clone_result()
        raise AssertionError(arguments)


@pytest.mark.parametrize(
    ("runtime_kwargs", "expected_state"),
    [
        ({"counts": "0 0"}, "READY"),
        ({"counts": "0 2"}, "SYNCABLE"),
        ({"current_branch": ""}, "DETACHED_LEGACY"),
        ({"status": " M tracked.py"}, "DIRTY"),
        ({"counts": "2 0"}, "AHEAD"),
        ({"counts": "2 3"}, "DIVERGED"),
        ({"current_branch": "feature"}, "WRONG_BRANCH"),
        ({"upstream": "origin/other"}, "WRONG_UPSTREAM"),
        ({"remote_head": ""}, "REMOTE_UNAVAILABLE"),
    ],
)
def test_managed_clone_snapshot_distinguishes_typed_states(
    tmp_path: Path, runtime_kwargs: dict[str, str], expected_state: str
):
    runtime = _ManagedCloneRuntime(**runtime_kwargs)
    snapshot = coderabbit.CodeRabbitAdapter()._managed_clone_snapshot(
        tmp_path,
        runtime,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
        remote="origin",
        branch="personal/stable",
    )

    assert snapshot.sync_state == expected_state
    assert snapshot.repository_identity == snapshot.origin_identity
    assert snapshot.upstream_ref == "origin/personal/stable"


@pytest.mark.parametrize(
    ("code", "expected_state"),
    [
        ("CODERABBIT_REVIEW_CHECKOUT_ALREADY_EXISTS", IntegrationState.UNAVAILABLE),
        ("CODERABBIT_REVIEW_CHECKOUT_READY", IntegrationState.DEGRADED),
        ("CODERABBIT_RUNTIME_READY", IntegrationState.DEGRADED),
    ],
)
def test_coderabbit_state_for_code_uses_exact_ready_suffix(
    code: str, expected_state: IntegrationState
):
    assert coderabbit.CodeRabbitAdapter._state_for_code(code) is expected_state


@pytest.mark.parametrize(
    ("runtime_kwargs", "expected_code"),
    [
        ({"status": "?? untracked.txt"}, ResultCode.TOOLING_OPERATION_CONFLICT),
        ({"counts": "2 0"}, ResultCode.TOOLING_UPDATE_LOCAL_AHEAD),
        ({"counts": "2 3"}, ResultCode.TOOLING_UPDATE_DIVERGED),
    ],
)
def test_managed_clone_reconcile_never_discards_dirty_or_local_commits(
    tmp_path: Path,
    runtime_kwargs: dict[str, str],
    expected_code: ResultCode,
):
    runtime = _ManagedCloneRuntime(**runtime_kwargs)

    with pytest.raises(ToolingError) as error:
        coderabbit.CodeRabbitAdapter()._sync_managed_clone(
            tmp_path,
            runtime,
            expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
        )

    assert error.value.code is expected_code
    assert not any(
        argument in {"reset", "clean", "checkout", "switch", "merge"}
        for call in runtime.calls
        for argument in call
    )


def test_managed_clone_reconcile_fast_forwards_clean_stale_clone(
    tmp_path: Path,
):
    runtime = _ManagedCloneRuntime(
        counts="0 2",
        head="a" * 40,
        remote_head="b" * 40,
    )

    snapshot = coderabbit.CodeRabbitAdapter()._sync_managed_clone(
        tmp_path,
        runtime,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert snapshot.head_sha == "b" * 40
    assert snapshot.ownership_state == "OWNED"
    assert ("merge", "--ff-only", "origin/personal/stable") in runtime.calls
    assert not any(
        argument in {"reset", "clean", "checkout"}
        for call in runtime.calls
        for argument in call
    )


def test_managed_clone_reconcile_adopts_clean_detached_legacy_clone(
    tmp_path: Path,
):
    runtime = _ManagedCloneRuntime(current_branch="")

    snapshot = coderabbit.CodeRabbitAdapter()._sync_managed_clone(
        tmp_path,
        runtime,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert snapshot.ownership_state == "OWNED"
    assert runtime.current_branch == "personal/stable"
    assert (
        "switch",
        "--create",
        "personal/stable",
        "--track",
        "origin/personal/stable",
    ) in runtime.calls


def test_managed_clone_reconcile_rejects_wrong_origin_without_mutation(
    tmp_path: Path,
):
    runtime = _ManagedCloneRuntime(
        remote_url="https://github.com/example/unrelated.git",
    )

    with pytest.raises(ToolingError) as error:
        coderabbit.CodeRabbitAdapter()._sync_managed_clone(
            tmp_path,
            runtime,
            expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
        )

    assert error.value.code is ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED
    assert not any(
        argument in {"reset", "clean", "checkout", "switch", "merge"}
        for call in runtime.calls
        for argument in call
    )


class _ReviewWorktreeRuntime(coderabbit._WslRuntime):
    """Fake WSL runtime с общей трассировкой lifecycle linked worktree."""

    def __init__(self, root: Path, *, clone: str, shared=None, worktrees=None) -> None:
        super().__init__(
            root,
            "wsl.exe",
            distro="ReviewLinux",
            user="reviewer",
            home="/home/reviewer",
            clone=clone,
            repository_identity="hosted:github.com/aliceliddell01/azurpilot-private-ru",
            coderabbit_executable="/home/reviewer/.local/bin/coderabbit",
            coderabbit_command="/home/reviewer/.local/bin/coderabbit",
        )
        self.shared = shared if shared is not None else []
        self.worktrees = worktrees if worktrees is not None else {
            "/home/reviewer/canonical": "a" * 40
        }
        self.current_branch = "personal/stable" if clone.endswith("canonical") else ""
        self.head = "a" * 40 if clone.endswith("canonical") else "c" * 40
        self.provider_alive = False
        self.provider_cwd = self.clone
        self.pgrep_status = None
        self.ps_stdout = None
        self.ps_returncode = 0
        self.checkout_status = ""
        self.missing_refs: set[str] = set()

    def with_clone(self, clone: str):
        result = _ReviewWorktreeRuntime(
            self.root,
            clone=clone,
            shared=self.shared,
            worktrees=self.worktrees,
        )
        result.provider_alive = self.provider_alive
        result.provider_cwd = self.provider_cwd
        result.pgrep_status = self.pgrep_status
        result.ps_stdout = self.ps_stdout
        result.ps_returncode = self.ps_returncode
        result.checkout_status = self.checkout_status
        result.missing_refs = self.missing_refs
        return result

    def run(self, args: tuple[str, ...], *, timeout: float = 30.0):
        del timeout
        self.shared.append((self.clone, *args))
        if args[:3] == ("--exec", "test", "-e"):
            return _managed_clone_result(returncode=0 if self.clone.endswith("review") else 1)
        if args[:2] == ("--exec", "mkdir"):
            return _managed_clone_result()
        if args[:2] == ("--exec", "realpath"):
            return _managed_clone_result(stdout=args[-1] + "\n")
        raise AssertionError(args)

    def command(self, command: str, *arguments: str, timeout: float = 30.0):
        del timeout
        self.shared.append((self.clone, command, *arguments))
        if command == "pgrep" and self.pgrep_status is not None:
            return _managed_clone_result(returncode=self.pgrep_status)
        if command == "pgrep" and self.provider_alive:
            return _managed_clone_result(returncode=0, stdout="1234\n")
        if command == "pgrep":
            return _managed_clone_result(returncode=1)
        if command == "ps":
            if self.ps_stdout is not None:
                return _managed_clone_result(
                    returncode=self.ps_returncode,
                    stdout=self.ps_stdout,
                )
            if self.provider_alive:
                return _managed_clone_result(
                    stdout=f"1234 /home/reviewer/.local/bin/coderabbit review --committed --base-commit {'b' * 40}\n"
                )
            return _managed_clone_result()
        if command == "readlink":
            return _managed_clone_result(stdout=self.provider_cwd + "\n")
        if command == "realpath":
            return _managed_clone_result(stdout=arguments[-1] + "\n")
        raise AssertionError((command, *arguments))

    def git(self, *arguments: str, timeout: float = 30.0):
        del timeout
        self.shared.append((self.clone, *arguments))
        if arguments == ("rev-parse", "--show-toplevel"):
            return _managed_clone_result(stdout=self.clone)
        if arguments == ("remote", "get-url", "origin"):
            return _managed_clone_result(
                stdout="https://github.com/AliceLiddell01/AzurPilot-private-Ru.git"
            )
        if arguments == ("status", "--porcelain=v1"):
            return _managed_clone_result(
                stdout=self.checkout_status if "/reviews/" in self.clone else ""
            )
        if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
            return _managed_clone_result(
                stdout=self.checkout_status if "/reviews/" in self.clone else ""
            )
        if arguments == ("rev-parse", "HEAD"):
            return _managed_clone_result(stdout=self.head)
        if arguments == ("branch", "--show-current"):
            return _managed_clone_result(stdout=self.current_branch)
        if arguments == (
            "rev-parse",
            "refs/remotes/origin/personal/stable",
        ):
            return _managed_clone_result(stdout="a" * 40)
        if arguments == (
            "rev-list",
            "--left-right",
            "--count",
            "HEAD...refs/remotes/origin/personal/stable",
        ):
            return _managed_clone_result(stdout="0 0")
        if arguments == (
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ):
            return _managed_clone_result(stdout="origin/personal/stable")
        if arguments == ("fetch", "--no-tags", "--prune", "origin"):
            return _managed_clone_result()
        if arguments[:3] == ("fetch", "--no-tags", "origin"):
            return _managed_clone_result()
        if arguments[:2] == ("cat-file", "-e"):
            reference = arguments[2].split("^{", 1)[0]
            if reference in self.missing_refs:
                return _managed_clone_result(returncode=1)
            return _managed_clone_result()
        if arguments == ("worktree", "list", "--porcelain"):
            payload = []
            for path, head in self.worktrees.items():
                payload.extend((f"worktree {path}", f"HEAD {head}"))
                if path.endswith("canonical"):
                    payload.append("branch refs/heads/personal/stable")
                else:
                    payload.append("detached")
                payload.append("")
            return _managed_clone_result(stdout="\n".join(payload))
        if arguments[:2] == ("worktree", "add"):
            self.worktrees[arguments[3]] = arguments[4]
            return _managed_clone_result()
        if arguments[:2] == ("worktree", "remove"):
            self.worktrees.pop(arguments[2], None)
            return _managed_clone_result()
        raise AssertionError(arguments)


def test_review_uses_owned_detached_worktree_and_preserves_managed_clone(
    tmp_path: Path,
):
    shared: list[tuple[str, ...]] = []
    managed = _ReviewWorktreeRuntime(
        tmp_path,
        clone="/home/reviewer/canonical",
        shared=shared,
    )
    adapter = coderabbit.CodeRabbitAdapter()
    checkout_runtime, checkout_path, reason = adapter._prepare_review_checkout(
        managed,
        tmp_path,
        operation_id="coderabbit-0123456789abcdef",
        expected_base="b" * 40,
        expected_head="c" * 40,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert reason == "CODERABBIT_REVIEW_CHECKOUT_READY"
    assert checkout_path == "/home/reviewer/.cache/azurpilot/coderabbit/reviews/coderabbit-0123456789abcdef"
    assert checkout_runtime.clone == checkout_path
    assert checkout_runtime.current_branch == ""
    assert managed.clone == "/home/reviewer/canonical"
    assert managed.current_branch == "personal/stable"
    assert (
        managed.clone,
        "worktree",
        "add",
        "--detach",
        checkout_path,
        "c" * 40,
    ) in shared
    assert (
        managed.clone,
        "fetch",
        "--no-tags",
        "origin",
        "b" * 40,
        "c" * 40,
    ) in shared
    assert (
        managed.clone,
        "cat-file",
        "-e",
        f"{'b' * 40}^{{commit}}",
    ) in shared

    cleaned, cleanup_reason = adapter._cleanup_review_checkout(
        managed,
        checkout_path,
        operation_id="coderabbit-0123456789abcdef",
        expected_head="c" * 40,
        base_sha="b" * 40,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert cleaned is True
    assert cleanup_reason == "CODERABBIT_REVIEW_CHECKOUT_REMOVED"
    assert (
        managed.clone,
        "worktree",
        "remove",
        checkout_path,
    ) in shared


def test_review_worktree_cleanup_is_blocked_for_live_provider_or_dirty_state(
    tmp_path: Path,
):
    shared: list[tuple[str, ...]] = []
    managed = _ReviewWorktreeRuntime(
        tmp_path,
        clone="/home/reviewer/canonical",
        shared=shared,
    )
    adapter = coderabbit.CodeRabbitAdapter()
    checkout_path = "/home/reviewer/.cache/azurpilot/coderabbit/reviews/coderabbit-0123456789abcdef"

    managed.provider_alive = True
    managed.provider_cwd = checkout_path
    managed.worktrees[checkout_path] = "c" * 40
    cleaned, reason = adapter._cleanup_review_checkout(
        managed,
        checkout_path,
        operation_id="coderabbit-0123456789abcdef",
        expected_head="c" * 40,
        base_sha="b" * 40,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )
    assert cleaned is False
    assert reason == "CODERABBIT_REVIEW_OPERATION_STILL_ALIVE"

    managed.provider_alive = False
    managed.checkout_status = "?? unexpected.txt"
    cleaned, reason = adapter._cleanup_review_checkout(
        managed,
        checkout_path,
        operation_id="coderabbit-0123456789abcdef",
        expected_head="c" * 40,
        base_sha="b" * 40,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )
    assert cleaned is False
    assert reason == "CODERABBIT_REVIEW_CHECKOUT_DIRTY"
    assert not any(call[1:3] == ("worktree", "remove") for call in shared)


@pytest.mark.parametrize("missing_ref", ["base", "head"])
def test_review_checkout_requires_each_exact_commit(
    missing_ref: str, tmp_path: Path
):
    managed = _ReviewWorktreeRuntime(
        tmp_path,
        clone="/home/reviewer/canonical",
    )
    base_sha = "b" * 40
    head_sha = "c" * 40
    managed.missing_refs.add(base_sha if missing_ref == "base" else head_sha)

    _, checkout_path, reason = coderabbit.CodeRabbitAdapter()._prepare_review_checkout(
        managed,
        tmp_path,
        operation_id="coderabbit-0123456789abcdef",
        expected_base=base_sha,
        expected_head=head_sha,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert checkout_path is None
    assert reason == "CODERABBIT_REVIEW_TARGET_UNAVAILABLE"
    assert not any(call[1:3] == ("worktree", "add") for call in managed.shared)


def test_review_checkout_rejects_fetched_head_mismatch(
    tmp_path: Path,
):
    managed = _ReviewWorktreeRuntime(
        tmp_path,
        clone="/home/reviewer/canonical",
    )

    _, checkout_path, reason = coderabbit.CodeRabbitAdapter()._prepare_review_checkout(
        managed,
        tmp_path,
        operation_id="coderabbit-0123456789abcdef",
        expected_base="b" * 40,
        expected_head="d" * 40,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert checkout_path is not None
    assert reason == "CODERABBIT_REVIEW_HEAD_MISMATCH"


def test_review_cleanup_preserves_foreign_linked_worktree(
    tmp_path: Path,
):
    managed = _ReviewWorktreeRuntime(
        tmp_path,
        clone="/home/reviewer/canonical",
    )
    checkout_path = "/home/reviewer/.cache/azurpilot/coderabbit/reviews/coderabbit-0123456789abcdef"
    managed.worktrees[checkout_path] = "d" * 40

    cleaned, reason = coderabbit.CodeRabbitAdapter()._cleanup_review_checkout(
        managed,
        checkout_path,
        operation_id="coderabbit-0123456789abcdef",
        expected_head="c" * 40,
        base_sha="b" * 40,
        expected_repository="hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    assert cleaned is False
    assert reason == "CODERABBIT_REVIEW_CHECKOUT_OWNERSHIP_UNKNOWN"
    assert managed.worktrees[checkout_path] == "d" * 40
    assert not any(call[1:3] == ("worktree", "remove") for call in managed.shared)


def test_review_reconciles_managed_wsl_clone_before_checkout(monkeypatch, tmp_path: Path):
    managed = _ReviewWorktreeRuntime(
        tmp_path,
        clone="/home/reviewer/canonical",
    )
    adapter = coderabbit.CodeRabbitAdapter()
    calls: list[str] = []

    monkeypatch.setattr(adapter, "_settings", lambda _config: {})
    monkeypatch.setattr(
        adapter,
        "_configured_runtime",
        lambda _root, _settings: (managed, None),
    )
    monkeypatch.setattr(
        adapter,
        "_expected_repository",
        lambda _root, _settings: "hosted:github.com/aliceliddell01/azurpilot-private-ru",
    )

    def reconcile(_root, _runtime, *, expected_repository):
        assert expected_repository == "hosted:github.com/aliceliddell01/azurpilot-private-ru"
        calls.append("reconcile")

    monkeypatch.setattr(adapter, "_sync_managed_clone", reconcile)
    monkeypatch.setattr(
        adapter,
        "_prepare_review_checkout",
        lambda *_args, **_kwargs: (
            managed,
            None,
            "CODERABBIT_MANAGEMENT_BRANCH_NOT_CONFIGURED",
        ),
    )

    outcome = adapter.review(
        tmp_path,
        coderabbit.IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert calls == ["reconcile"]
    assert outcome.record.reason_code == "CODERABBIT_MANAGEMENT_BRANCH_NOT_CONFIGURED"


@pytest.mark.parametrize("probe_code", [2, 3])
def test_coderabbit_liveness_probe_errors_are_unknown(
    probe_code: int, tmp_path: Path
):
    runtime = _ReviewWorktreeRuntime(
        tmp_path,
        clone="/home/reviewer/canonical",
    )
    runtime.pgrep_status = probe_code

    assert (
        coderabbit.CodeRabbitAdapter()._provider_liveness(
            runtime,
            "/home/reviewer/.cache/azurpilot/coderabbit/reviews/coderabbit-op",
            base_sha="b" * 40,
        )
        == "unknown"
    )


def test_coderabbit_liveness_ignores_unrelated_and_rejects_stale_pid(
    tmp_path: Path,
):
    runtime = _ReviewWorktreeRuntime(
        tmp_path,
        clone="/home/reviewer/canonical",
    )
    runtime.ps_stdout = (
        "1234 /home/reviewer/.local/bin/coderabbit review --committed "
        + "--base-commit "
        + "c" * 40
        + "\n"
    )
    assert (
        coderabbit.CodeRabbitAdapter()._provider_liveness(
            runtime,
            "/home/reviewer/.cache/azurpilot/coderabbit/reviews/coderabbit-op",
            base_sha="b" * 40,
        )
        == "absent"
    )

    runtime.pgrep_status = 0
    runtime.ps_stdout = ""
    assert (
        coderabbit.CodeRabbitAdapter()._provider_liveness(
            runtime,
            "/home/reviewer/.cache/azurpilot/coderabbit/reviews/coderabbit-op",
            base_sha="b" * 40,
        )
        == "unknown"
    )
