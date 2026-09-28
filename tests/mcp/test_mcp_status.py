from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import dev_tools.integration_contract_gate as gate
import dev_tools.mcp_status as status
from azurpilot.integrations.contracts import (
    IntegrationEvidence,
    IntegrationName,
    IntegrationRecord,
    IntegrationState,
)
from azurpilot.integrations.mcp_client import FreshMcpClientResult
from azurpilot.tooling.contracts import ResultCode
from module.mcp_shared import local_http_auth, local_http_supervisor
from tests.support.paths import REPOSITORY_ROOT


def _record(
    name: IntegrationName,
    state: IntegrationState = IntegrationState.READY,
) -> IntegrationRecord:
    return IntegrationRecord(
        name=name,
        state=state,
        reason_code=(
            "INTEGRATION_DIRECT_READY"
            if state is IntegrationState.READY
            else "INTEGRATION_NOT_READY"
        ),
        message=(
            "Прямой адаптер подтверждён."
            if state is IntegrationState.READY
            else "Адаптер не подтверждён."
        ),
        evidence=IntegrationEvidence(
            route="direct_test",
            configured=state is not IntegrationState.NOT_CONFIGURED,
            reachable=state is IntegrationState.READY,
            read_only=True,
        ),
    )


class _IntegrationService:
    def __init__(self, records: tuple[IntegrationRecord, ...]) -> None:
        self.records = records

    async def doctor_async(self, _root: Path):
        return SimpleNamespace(details=SimpleNamespace(integrations=self.records))


class _TimeoutService:
    async def doctor_async(self, _root: Path):
        raise TimeoutError


def _source_config() -> dict[str, object]:
    return {
        "status": "ready",
        "servers": {
            name: {
                "status": "ready",
                "reason_code": "CODEX_SOURCE_CONFIG_READY",
                "source_config": {"status": "configured"},
                "local_http_source_config": {"status": "configured"},
            }
            for name in status.SERVER_NAMES
        },
    }


async def _local_probe(name: str, _root: Path, _revision: str) -> dict[str, object]:
    return {
        "status": "ready",
        "reason_code": "LOCAL_CONTRACT_READY",
        "server_name": name,
        "server_version": "1.0.0",
        "protocol_version": "2025-11-25",
        "evidence_kind": "representative_local_probe",
        "runtime_reachable": True,
        "runtime_ready": True,
    }


async def _remote_probe(name: str) -> dict[str, object]:
    return status._not_configured_remote(name)


def _patch_ready_collectors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status, "git_source_snapshot", lambda _root: ("a" * 40, "clean")
    )
    monkeypatch.setattr(
        status,
        "load_server_versions",
        lambda _root: {name: "1.0.0" for name in status.SERVER_NAMES},
    )
    monkeypatch.setattr(
        status, "first_party_source_registration", lambda _root: _source_config()
    )
    monkeypatch.setattr(status, "_codex_plugin_status", lambda _root: {"status": "ready"})
    monkeypatch.setattr(
        status, "_version_guard", lambda _root, _versions: {"status": "ready"}
    )


def _ready_report(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    _patch_ready_collectors(monkeypatch)
    records = tuple(
        _record(IntegrationName(name)) for name in status.DIRECT_INTEGRATION_NAMES
    )
    return asyncio.run(
        status.collect_status_async(
            REPOSITORY_ROOT,
            local_probe=_local_probe,
            remote_probe=_remote_probe,
            integration_service=_IntegrationService(records),
            now=lambda: "2026-01-01T00:00:00Z",
        )
    )


def test_status_keeps_first_party_contract_and_adds_exactly_six_direct_integrations(
    monkeypatch,
):
    report = _ready_report(monkeypatch)

    assert report["status"] == "ready"
    assert tuple(report["integrations"]) == status.DIRECT_INTEGRATION_NAMES
    assert "direct_routes" not in report
    assert all(item["state"] == "ready" for item in report["integrations"].values())
    assert all(
        item["local_direct"]["status"] == "ready"
        for item in report["servers"].values()
    )
    assert all("owned_local_http" in item for item in report["servers"].values())
    assert all(
        "legacy_stdio_processes" in item for item in report["servers"].values()
    )
    assert report["effective_codex_registration"]["status"] == "not_observable"


def test_status_timeout_is_bounded_and_fail_closed(monkeypatch):
    _patch_ready_collectors(monkeypatch)
    report = asyncio.run(
        status.collect_status_async(
            REPOSITORY_ROOT,
            local_probe=_local_probe,
            remote_probe=_remote_probe,
            integration_service=_TimeoutService(),
        )
    )

    assert report["probe"]["status"] == "unavailable"
    assert all(item["state"] == "unavailable" for item in report["integrations"].values())
    assert report["status"] == "partial"


def test_status_does_not_create_state_directories_and_reports_auth_oserror_as_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_ready_collectors(monkeypatch)

    def unreadable_token(*_args, **_kwargs):
        raise PermissionError("synthetic access denial")

    monkeypatch.setattr(local_http_auth, "read_local_mcp_token", unreadable_token)
    records = tuple(
        _record(IntegrationName(name)) for name in status.DIRECT_INTEGRATION_NAMES
    )
    report = asyncio.run(
        status.collect_status_async(
            tmp_path,
            local_probe=_local_probe,
            remote_probe=_remote_probe,
            integration_service=_IntegrationService(records),
            now=lambda: "2026-01-01T00:00:00Z",
        )
    )

    owned_status = report["servers"]["azurpilot-dev"]["owned_local_http"]
    assert owned_status["status"] == "unknown"
    assert owned_status["credential"] == "unknown"
    assert not (tmp_path / "config" / "state").exists()


def test_owned_local_http_requires_configured_credential_for_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = local_http_supervisor.LOCAL_HTTP_SERVICES[0]
    monkeypatch.setattr(
        local_http_supervisor,
        "LocalHttpSupervisor",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=lambda: {"code": "LOCAL_MCP_SUPERVISOR_READY"}
        ),
    )

    def unavailable_token(*_args, **_kwargs):
        raise local_http_auth.LocalHttpAuthError("LOCAL_MCP_AUTH_UNAVAILABLE")

    monkeypatch.setattr(local_http_auth, "read_local_mcp_token", unavailable_token)

    result = status._owned_local_http_status(tmp_path, service.name)

    assert result["status"] == "partial"
    assert result["credential"] == "unavailable"


def test_owned_local_http_keeps_unknown_credential_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = local_http_supervisor.LOCAL_HTTP_SERVICES[0]
    monkeypatch.setattr(
        local_http_supervisor,
        "LocalHttpSupervisor",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=lambda: {"code": "LOCAL_MCP_SUPERVISOR_READY"}
        ),
    )

    def unknown_token(*_args, **_kwargs):
        raise local_http_auth.LocalHttpAuthUnknownError("LOCAL_MCP_AUTH_UNKNOWN")

    monkeypatch.setattr(local_http_auth, "read_local_mcp_token", unknown_token)

    result = status._owned_local_http_status(tmp_path, service.name)

    assert result["status"] == "unknown"
    assert result["credential"] == "unknown"


def test_local_http_probe_keeps_unknown_credential_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unknown_headers(*_args, **_kwargs):
        raise local_http_auth.LocalHttpAuthUnknownError("LOCAL_MCP_AUTH_UNKNOWN")

    monkeypatch.setattr(status, "local_http_headers", unknown_headers)
    monkeypatch.setattr(
        status,
        "accept_fresh_http",
        lambda **_kwargs: pytest.fail("unknown credentials must block fresh HTTP"),
    )

    result = asyncio.run(
        status._probe_local_http("azurpilot-dev", root=tmp_path, revision="a" * 40)
    )

    assert result["status"] == "unknown"
    assert result["reason_code"] == "MCP_PROJECT_LOCAL_CREDENTIAL_UNKNOWN"


@pytest.mark.parametrize("denied_attribute", ("cmdline", "username", "exe", "cwd"))
def test_legacy_stdio_status_is_unknown_for_access_denied_attributes(
    monkeypatch: pytest.MonkeyPatch, denied_attribute: str
) -> None:
    current_username = status.psutil.Process().username()
    info = {
        "pid": 123,
        "cmdline": ["python", "-m", status.SERVER_MODULES["azurpilot-dev"][0]],
        "username": current_username,
        "exe": "python.exe",
        "cwd": "C:/AzurPilot",
    }

    def process_iter(_attrs, *, ad_value):
        info[denied_attribute] = ad_value
        return (SimpleNamespace(info=info),)

    monkeypatch.setattr(status.psutil, "process_iter", process_iter)

    result = status._legacy_stdio_process_status("azurpilot-dev")

    assert result["status"] == "unknown"
    assert result["reason_code"] == "LEGACY_STDIO_PROCESS_UNOBSERVABLE"


def test_legacy_stdio_status_reports_absent_after_complete_unmatched_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_username = status.psutil.Process().username()

    def process_iter(_attrs, *, ad_value):
        return (
            SimpleNamespace(
                info={
                    "pid": 123,
                    "cmdline": ["python", "-c", "pass"],
                    "username": current_username,
                    "exe": "python.exe",
                    "cwd": "C:/AzurPilot",
                }
            ),
        )

    monkeypatch.setattr(status.psutil, "process_iter", process_iter)

    result = status._legacy_stdio_process_status("azurpilot-dev")

    assert result["status"] == "absent"
    assert result["reason_code"] == "LEGACY_STDIO_PROCESS_ABSENT"


@pytest.mark.parametrize("denied_attribute", ("exe", "cwd"))
def test_legacy_stdio_status_ignores_inaccessible_metadata_for_unmatched_process(
    monkeypatch: pytest.MonkeyPatch, denied_attribute: str
) -> None:
    current_username = status.psutil.Process().username()
    info = {
        "pid": 123,
        "cmdline": ["python", "-c", "pass"],
        "username": current_username,
        "exe": "python.exe",
        "cwd": "C:/Windows/System32",
    }

    def process_iter(attrs, *, ad_value):
        assert "username" in attrs
        info[denied_attribute] = ad_value
        return (SimpleNamespace(info=info),)

    monkeypatch.setattr(status.psutil, "process_iter", process_iter)

    result = status._legacy_stdio_process_status("azurpilot-dev")

    assert result["status"] == "absent"


def test_legacy_stdio_status_ignores_matching_process_from_another_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = {
        "pid": 123,
        "cmdline": ["python", "-m", status.SERVER_MODULES["azurpilot-dev"][0]],
        "username": "another-user",
    }

    monkeypatch.setattr(
        status.psutil,
        "process_iter",
        lambda _attrs, *, ad_value: (SimpleNamespace(info=info),),
    )

    result = status._legacy_stdio_process_status("azurpilot-dev")

    assert result["status"] == "absent"


def test_legacy_stdio_status_skips_foreign_process_with_unreadable_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def process_iter(_attrs, *, ad_value):
        return (
            SimpleNamespace(
                info={
                    "pid": 123,
                    "cmdline": ad_value,
                    "username": "another-user",
                    "exe": ad_value,
                    "cwd": ad_value,
                    "name": ad_value,
                }
            ),
        )

    monkeypatch.setattr(status.psutil, "process_iter", process_iter)

    result = status._legacy_stdio_process_status("azurpilot-dev")

    assert result["status"] == "absent"
    assert result["reason_code"] == "LEGACY_STDIO_PROCESS_ABSENT"


def test_legacy_stdio_status_skips_non_python_process_with_unreadable_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_username = status.psutil.Process().username()

    def process_iter(_attrs, *, ad_value):
        return (
            SimpleNamespace(
                info={
                    "pid": 123,
                    "cmdline": ad_value,
                    "username": current_username,
                    "exe": "C:/Program Files/OpenAI/ChatGPT.exe",
                    "cwd": ad_value,
                    "name": "ChatGPT.exe",
                }
            ),
        )

    monkeypatch.setattr(status.psutil, "process_iter", process_iter)

    result = status._legacy_stdio_process_status("azurpilot-dev")

    assert result["status"] == "absent"
    assert result["reason_code"] == "LEGACY_STDIO_PROCESS_ABSENT"


def test_local_http_probe_does_not_claim_authentication_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status, "local_http_headers", lambda *_args: {"Authorization": "Bearer test"})
    async def failed_probe(**_kwargs):
        return _failed_fresh_result()

    monkeypatch.setattr(status, "accept_fresh_http", failed_probe)

    payload = asyncio.run(
        status._probe_local_http("azurpilot-dev", root=REPOSITORY_ROOT, revision="a" * 40)
    )

    assert payload["status"] == "unavailable"
    assert payload["authenticated"] is False


def _failed_fresh_result() -> FreshMcpClientResult:
    return FreshMcpClientResult(
        state=IntegrationState.UNAVAILABLE,
        reason_code="MCP_FRESH_CLIENT_HTTP_FAILED",
    )


def test_json_report_and_metric_labels_are_bounded(monkeypatch):
    report = _ready_report(monkeypatch)
    encoded = json.dumps(report, ensure_ascii=False)
    samples = status.status_metric_samples(report)

    assert json.loads(encoded)["integrations"]["grafana"]["state"] == "ready"
    external = [
        sample
        for sample in samples
        if sample.attributes["surface"] == "external_direct"
    ]
    assert external
    codex_source = [
        sample
        for sample in samples
        if sample.attributes["surface"] == "codex_source"
    ]
    assert codex_source
    configured = [
        sample
        for sample in codex_source
        if sample.name == "azurpilot_mcp_surface_configured"
    ]
    assert configured
    assert all(sample.value == 1.0 for sample in configured)
    codex_effective = [
        sample
        for sample in samples
        if sample.attributes["surface"] == "codex_effective"
    ]
    assert codex_effective
    assert all(sample.attributes["required_runtime"] == "0" for sample in codex_effective)
    assert all(
        set(sample.attributes)
        <= {"server", "surface", "version", "protocol", "required_runtime"}
        for sample in samples
    )
    assert "password" not in encoded.casefold()
    assert "authorization" not in encoded.casefold()
    names = {sample.name for sample in samples}
    assert "azurpilot_mcp_version_drift" in names
    assert "azurpilot_mcp_last_successful_probe_timestamp_seconds" in names
    assert "azurpilot_mcp_observed_version_info" in names


def test_human_report_mentions_direct_integrations_without_legacy_route(
    monkeypatch, capsys
):
    report = _ready_report(monkeypatch)
    status._print_human(report, None)
    output = capsys.readouterr().out.casefold()

    assert "внешние интеграции" in output
    assert "coderabbit" in output
    assert "docker-hub" in output
    assert "external_direct" not in output
    assert "gateway" not in output


def test_human_report_uses_russian_unknown_and_notes_labels(monkeypatch, capsys):
    report = _ready_report(monkeypatch)
    report["status"] = None
    report["servers"]["azurpilot-dev"]["local_direct"]["status"] = "unavailable"

    status._print_human(report, None)
    output = capsys.readouterr().out

    assert "ИТОГ     НЕИЗВЕСТНО" in output
    assert "НЕДОСТУПНО" in output
    assert "Примечания" in output
    assert "Notes" not in output


def test_human_table_expands_status_column_and_keeps_later_columns_aligned(capsys):
    status._print_human_table(
        ("СЕРВЕР", "СОСТОЯНИЕ", "ПРИЧИНА"),
        (
            ("azurpilot-dev", "НЕИЗВЕСТНО", "CODE_A"),
            ("azurpilot-game", "НЕДОСТУПНО", "CODE_B"),
        ),
    )
    lines = capsys.readouterr().out.splitlines()

    reason_column = lines[0].index("ПРИЧИНА")
    assert lines[2].index("CODE_A") == reason_column
    assert lines[3].index("CODE_B") == reason_column


def test_missing_yaml_dependency_is_reported_without_name_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "yaml", None)
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: test\ndescription: test\n---\n", encoding="utf-8")

    result = status._codex_skill_status(skill, "test")

    assert result == {
        "status": "unavailable",
        "reason_code": "CODEX_PLUGIN_SKILL_UNAVAILABLE",
    }


def test_strict_allows_codex_session_to_remain_not_observable(monkeypatch):
    report = _ready_report(monkeypatch)
    assert not status._strict_failure(
        report,
        status.MetricEmission(
            emitted=True, reason_code="MCP_METRICS_EXPORTED", sample_count=1
        ),
    )


def test_strict_rejects_invalid_codex_session_state(monkeypatch):
    report = _ready_report(monkeypatch)
    report["effective_codex_registration"]["status"] = "failed"
    assert status._strict_failure(
        report,
        status.MetricEmission(
            emitted=True, reason_code="MCP_METRICS_EXPORTED", sample_count=1
        ),
    )


def test_repository_boundary_has_no_toolkit_registration_or_retired_profiles():
    config = (
        REPOSITORY_ROOT / ".codex" / "config.toml"
    ).read_text(encoding="utf-8").casefold()
    toolkit_registration = "mcp" + "_" + "docker"
    gateway_phrase = "docker" + " mcp " + "gateway"

    assert toolkit_registration not in config
    assert gateway_phrase not in config
    assert "context7_direct" in config
    assert "grafana_direct" in config
    assert "dockerhub_direct" in config
    assert gate.RETIRED_PROFILE_PATHS
    assert all(
        not (REPOSITORY_ROOT / relative).exists()
        for relative in gate.RETIRED_PROFILE_PATHS
    )


def test_first_party_source_registration_remains_readable():
    result = status.first_party_source_registration(REPOSITORY_ROOT)
    assert result["status"] == "ready"
    assert set(result["servers"]) == set(status.SERVER_NAMES)


def test_child_environment_does_not_inherit_unknown_secret(monkeypatch):
    monkeypatch.setenv("AZURPILOT_TEST_SECRET", "not-for-child")
    environment = status.child_environment("a" * 40)

    assert environment["AZURPILOT_SOURCE_REVISION"] == "a" * 40
    assert "AZURPILOT_TEST_SECRET" not in environment


def test_metric_emission_is_fail_open_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURPILOT_OBSERVABILITY_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    emission = status.emit_metrics(_ready_report(monkeypatch))

    assert emission.emitted is False
    assert emission.reason_code == "MCP_METRICS_ENDPOINT_UNCONFIGURED"


def test_result_code_mapping_preserves_typed_provider_failure():
    from azurpilot.integrations.service import IntegrationService

    records = (_record(IntegrationName.GRAFANA, IntegrationState.RATE_LIMITED),)
    assert (
        IntegrationService._result_code(records)
        is ResultCode.TOOLING_PROVIDER_UNAVAILABLE
    )
