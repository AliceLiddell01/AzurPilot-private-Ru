"""Контракт изолированного отказа, recovery и ограниченной конфигурации collector."""

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from dev_tools import observability_reliability as target

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def docker_state(monkeypatch):
    state = {
        name: {
            "id": name,
            "image": "digest",
            "status": "running",
            "health": "healthy",
            "volumes": {"/data": name},
            "restart_count": 0,
        }
        for name in (*target.SERVICES, "postgres", "caddy", "pgadmin")
    }
    calls = []
    monkeypatch.setattr(target, "inventory", lambda: copy.deepcopy(state))
    monkeypatch.setattr(
        target, "ready", lambda *args: dict.fromkeys(target.SERVICES, True)
    )

    def docker(*args, **_kwargs):
        calls.append(args)
        if args[0] == "stop":
            state[args[-1]]["status"] = "exited"
        elif args[0] == "start":
            state[args[-1]]["status"] = "running"
        else:
            pytest.fail("Недопустимая Docker mutation")
        return ""

    monkeypatch.setattr(target, "docker", docker)
    return state, calls, docker


@pytest.mark.parametrize(
    "services",
    [
        ("postgres",),
        ("caddy",),
        ("pgadmin",),
        (),
        ("alloy", "alloy"),
        ("loki", "postgres"),
    ],
)
def test_denied_services_never_reach_docker(docker_state, tmp_path, services):
    with pytest.raises(target.ReliabilityError, match="SERVICE_DENIED"):
        with target.outage(services, tmp_path / "recovery.json"):
            pytest.fail("Недопустимый service был разрешён")
    assert docker_state[1] == []


def test_failed_probe_restores_only_attempted_services(docker_state, tmp_path):
    state, calls, _ = docker_state
    with pytest.raises(ValueError, match="исходная ошибка"):
        with target.outage(("tempo", "loki"), tmp_path / "recovery.json"):
            assert state["tempo"]["status"] == "exited"
            raise ValueError("исходная ошибка")
    assert calls == [
        ("stop", "--time", "10", "tempo"),
        ("stop", "--time", "10", "loki"),
        ("start", "loki"),
        ("start", "tempo"),
    ]
    assert all(item["status"] == "running" for item in state.values())


def test_stop_timeout_after_side_effect_still_recovers(
    docker_state, monkeypatch, tmp_path
):
    state, calls, docker = docker_state

    def failing(*args):
        docker(*args)
        if args[0] == "stop":
            raise TimeoutError("таймаут после stop")

    monkeypatch.setattr(target, "docker", failing)
    with pytest.raises(TimeoutError):
        with target.outage(("tempo", "loki"), tmp_path / "recovery.json"):
            pytest.fail("После timeout нельзя продолжать probe")
    assert calls[-1] == ("start", "tempo")
    assert state["tempo"]["status"] == "running"
    assert not any("loki" in call for call in calls)


def test_recovery_error_does_not_mask_original(docker_state, monkeypatch, tmp_path):
    _, calls, docker = docker_state

    def failing(*args):
        if args[0] == "start" and args[-1] == "loki":
            raise OSError("ошибка recovery")
        return docker(*args)

    monkeypatch.setattr(target, "docker", failing)
    journal = tmp_path / "recovery.json"
    with pytest.raises(ValueError, match="исходная ошибка"):
        with target.outage(("tempo", "loki"), journal):
            raise ValueError("исходная ошибка")
    assert calls[-1] == ("start", "tempo")
    assert json.loads(journal.read_text())["recovery_errors"] == [
        {"service": "loki", "error": "OSError"}
    ]


def test_existing_journal_prevents_mutation(docker_state, tmp_path):
    journal = tmp_path / "recovery.json"
    journal.write_text("{}")
    with pytest.raises(FileExistsError):
        with target.outage(("tempo",), journal):
            pass
    assert docker_state[1] == []


def test_backend_probe_is_limited_to_internal_observability_endpoints(
    docker_state,
):
    _state, calls, _docker = docker_state
    with pytest.raises(target.ReliabilityError, match="ENDPOINT_DENIED"):
        target.backend_get("https://example.test:443/health", _state)
    assert calls == []


def test_mcp_health_localizes_unhealthy_datasource(monkeypatch):
    from dev_tools import observability_mcp

    calls = []

    def gateway(name, arguments):
        calls.append(name)
        if name == "check_datasources_health":
            return {
                "results": [
                    {"uid": "prometheus", "status": "OK"},
                    {"uid": "loki", "status": "ERROR"},
                    {"uid": "tempo", "status": "OK"},
                ]
            }
        if name == "tempo_get-trace":
            return {"trace": {"traceId": arguments["trace_id"]}}
        return {"data": {"result": [{"value": [1, "1"]}]}}

    monkeypatch.setattr(observability_mcp, "_gateway_tool_call", gateway)
    result = target.mcp_signals(
        {"environment": "probe", "marker": "marker", "trace_ids": ["trace"]}
    )

    assert calls == [
        "check_datasources_health",
        "query_prometheus",
        "tempo_get-trace",
    ]
    assert result["loki"] == {"responded": False, "source_unavailable": True}
    assert result["prometheus"] == {"responded": True, "nonempty": True}
    assert result["tempo"] == {"responded": True, "nonempty": True}
    assert result["operator_checks"] == {"skipped": "DATASOURCE_UNAVAILABLE"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "different"),
        ("image", "different"),
        ("volumes", {}),
        ("restart_count", 1),
    ],
)
def test_identity_or_volume_change_fails_closed(docker_state, field, value):
    before = copy.deepcopy(docker_state[0])
    docker_state[0]["tempo"][field] = value
    with pytest.raises(target.ReliabilityError, match="STORAGE_OR_IDENTITY_CHANGED"):
        target.verify_preserved(before, docker_state[0], ("tempo",))


def test_retention_and_private_ports_contract():
    root = ROOT / "infrastructure/observability"
    compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
    loki = yaml.safe_load((root / "loki/config.yaml").read_text(encoding="utf-8"))
    tempo = yaml.safe_load((root / "tempo/config.yaml").read_text(encoding="utf-8"))
    assert loki["compactor"]["retention_enabled"] is True
    assert loki["compactor"]["working_directory"].startswith("/loki/")
    assert loki["limits_config"]["retention_period"] == "168h"
    assert tempo["compactor"]["compaction"]["block_retention"] == "168h"
    assert (
        "--storage.tsdb.retention.time=15d"
        in compose["services"]["prometheus"]["command"]
    )
    for service in ("loki", "tempo", "prometheus"):
        assert not compose["services"][service].get("ports")
    for service in ("alloy", "grafana"):
        assert all(
            port.startswith("127.0.0.1:")
            for port in compose["services"][service]["ports"]
        )
    config = (root / "alloy/config.alloy").read_text(encoding="utf-8")
    for name in ("loki", "tempo"):
        block = config.split(f'otelcol.exporter.otlphttp "{name}" {{', 1)[1].split(
            'otelcol.exporter.otlphttp "', 1
        )[0]
        normalized = re.sub(r"[ \t]+", " ", block)
        assert 'sizer = "bytes"' in normalized
        assert "queue_size = 16777216" in normalized
        assert 'max_elapsed_time = "5m"' in normalized
        assert "block_on_overflow = false" in normalized
    assert "otelcol.storage.file" not in config
    assert 'max_keepalive_time = "8h"' in re.sub(r"[ \t]+", " ", config)


def test_inventory_rejects_malformed_docker_inspect(monkeypatch):
    def fake_docker(*arguments, **_kwargs):
        return "container-id\n" if arguments[:2] == ("ps", "-aq") else "[{}]"

    monkeypatch.setattr(target, "docker", fake_docker)

    with pytest.raises(target.ReliabilityError, match="INVENTORY_INVALID"):
        target.inventory()


def test_subprocess_emit_timeout_is_safe(monkeypatch, tmp_path):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("emit", 15)

    monkeypatch.setattr(target.subprocess, "run", timeout)

    with pytest.raises(target.ReliabilityError, match="EMITTER_FAILED"):
        target.subprocess_emit(tmp_path / "emit")
