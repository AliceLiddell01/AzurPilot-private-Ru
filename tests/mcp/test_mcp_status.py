from __future__ import annotations

import asyncio
import builtins
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import dev_tools.mcp_status as status
from azurpilot.integrations.contracts import (
    IntegrationEvidence,
    IntegrationName,
    IntegrationRecord,
    IntegrationState,
)
from azurpilot.tooling.contracts import ResultCode
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
        status, "_git_source_snapshot", lambda _root: ("a" * 40, "clean")
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


def test_missing_yaml_dependency_is_reported_without_name_error(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def missing_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("yaml unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_yaml)
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: test\ndescription: test\n---\n", encoding="utf-8")

    result = status._codex_skill_status(skill, "test")

    assert result == {
        "status": "unavailable",
        "reason_code": "CODEX_PLUGIN_SKILL_UNAVAILABLE",
    }


def test_strict_requires_observable_codex_session(monkeypatch):
    report = _ready_report(monkeypatch)
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
    assert not (
        REPOSITORY_ROOT / ".docker" / ("azurpilot-" + "development-profile.json")
    ).exists()
    assert not (
        REPOSITORY_ROOT / ".docker" / ("azurpilot-" + "observability-profile.json")
    ).exists()


def test_first_party_source_registration_remains_readable():
    result = status.first_party_source_registration(REPOSITORY_ROOT)
    assert result["status"] == "ready"
    assert set(result["servers"]) == set(status.SERVER_NAMES)


def test_child_environment_does_not_inherit_unknown_secret(monkeypatch):
    monkeypatch.setenv("AZURPILOT_TEST_SECRET", "not-for-child")
    environment = status._child_environment("a" * 40)

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
