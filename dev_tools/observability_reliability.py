"""Ограниченная проверка телеметрии приложения и изолированных отказов Docker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from dev_tools.infrastructure_doctor import CANONICAL_PROJECT, _run

SERVICES = ("alloy", "loki", "prometheus", "tempo", "grafana")
ENDPOINTS = {
    "alloy": "http://alloy:12345/-/ready",
    "loki": "http://loki:3100/ready",
    "prometheus": "http://prometheus:9090/-/ready",
    "tempo": "http://tempo:3200/ready",
    "grafana": "http://grafana:3000/api/health",
}
_INTERNAL_ENDPOINTS = {
    "alloy": 12345,
    "loki": 3100,
    "prometheus": 9090,
    "tempo": 3200,
    "grafana": 3000,
}


class ReliabilityError(RuntimeError):
    """Безопасный код ошибки без Docker stderr и пользовательских данных."""


def validate_services(services: tuple[str, ...]) -> None:
    if (
        not services
        or len(set(services)) != len(services)
        or any(s not in SERVICES for s in services)
    ):
        raise ReliabilityError("OBSERVABILITY_SERVICE_DENIED")


def verify_preserved(before: dict, after: dict, changed: tuple[str, ...]) -> None:
    """Проверить identity, volumes и состояние посторонних services."""
    if before.keys() != after.keys():
        raise ReliabilityError("OBSERVABILITY_CONTAINER_SET_CHANGED")
    for service, original in before.items():
        current = after[service]
        if service not in changed:
            if current != original:
                raise ReliabilityError("OBSERVABILITY_UNRELATED_SERVICE_CHANGED")
        elif any(
            current[key] != original[key]
            for key in ("id", "image", "volumes", "restart_count")
        ):
            raise ReliabilityError("OBSERVABILITY_STORAGE_OR_IDENTITY_CHANGED")


@contextmanager
def outage(services: tuple[str, ...], journal: Path):
    """Остановить выбранные containers и восстановить их после ошибки probe."""
    validate_services(services)
    before = inventory()
    if (
        not all(ready(before).values())
        or before.get("postgres", {}).get("health") != "healthy"
    ):
        raise ReliabilityError("OBSERVABILITY_BASELINE_NOT_READY")
    if any(before.get(s, {}).get("status") != "running" for s in services):
        raise ReliabilityError("OBSERVABILITY_SERVICE_NOT_RUNNING")
    journal.parent.mkdir(parents=True, exist_ok=True)
    state = {"before": before, "attempted": [], "recovered": [], "recovery_errors": []}
    # Маркер только для создания защищает незавершённое recovery evidence.
    with journal.open("x", encoding="utf-8") as stream:
        json.dump(state, stream)
    original_error = None
    try:
        for service in services:
            verify_preserved(before, inventory(), services)
            state["attempted"].append(service)
            _write_json_best_effort(journal, state)
            docker("stop", "--time", "10", before[service]["id"])
            if inventory()[service]["status"] != "exited":
                raise ReliabilityError("OBSERVABILITY_STOP_NOT_CONFIRMED")
        verify_preserved(before, inventory(), services)
        yield state
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        for service in reversed(state["attempted"]):
            try:
                current = inventory().get(service, {})
                if (
                    current.get("id") != before[service]["id"]
                    or current.get("volumes") != before[service]["volumes"]
                ):
                    raise ReliabilityError("OBSERVABILITY_RECOVERY_IDENTITY_CHANGED")
                if current["status"] != "running":
                    docker("start", before[service]["id"])
                state["recovered"].append(service)
            except Exception as exc:
                state["recovery_errors"].append(
                    {"service": service, "error": type(exc).__name__}
                )
        _write_json_best_effort(journal, state)
        if state["recovery_errors"] and original_error is None:
            raise ReliabilityError("OBSERVABILITY_RECOVERY_FAILED")


def _write_json_best_effort(path: Path, payload: dict) -> None:
    """Сохранить диагностический артефакт, не маскируя исходный outage error."""
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        # Основной результат smoke важнее необязательной записи evidence.
        pass


def wait_ready(timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(ready().values()):
            return
        time.sleep(2)
    raise ReliabilityError("OBSERVABILITY_RECOVERY_NOT_READY")


def subprocess_emit(output: Path, count: int = 1) -> dict:
    """Изолировать переменные SDK и ограниченное завершение процесса."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "dev_tools.observability_reliability",
                "emit",
                "--output",
                str(output),
                "--count",
                str(count),
            ],
            env=environment,
            capture_output=True,
            timeout=15,
            check=False,
            **options,
        )
    except subprocess.TimeoutExpired:
        raise ReliabilityError("OBSERVABILITY_EMITTER_FAILED") from None
    if result.returncode:
        raise ReliabilityError("OBSERVABILITY_EMITTER_FAILED")
    return json.loads((output / "emission.json").read_text(encoding="utf-8"))


def wait_signals(emission: dict, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        signals = query_signals(emission)
        if all(signals.values()):
            return signals
        time.sleep(2)
    raise ReliabilityError("OBSERVABILITY_SIGNALS_NOT_RECOVERED")


def run_scenario(
    services: tuple[str, ...], output: Path, hold_seconds: int = 15
) -> dict:
    """Проверить доставку до, во время и после отказа."""
    validate_services(services)
    if not 1 <= hold_seconds <= 300:
        raise ReliabilityError("OBSERVABILITY_OUTAGE_DURATION_DENIED")
    # Некоторые backends после cold start сообщают 503 несколько секунд;
    # это не baseline failure, пока bounded readiness window не исчерпан.
    wait_ready()
    baseline = subprocess_emit(output / "before")
    wait_signals(baseline)
    result = {
        "services": services,
        "before": baseline,
        "metrics_before": internal_metrics(),
    }
    try:
        with outage(services, output / "recovery.json") as state:
            emission = subprocess_emit(output / "during", count=128)
            result["during"] = emission
            result["during_signals"] = query_signals(emission)
            result["during_ready"] = ready()
            if "grafana" in services:
                # При намеренно остановленной query layer transport failure
                # является ожидаемым результатом и не должен запускать retry storm.
                result["mcp_during"] = {
                    "query_layer_available": False,
                    "expected": "GRAFANA_UNAVAILABLE",
                }
            else:
                result["mcp_during"] = mcp_signals(baseline)
            if "alloy" not in services:
                result["metrics_during"] = internal_metrics()
            if (
                not emission["local_log"]
                or not emission["incident"]
                or emission["shutdown_seconds"] > 4
            ):
                raise ReliabilityError("OBSERVABILITY_LOCAL_FALLBACK_FAILED")
            unaffected = set(("loki", "prometheus", "tempo")) - set(services)
            if "alloy" not in services:
                deadline = time.monotonic() + 30
                while not all(result["during_signals"][s] for s in unaffected):
                    if time.monotonic() >= deadline:
                        raise ReliabilityError("OBSERVABILITY_UNAFFECTED_SIGNAL_FAILED")
                    time.sleep(2)
                    result["during_signals"] = query_signals(emission)
            print(
                json.dumps(
                    {
                        "phase": "outage",
                        "services": services,
                        "hold_seconds": hold_seconds,
                    }
                ),
                flush=True,
            )
            time.sleep(hold_seconds)
            verify_preserved(state["before"], inventory(), services)
        wait_ready()
        verify_preserved(state["before"], inventory(), services)
        result["history_preserved"] = wait_signals(baseline)
        if "alloy" not in services:
            result["backlog"] = wait_signals(emission)
        fresh = subprocess_emit(output / "after")
        result["after"] = fresh
        result["fresh_signals"] = wait_signals(fresh)
        result["metrics_after"] = internal_metrics()
        result["mcp_after"] = mcp_signals(fresh)
        if (
            not all(result["mcp_after"]["health"].values())
            or len(result["mcp_after"]["health"]) != 3
        ):
            raise ReliabilityError("OBSERVABILITY_MCP_NOT_RECOVERED")
        result["ok"] = True
        return result
    finally:
        _write_json_best_effort(output / "result.json", result)


def docker(*arguments: str, timeout: int = 30) -> str:
    try:
        result = _run(list(arguments), timeout=timeout)
    except OSError, subprocess.SubprocessError:
        raise ReliabilityError("OBSERVABILITY_DOCKER_UNAVAILABLE") from None
    if result.returncode:
        raise ReliabilityError("OBSERVABILITY_DOCKER_COMMAND_FAILED")
    return result.stdout


def inventory() -> dict:
    """Снять разрешённые metadata без env и содержимого secrets."""
    ids = docker(
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={CANONICAL_PROJECT}"
    ).split()
    if not ids:
        raise ReliabilityError("OBSERVABILITY_PROJECT_NOT_FOUND")
    try:
        records = json.loads(docker("inspect", *ids))
        if not isinstance(records, list):
            raise TypeError("Docker inspect result is not a list")
        result = {}
        for item in records:
            service = item["Config"]["Labels"]["com.docker.compose.service"]
            if not isinstance(service, str) or not service:
                raise TypeError("Docker service label is invalid")
            result[service] = {
                "id": item["Id"],
                "image": item["Image"],
                "status": item["State"]["Status"],
                "health": item["State"].get("Health", {}).get("Status"),
                "started_at": item["State"]["StartedAt"],
                "restart_count": item["RestartCount"],
                "volumes": {
                    m["Destination"]: m["Name"]
                    for m in item["Mounts"]
                    if m["Type"] == "volume"
                },
                "ports": item["NetworkSettings"]["Ports"],
            }
        return result
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError):
        raise ReliabilityError("OBSERVABILITY_INVENTORY_INVALID") from None


def backend_get(url: str, state: dict | None = None) -> str:
    """Использовать существующий network namespace без нового host port."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ReliabilityError("OBSERVABILITY_ENDPOINT_DENIED") from None
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname not in _INTERNAL_ENDPOINTS
        or port != _INTERNAL_ENDPOINTS[parsed.hostname]
    ):
        raise ReliabilityError("OBSERVABILITY_ENDPOINT_DENIED")
    state = inventory() if state is None else state
    # pgAdmin не входит в outage allowlist и содержит wget для read-only probes.
    helper = state.get("pgadmin", {})
    if helper.get("status") != "running":
        raise ReliabilityError("OBSERVABILITY_NETWORK_PROBE_UNAVAILABLE")
    return docker("exec", helper["id"], "wget", "-qO-", "-T", "5", url, timeout=10)


def ready(state: dict | None = None) -> dict[str, bool]:
    state = inventory() if state is None else state
    result = {}
    for service, endpoint in ENDPOINTS.items():
        try:
            backend_get(endpoint, state)
            result[service] = True
        except ReliabilityError:
            result[service] = False
    return result


def internal_metrics() -> list[str]:
    """Сохранить ограниченные числовые метрики collector без полного endpoint."""
    prefixes = (
        "otelcol_exporter_queue_size{",
        "otelcol_exporter_queue_capacity{",
        "otelcol_exporter_enqueue_failed_",
        "otelcol_exporter_send_failed_",
        "otelcol_exporter_in_flight_requests",
        "otelcol_receiver_refused_",
        "otelcol_exporter_queue_batch_send_size_count{",
        "prometheus_remote_storage_samples_retries_total{",
        "prometheus_remote_storage_enqueue_retries_total{",
        "prometheus_remote_storage_samples_pending{",
        "prometheus_remote_write_wal_samples_appended_total{",
        "process_resident_memory_bytes ",
    )
    return [
        line
        for line in backend_get("http://alloy:12345/metrics").splitlines()
        if line.startswith(prefixes)
    ][:100]


def emit(output: Path, *, count: int = 1) -> dict:
    """Пройти настоящий bootstrap и общую границу scheduler telemetry без игры."""
    from types import SimpleNamespace

    from module.logging_context import logging_context
    from module.observability import scheduler_task_run
    from module.observability.bootstrap import (
        configure_application_observability,
        shutdown_application_observability,
    )
    from module.observability.incident import (
        build_incident_metadata,
        write_incident_metadata,
    )
    from module.observability.tracing import get_current_trace_context, trace_operation

    if not 1 <= count <= 256:
        raise ReliabilityError("OBSERVABILITY_EMISSION_LIMIT")
    marker = uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    # Уникальный resource отделяет synthetic series; игровой profile не изменяется.
    os.environ.update(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
            "OTEL_RESOURCE_ATTRIBUTES": f"deployment.environment.name=probe-{marker}",
            "OTEL_TRACES_SAMPLER": "always_on",
            "OTEL_METRIC_EXPORT_INTERVAL": "1000",
            "OTEL_EXPORTER_OTLP_TIMEOUT": "1000",
        }
    )
    target = logging.getLogger("azurpilot.observability.probe")
    target.setLevel(logging.INFO)
    target.propagate = False
    handler = logging.FileHandler(output / "log.txt", encoding="utf-8")
    target.addHandler(handler)
    started = time.monotonic()
    correlations = []
    try:
        if not configure_application_observability(
            target, default_component="acceptance"
        ):
            raise ReliabilityError("OBSERVABILITY_BOOTSTRAP_FAILED")
        with logging_context(
            profile="acceptance", component="acceptance", run_id=marker
        ):
            with scheduler_task_run(
                profile="acceptance",
                task=SimpleNamespace(command="TelemetryProbe"),
                registry=("TelemetryProbe",),
            ) as task:
                context = get_current_trace_context()
                if context is None:
                    raise ReliabilityError("OBSERVABILITY_TRACE_CONTEXT_MISSING")
                correlations.append(context.trace_id)
                for index in range(count):
                    with trace_operation("azurpilot.acceptance.probe"):
                        target.info(
                            "Проверка observability marker=%s index=%s", marker, index
                        )
                write_incident_metadata(
                    output,
                    build_incident_metadata(profile="acceptance", exception=None),
                )
                task.finish(True)
        action_seconds = time.monotonic() - started
    finally:
        shutdown_started = time.monotonic()
        flushed = shutdown_application_observability(target, timeout_millis=3000)
        shutdown_seconds = time.monotonic() - shutdown_started
        target.removeHandler(handler)
        handler.close()
    result = {
        "marker": marker,
        "environment": f"probe-{marker}",
        "trace_ids": correlations,
        "count": count,
        "action_seconds": action_seconds,
        "shutdown_seconds": shutdown_seconds,
        "flush_completed": flushed,
        "local_log": marker in (output / "log.txt").read_text(encoding="utf-8"),
        "incident": (output / "incident.json").is_file(),
    }
    (output / "emission.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    return result


def query_signals(emission: dict) -> dict[str, bool]:
    """Проверить marker через query API без записи в backends."""
    queries = {
        "loki": "http://loki:3100/loki/api/v1/query_range?"
        + urlencode(
            {
                "query": '{deployment_environment_name="'
                + emission["environment"]
                + '"} |= "'
                + emission["marker"]
                + '"',
                "limit": "10",
            }
        ),
        "prometheus": "http://prometheus:9090/api/v1/query?"
        + urlencode(
            {
                "query": 'azurpilot_task_run_total{deployment_environment_name="'
                + emission["environment"]
                + '"}'
            }
        ),
        "tempo": "http://tempo:3200/api/traces/" + emission["trace_ids"][0],
    }
    result = {}
    for service, url in queries.items():
        try:
            payload = json.loads(backend_get(url))
            result[service] = (
                bool(payload.get("batches") or payload.get("resourceSpans"))
                if service == "tempo"
                else bool(payload.get("data", {}).get("result"))
            )
        except ReliabilityError, json.JSONDecodeError:
            result[service] = False
    return result


def mcp_signals(emission: dict) -> dict:
    """Проверить read-only Gateway без raw logs и credentials в отчёте."""
    from dev_tools.observability_mcp import _gateway_tool_call

    requests = {
        "health": ("check_datasources_health", {}),
        "prometheus": (
            "query_prometheus",
            {
                "datasourceUid": "prometheus",
                "expr": 'azurpilot_task_run_total{deployment_environment_name="'
                + emission["environment"]
                + '"}',
                "queryType": "instant",
                "startTime": "now-5m",
                "endTime": "now",
            },
        ),
        "loki": (
            "query_loki_logs",
            {
                "datasourceUid": "loki",
                "logql": '{service_name="azurpilot"} |= "' + emission["marker"] + '"',
                "limit": "1",
            },
        ),
        "tempo": (
            "tempo_get-trace",
            {"datasourceUid": "tempo", "trace_id": emission["trace_ids"][0]},
        ),
    }
    operator_checks = {
        "dashboard": (
            "get_dashboard_summary",
            {"uid": "azurpilot-overview"},
        ),
        "dashboard_queries": (
            "get_dashboard_panel_queries",
            {"uid": "azurpilot-overview"},
        ),
        "alerts": (
            "alerting_manage_rules",
            {"operation": "list", "rule_limit": "50"},
        ),
    }
    result = {}
    try:
        health_payload = _gateway_tool_call(*requests["health"])
        result["health"] = {
            item["uid"]: item.get("status") == "OK"
            for item in health_payload.get("results", [])
        }
    except Exception as exc:
        return {
            "health": {},
            "query_layer_available": False,
            "health_error": type(exc).__name__,
        }
    datasource_by_signal = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo"}
    for signal in ("prometheus", "loki", "tempo"):
        name, arguments = requests[signal]
        if not result["health"].get(datasource_by_signal[signal], False):
            result[signal] = {"responded": False, "source_unavailable": True}
            continue
        try:
            payload = _gateway_tool_call(name, arguments)
            nonempty = (
                payload.get("trace", {}).get("traceId") == emission["trace_ids"][0]
                if signal == "tempo"
                else bool(payload.get("data"))
            )
            result[signal] = {
                "responded": not payload.get("isError", False),
                "nonempty": nonempty,
            }
        except Exception as exc:
            result[signal] = {"responded": False, "error": type(exc).__name__}
    if len(result["health"]) == 3 and all(result["health"].values()):
        for signal, (name, arguments) in operator_checks.items():
            try:
                payload = _gateway_tool_call(name, arguments)
                result[signal] = {
                    "responded": not (
                        isinstance(payload, dict) and payload.get("isError", False)
                    ),
                    "nonempty": bool(payload),
                }
            except Exception as exc:
                result[signal] = {"responded": False, "error": type(exc).__name__}
    else:
        result["operator_checks"] = {"skipped": "DATASOURCE_UNAVAILABLE"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("inventory", "emit", "query", "metrics", "outage")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--services", nargs="+", choices=SERVICES)
    parser.add_argument("--hold-seconds", type=int, default=15)
    args = parser.parse_args()
    try:
        if args.command == "inventory":
            result = {"containers": inventory(), "ready": ready()}
        elif args.command == "metrics":
            result = internal_metrics()
        elif args.output is None:
            parser.error("Для emit/query требуется --output")
        elif args.command == "emit":
            result = emit(args.output, count=args.count)
        elif args.command == "outage":
            result = run_scenario(
                tuple(args.services or ()), args.output, args.hold_seconds
            )
            result = {"ok": result["ok"], "services": result["services"]}
        else:
            result = query_signals(
                json.loads((args.output / "emission.json").read_text(encoding="utf-8"))
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ReliabilityError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": str(exc)
                    if isinstance(exc, ReliabilityError)
                    else type(exc).__name__,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
