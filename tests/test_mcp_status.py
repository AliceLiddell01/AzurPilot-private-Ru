from __future__ import annotations

import asyncio
import json
import re
import time
from copy import deepcopy
from pathlib import Path

import pytest

import dev_tools.mcp_status as status
from module.mcp_shared.versioning import load_server_versions


@pytest.fixture(autouse=True)
def _stub_semgrep_probe(monkeypatch) -> None:
    async def probe(root: Path) -> dict[str, object]:
        return {
            "status": "ready",
            "reason_code": "SEMGREP_SCAN_READY",
            "server_name": "Semgrep",
            "server_version": "1.29.0",
            "scan_tool": "semgrep_scan",
            "runtime_reachable": True,
            "runtime_ready": True,
        }

    monkeypatch.setattr(status, "_probe_semgrep_local_mcp", probe)


def _versions() -> dict[str, str]:
    return load_server_versions(Path(__file__).resolve().parents[1])


def _local_result(name: str, version: str, revision: str) -> dict[str, object]:
    return {
        "status": "ready",
        "reason_code": "LOCAL_CONTRACT_READY",
        "server_name": name,
        "server_version": version,
        "protocol_version": "2025-11-25",
        "contract_schema_version": 1,
        "source_revision": revision,
        "tool_count": 1,
    }


def _docker_ready() -> dict[str, object]:
    profile_servers = {
        name: {
            "status": "ready",
            "reason_code": "DOCKER_SERVER_PROFILE_CONFIGURED",
            "configured": True,
            "read_only": True,
            "read_only_policy": {"status": "ready"},
            "profile_tool_names": [],
            "snapshot_tool_names": [],
        }
        for name in status.THIRD_PARTY_SERVERS
    }
    runtime_servers = {
        name: {
            "status": "ready",
            "reason_code": "DOCKER_GATEWAY_READ_ONLY_CALL_READY",
            "runtime_reachable": True,
            "runtime_ready": True,
            "tools_observable": True,
        }
        for name in status.THIRD_PARTY_SERVERS
    }
    third_party = {
        name: {
            **profile_servers[name],
            "profile_config": profile_servers[name],
            "gateway_runtime": runtime_servers[name],
            "tools_observable": True,
            "runtime_reachable": True,
            "runtime_ready": True,
            "status": "ready",
        }
        for name in status.THIRD_PARTY_SERVERS
    }
    return {
        "status": "ready",
        "reason_code": "DOCKER_PROFILE_READY",
        "profile_id": status.CANONICAL_DOCKER_PROFILE_ID,
        "server_names": list(status.THIRD_PARTY_SERVERS),
        "profile_config": {
            "status": "ready",
            "reason_code": "DOCKER_PROFILE_READY",
            "third_party": profile_servers,
        },
        "gateway_runtime": {
            "status": "ready",
            "reason_code": "DOCKER_GATEWAY_RUNTIME_READY",
            "runtime_reachable": True,
            "runtime_ready": True,
            "servers": runtime_servers,
        },
        "client_connection": {"status": "configured"},
        "third_party": third_party,
        "secret_engine": {"secret_store": {"status": "ready"}},
    }


def _remote_ready(name: str) -> dict[str, object]:
    return {
        "remote_backend": {
            "status": "ready",
            "server_name": name,
            "server_version": _versions()[name],
            "runtime_reachable": True,
            "runtime_ready": True,
            "protocol_version": "2025-11-25",
        },
        "public_edge": {"status": "ready", "edge_reachable": True},
    }


def test_status_json_model_records_exact_local_identity(monkeypatch) -> None:
    revision = "a" * 40
    monkeypatch.setattr(
        status, "_git_source_snapshot", lambda root: (revision, "clean")
    )

    async def local(name: str, root: Path, current_revision: str) -> dict[str, object]:
        version = _versions()[name]
        return _local_result(name, version, current_revision)

    async def remote(name: str) -> dict[str, object]:
        return _remote_ready(name)

    report = asyncio.run(
        status.collect_status_async(
            Path(__file__).resolve().parents[1],
            local_probe=local,
            remote_probe=remote,
            docker_probe=_docker_ready,
            now=lambda: "2026-01-01T00:00:00Z",
        )
    )

    assert report["schema_version"] == 1
    assert report["status"] == "ready"
    assert report["source"] == {"revision": revision, "working_tree": "clean"}
    assert report["version_guard"] == {
        "status": "ready",
        "reason_code": "MCP_VERSION_GUARD_READY",
    }
    assert (
        report["servers"]["azurpilot-dev"]["local_direct"]["version_status"]
        == "compatible"
    )
    assert report["chatgpt"]["status"] == "not_observable"


def test_human_status_uses_compact_tables_and_sections(capsys) -> None:
    revision = "a" * 40
    report = {
        "status": "partial",
        "reason_code": "MCP_STATUS_PARTIAL",
        "source": {"revision": revision, "working_tree": "clean"},
        "version_guard": {
            "status": "ready",
            "reason_code": "MCP_VERSION_GUARD_READY",
        },
        "servers": {
            "azurpilot-dev": {
                "expected_version": _versions()["azurpilot-dev"],
                "local_direct": _local_result(
                    "azurpilot-dev", _versions()["azurpilot-dev"], revision
                ),
                "codex": {
                    "status": "configured",
                    "reason_code": "CODEX_SERVER_CONFIGURED",
                },
                "remote_backend": {
                    "status": "not_configured",
                    "reason_code": "REMOTE_PUBLIC_URL_NOT_CONFIGURED",
                },
                "public_edge": {
                    "status": "not_configured",
                    "reason_code": "REMOTE_PUBLIC_URL_NOT_CONFIGURED",
                },
            },
            "azurpilot-game": {
                "expected_version": _versions()["azurpilot-game"],
                "local_direct": _local_result(
                    "azurpilot-game", _versions()["azurpilot-game"], revision
                ),
                "codex": {
                    "status": "not_configured",
                    "reason_code": "CODEX_GAME_SURFACE_EXTERNAL",
                },
                "remote_backend": {
                    "status": "unavailable",
                    "reason_code": "REMOTE_METADATA_UNAVAILABLE",
                },
                "public_edge": {
                    "status": "unavailable",
                    "reason_code": "REMOTE_METADATA_UNAVAILABLE",
                },
            },
        },
        "docker_mcp": _docker_ready(),
        "chatgpt": {
            "status": "not_observable",
            "reason_code": "CHATGPT_ACTION_SNAPSHOT_NOT_OBSERVABLE",
        },
    }

    status._print_human(report, None)
    output = capsys.readouterr().out

    assert "AzurPilot MCP Status" in output
    assert "SERVER" in output and "REMOTE BACKEND" in output
    assert "azurpilot-dev" in output
    assert f"{_versions()['azurpilot-dev']} OK" in output
    assert "EXTERNAL" in output
    assert "Docker MCP Gateway" in output
    assert "Status: OK" in output
    assert "context7" in output
    assert "ChatGPT action cache" in output
    assert "MCP_STATUS_PARTIAL" in output
    assert "local_direct" not in output
    assert "reason_code" not in output


def test_status_marks_source_drift_and_strict_fails(monkeypatch) -> None:
    expected_revision = "a" * 40
    monkeypatch.setattr(
        status, "_git_source_snapshot", lambda root: (expected_revision, "clean")
    )

    async def local(name: str, root: Path, current_revision: str) -> dict[str, object]:
        version = _versions()[name]
        return _local_result(name, version, "b" * 40)

    async def remote(name: str) -> dict[str, object]:
        return _remote_ready(name)

    report = asyncio.run(
        status.collect_status_async(
            Path(__file__).resolve().parents[1],
            local_probe=local,
            remote_probe=remote,
            docker_probe=_docker_ready,
        )
    )

    assert report["status"] == "drift"
    assert status._strict_failure(report, None)


def test_modified_working_tree_is_partial_and_preserves_source_status() -> None:
    revision = "a" * 40
    surface = status._surface_status(
        _local_result("azurpilot-dev", _versions()["azurpilot-dev"], revision),
        expected_version=_versions()["azurpilot-dev"],
        expected_revision=revision,
        working_tree="modified",
    )

    assert surface["status"] == "partial"
    assert surface["source_status"] == "modified"
    assert surface["reason_code"] == "LOCAL_CONTRACT_READY"
    assert (
        status._human_surface_cell(
            surface, expected_version=_versions()["azurpilot-dev"]
        )
        == f"{_versions()['azurpilot-dev']} MODIFIED"
    )


def test_docker_probe_timeout_is_reported_without_waiting_for_the_probe(
    monkeypatch,
) -> None:
    monkeypatch.setattr(status, "DOCKER_PROBE_TIMEOUT_SECONDS", 0.001)

    async def local(name: str, root: Path, revision: str) -> dict[str, object]:
        version = _versions()[name]
        return _local_result(name, version, revision)

    async def remote(name: str) -> dict[str, object]:
        return _remote_ready(name)

    def docker_probe() -> dict[str, object]:
        time.sleep(0.05)
        return _docker_ready()

    started = time.monotonic()
    report = asyncio.run(
        status.collect_status_async(
            Path(__file__).resolve().parents[1],
            local_probe=local,
            remote_probe=remote,
            docker_probe=docker_probe,
        )
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert report["docker_mcp"]["status"] == "unavailable"
    assert report["docker_mcp"]["reason_code"] == "DOCKER_PROBE_TIMEOUT"


def test_semgrep_probe_timeout_is_reported_with_bounded_wait(monkeypatch) -> None:
    monkeypatch.setattr(status, "SEMGREP_PROBE_TOTAL_TIMEOUT_SECONDS", 0.01)

    async def local(name: str, root: Path, revision: str) -> dict[str, object]:
        version = _versions()[name]
        return _local_result(name, version, revision)

    async def remote(name: str) -> dict[str, object]:
        return _remote_ready(name)

    async def semgrep_probe(root: Path) -> dict[str, object]:
        await asyncio.sleep(0.1)
        return {"status": "ready"}

    started = time.monotonic()
    report = asyncio.run(
        status.collect_status_async(
            Path(__file__).resolve().parents[1],
            local_probe=local,
            remote_probe=remote,
            docker_probe=_docker_ready,
            semgrep_probe=semgrep_probe,
        )
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert report["semgrep_mcp"]["status"] == "unavailable"
    assert report["semgrep_mcp"]["reason_code"] == "SEMGREP_LOCAL_PROBE_TIMEOUT"


def test_semgrep_result_without_json_payload_is_not_observable() -> None:
    result = type("_Result", (), {"is_error": False, "content": []})()

    summary = status._semgrep_result_summary(result)

    assert summary == {
        "status": "not_observable",
        "reason_code": "SEMGREP_SCAN_RESULT_NOT_OBSERVABLE",
        "finding_count": None,
    }


def test_metric_samples_have_bounded_static_labels() -> None:
    report = {
        "servers": {
            "azurpilot-dev": {
                "expected_version": "3.0.0",
                "local_direct": {
                    "status": "ready",
                    "protocol_version": "2025-11-25",
                    "version_status": "compatible",
                },
                "codex": {
                    "status": "configured",
                    "protocol_version": "2025-11-25",
                },
            }
        },
        "docker_mcp": {"third_party": {}, "version": "v0.43.3", "status": "ready"},
    }
    samples = status.status_metric_samples(report)

    assert samples
    assert all(
        set(sample.attributes) <= {"server", "surface", "version", "protocol"}
        for sample in samples
    )
    assert all("source_revision" not in sample.attributes for sample in samples)
    assert all(
        "http" not in value
        for sample in samples
        for value in sample.attributes.values()
    )
    assert any(
        sample.name == "azurpilot_mcp_observed_version_info"
        and sample.attributes["server"] == "docker-gateway"
        and sample.attributes["version"] == "0.43.3"
        for sample in samples
    )
    assert any(
        sample.name == "azurpilot_mcp_surface_configured"
        and sample.attributes["surface"] == "codex"
        and sample.value == 1.0
        for sample in samples
    )
    assert any(
        sample.name == "azurpilot_mcp_expected_version_info"
        and sample.attributes["surface"] == "codex"
        and sample.value == 1.0
        for sample in samples
    )


def test_metric_samples_keep_configured_distinct_from_reachable_and_observed() -> None:
    report = {
        "servers": {
            "azurpilot-dev": {
                "expected_version": "3.0.0",
                "codex": {"status": "configured"},
            }
        },
        "probe": {"last_successful_probe_timestamp_seconds": None},
    }

    samples = status.status_metric_samples(report)

    assert next(
        sample
        for sample in samples
        if sample.name == "azurpilot_mcp_surface_configured"
        and sample.attributes["surface"] == "codex"
    ).value == 1.0
    assert next(
        sample
        for sample in samples
        if sample.name == "azurpilot_mcp_surface_reachable"
        and sample.attributes["surface"] == "codex"
    ).value == 0.0
    assert next(
        sample
        for sample in samples
        if sample.name == "azurpilot_mcp_surface_runtime_ready"
        and sample.attributes["surface"] == "codex"
    ).value == 0.0
    assert not any(
        sample.name == "azurpilot_mcp_observed_version_info"
        and sample.attributes["surface"] == "codex"
        for sample in samples
    )
    assert not any(
        sample.name == "azurpilot_mcp_last_successful_probe_timestamp_seconds"
        for sample in samples
    )


def test_emit_metrics_delegates_to_canonical_observability_runtime(monkeypatch) -> None:
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://127.0.0.1:4318/v1/metrics"
    )
    captured: dict[str, object] = {}

    def emit(samples, **kwargs):
        captured["samples"] = tuple(samples)
        captured.update(kwargs)
        return True

    monkeypatch.setattr(
        "module.observability.metrics.emit_metric_samples_once", emit
    )

    result = status.emit_metrics({"servers": {}, "docker_mcp": {"third_party": {}}})

    assert result == status.MetricEmission(True, "MCP_METRICS_EXPORTED", 0)
    assert captured["endpoint"] == "http://127.0.0.1:4318/v1/metrics"
    assert captured["timeout_millis"] == 5000
    assert isinstance(captured["repository_root"], Path)


def test_emit_metrics_returns_bounded_error_for_invalid_samples(monkeypatch) -> None:
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://127.0.0.1:4318/v1/metrics"
    )

    def invalid_samples(_report: object) -> tuple[object, ...]:
        raise status.StatusError("MCP_METRIC_ATTRIBUTES_INVALID")

    monkeypatch.setattr(status, "status_metric_samples", invalid_samples)

    assert status.emit_metrics({}) == status.MetricEmission(
        False, "MCP_METRIC_ATTRIBUTES_INVALID", 0
    )


def test_metrics_are_fail_open_when_otlp_endpoint_is_not_configured(
    monkeypatch,
) -> None:
    for name in (
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "AZURPILOT_OBSERVABILITY_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    result = status.emit_metrics({"servers": {}, "docker_mcp": {"third_party": {}}})

    assert result.emitted is False
    assert result.reason_code == "MCP_METRICS_ENDPOINT_UNCONFIGURED"


def test_strict_rejects_unobservable_remote_metadata_and_optional_catalogs() -> None:
    report = {
        "status": "partial",
        "source": {"working_tree": "clean"},
        "version_guard": {"status": "ready"},
        "servers": {
            "azurpilot-dev": {
                "local_direct": {"status": "ready"},
                "remote_backend": {"status": "not_observable"},
                "public_edge": {"status": "ready"},
            }
        },
        "docker_mcp": {
            "status": "partial",
            "secret_engine": {
                "secret_store": {"status": "ready"},
            },
            "third_party": {
                "context7": {"status": "not_observable"},
                "docker-docs": {"status": "not_observable"},
                "dockerhub": {"status": "ready"},
                "grafana": {"status": "ready"},
                "semgrep": {"status": "ready"},
            },
        },
    }

    assert status._strict_failure(report, None)


def test_secret_engine_status_separates_keychain_from_rpc(monkeypatch) -> None:
    def run_process(arguments, **kwargs):
        if tuple(arguments[-3:]) == ("pass", "plugins", "ls"):
            return status.subprocess.CompletedProcess(arguments, 1, "", "socket error")
        return status.subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(status, "_run_process", run_process)

    result = status._docker_secret_engine_status("docker")

    assert result == {
        "status": "partial",
        "reason_code": "DOCKER_SECRET_ENGINE_RPC_UNAVAILABLE",
        "cli_status": "ready",
        "keychain_status": "ready",
        "rpc_status": "unavailable",
        "secret_store": {
            "status": "ready",
            "reason_code": "DOCKER_SECRET_STORE_READY",
        },
        "container_runtime_secret_injection": {
            "status": "not_observable",
            "reason_code": "CONTAINER_RUNTIME_SECRET_INJECTION_NOT_PROBED",
        },
        "gateway_secret_injection": {
            "status": "not_observable",
            "reason_code": "GATEWAY_SECRET_INJECTION_NOT_PROBED",
        },
        "host_pass_resolution": {
            "status": "degraded",
            "reason_code": "DOCKER_SECRET_ENGINE_RPC_UNAVAILABLE",
            "scope": "current_process",
        },
    }


def test_exported_development_profile_is_exact_and_read_only() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / ".docker"
        / "azurpilot-development-profile.json"
    )
    profile = json.loads(path.read_text(encoding="utf-8"))

    status.validate_development_profile(profile)
    assert {
        server["snapshot"]["server"]["name"] for server in profile["servers"]
    } == set(status.THIRD_PARTY_SERVERS)
    dockerhub = next(
        server
        for server in profile["servers"]
        if server["snapshot"]["server"]["name"] == "dockerhub"
    )
    assert set(dockerhub["tools"]) == set(status.DOCKERHUB_READ_ONLY_TOOLS)
    assert "createRepository" not in dockerhub["tools"]
    assert "updateRepositoryInfo" not in dockerhub["tools"]
    grafana = next(
        server
        for server in profile["servers"]
        if server["snapshot"]["server"]["name"] == "grafana"
    )
    assert grafana["config"]["url"] == status.CANONICAL_GRAFANA_PROFILE_URL
    assert "@sha256:" in grafana["image"]

    drifted = deepcopy(profile)
    drifted_dockerhub = next(
        server
        for server in drifted["servers"]
        if server["snapshot"]["server"]["name"] == "dockerhub"
    )
    drifted_dockerhub["tools"].append("updateRepositoryInfo")
    with pytest.raises(status.StatusError, match="DOCKER_PROFILE_ALLOWLIST_INVALID"):
        status.validate_development_profile(drifted)

    duplicated = deepcopy(profile)
    duplicated["servers"].append(deepcopy(duplicated["servers"][0]))
    with pytest.raises(status.StatusError, match="DOCKER_PROFILE_SERVER_COUNT_INVALID"):
        status.validate_development_profile(duplicated)

    unknown = deepcopy(profile)
    unknown["servers"][0]["snapshot"]["server"]["name"] = "unexpected"
    unknown["servers"][0]["name"] = "unexpected"
    with pytest.raises(status.StatusError, match="DOCKER_PROFILE_SERVER_SET_INVALID"):
        status.validate_development_profile(unknown)

    remote_drift = deepcopy(profile)
    context7 = next(
        server
        for server in remote_drift["servers"]
        if server["snapshot"]["server"]["name"] == "context7"
    )
    context7["snapshot"]["server"]["remote"]["url"] = "https://example.invalid/mcp"
    with pytest.raises(status.StatusError, match="DOCKER_PROFILE_REMOTE_ENDPOINT_INVALID"):
        status.validate_development_profile(remote_drift)


def test_exported_profile_contains_secret_references_but_no_secret_values() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / ".docker"
        / "azurpilot-development-profile.json"
    )
    profile = json.loads(path.read_text(encoding="utf-8"))
    sensitive_keys = {
        re.sub(r"[^a-z0-9]", "", key.casefold())
        for key in (
            "access_token",
            "api_key",
            "password",
            "pat_token",
            "secret_value",
            "token",
            "client_secret",
            "authorization",
            "bearer_token",
        )
    }
    secret_value_patterns = (
        re.compile(r"\b(?:sk|rk|xox[baprs])-[A-Za-z0-9_-]{12,}\b"),
        re.compile(r"\b(?:ghp|github_pat|pat)_[A-Za-z0-9_]{12,}\b"),
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    )

    def walk(value: object, key: str = "") -> list[tuple[str, object]]:
        if isinstance(value, dict):
            return [
                nested
                for key, item in value.items()
                for nested in walk(item, str(key))
            ]
        if isinstance(value, list):
            return [nested for item in value for nested in walk(item, key)]
        return [(key, value)]

    def is_secret_reference(value: object) -> bool:
        return isinstance(value, str) and value.startswith("se://") and len(value) > 5

    def is_literal_secret(value: object) -> bool:
        return isinstance(value, str) and not is_secret_reference(value) and any(
            pattern.search(value) for pattern in secret_value_patterns
        )

    for key, value in walk(profile):
        normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
        if normalized_key in sensitive_keys:
            assert is_secret_reference(value), key
        assert not is_literal_secret(value), key

    synthetic = {
        "token": "se://docker/token",
        "nested": [
            {"clientSecret": "sk-" + ("x" * 20)},
            {"authorization": "ghp_" + ("x" * 20)},
            {"bearerToken": "pat_" + ("x" * 20)},
            {"jwt": "eyJ" + ("a" * 12) + "." + ("b" * 12) + "." + ("c" * 12)},
        ],
    }
    synthetic_entries = walk(synthetic)
    assert is_secret_reference(synthetic_entries[0][1])
    assert all(
        is_literal_secret(value)
        for key, value in synthetic_entries
        if key != "token"
    )


def test_docker_status_rejects_non_list_profile_payload(monkeypatch) -> None:
    monkeypatch.setattr(status, "_docker_executable", lambda: "docker")
    monkeypatch.setattr(
        status,
        "_run_process",
        lambda arguments, **kwargs: status.subprocess.CompletedProcess(
            arguments, 0, "0.43.3\n", ""
        ),
    )
    monkeypatch.setattr(
        status,
        "_docker_secret_engine_status",
        lambda executable: {"status": "ready"},
    )
    monkeypatch.setattr(
        status,
        "_docker_json",
        lambda arguments: ({"profiles": []}, "OK"),
    )

    result = status._docker_status()

    assert result == {
        "status": "unavailable",
        "reason_code": "DOCKER_PROFILE_LIST_INVALID",
        "version": "0.43.3",
        "secret_engine": {"status": "ready"},
    }


def test_timeout_injected_local_probe_is_reported_without_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        status, "_git_source_snapshot", lambda root: ("a" * 40, "clean")
    )

    async def local(name: str, root: Path, revision: str) -> dict[str, object]:
        await asyncio.sleep(0.01)
        raise TimeoutError("do not publish this message")

    async def remote(name: str) -> dict[str, object]:
        return _remote_ready(name)

    report = asyncio.run(
        status.collect_status_async(
            Path(__file__).resolve().parents[1],
            local_probe=local,
            remote_probe=remote,
            docker_probe=_docker_ready,
        )
    )
    assert (
        report["servers"]["azurpilot-dev"]["local_direct"]["reason_code"]
        == "LOCAL_PROBE_TIMEOUT"
    )
    assert "do not publish" not in str(report)
