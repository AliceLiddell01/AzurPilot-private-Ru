"""Ограниченная проверка телеметрии приложения и изолированных отказов Docker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from dev_tools.infrastructure_doctor import CANONICAL_PROJECT, run_docker

SERVICES = ("alloy", "loki", "prometheus", "tempo", "grafana")
MCP_BACKENDS = ("prometheus", "loki", "tempo")
MCP_OPERATOR_CHECKS = ("dashboard", "dashboard_queries", "alerts")
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

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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


def _journal_payload(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)


def _create_recovery_journal(path: Path, state: dict) -> None:
    """Создать полный journal эксклюзивно и durable до первой мутации."""
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
    except FileExistsError:
        raise
    except OSError as exc:
        raise ReliabilityError("OBSERVABILITY_RECOVERY_JOURNAL_WRITE_FAILED") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_journal_payload(state))
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, TypeError, ValueError) as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ReliabilityError("OBSERVABILITY_RECOVERY_JOURNAL_WRITE_FAILED") from exc


def _write_authoritative_journal(path: Path, state: dict) -> None:
    """Атомарно обновить recovery journal; failure запрещает следующую мутацию."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(_journal_payload(state))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ReliabilityError("OBSERVABILITY_RECOVERY_JOURNAL_WRITE_FAILED") from exc


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
    state = {
        "before": before,
        "attempted": [],
        "stopping": [],
        "stopped": [],
        "recovered": [],
        "recovery_errors": [],
    }
    _create_recovery_journal(journal, state)
    original_error = None
    try:
        for service in services:
            verify_preserved(before, inventory(), services)
            state["attempted"].append(service)
            state["stopping"].append(service)
            try:
                _write_authoritative_journal(journal, state)
            except BaseException:
                state["stopping"].remove(service)
                raise
            docker("stop", "--time", "10", before[service]["id"])
            if inventory()[service]["status"] != "exited":
                raise ReliabilityError("OBSERVABILITY_STOP_NOT_CONFIRMED")
            state["stopping"].remove(service)
            state["stopped"].append(service)
            _write_authoritative_journal(journal, state)
        verify_preserved(before, inventory(), services)
        yield state
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        recovery_services = set(state["stopping"]) | set(state["stopped"])
        for service in reversed(state["attempted"]):
            if service not in recovery_services:
                continue
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
        try:
            _write_authoritative_journal(journal, state)
        except ReliabilityError as exc:
            if original_error is None:
                raise
            original_error.add_note(exc.code)
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
                assert_mcp_partial_outage(
                    result["mcp_during"],
                    set(services).intersection(MCP_BACKENDS),
                )
            if "alloy" not in services:
                result["metrics_during"] = internal_metrics()
            if (
                not emission["local_log"]
                or not emission["incident"]
                or not emission["incident_sanitized"]
                or emission["screenshots"] <= 0
                or emission["normal_runtime_text_files"]
                or emission["shutdown_seconds"] > 4
            ):
                raise ReliabilityError("OBSERVABILITY_LOCAL_FALLBACK_FAILED")
            if "alloy" not in services:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        assert_direct_partial_signals(
                            result["during_signals"], services
                        )
                        break
                    except ReliabilityError as exc:
                        if str(exc) != "OBSERVABILITY_UNAFFECTED_SIGNAL_FAILED":
                            raise
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(2)
                        result["during_signals"] = query_signals(emission)
            else:
                assert_direct_partial_signals(result["during_signals"], services)
            if "alloy" not in services:
                assert_bounded_outage_metrics(
                    result["metrics_during"], services=services
                )
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
            if "alloy" not in services:
                result["metrics_during_end"] = internal_metrics()
                assert_bounded_outage_metrics(
                    result["metrics_during"] + result["metrics_during_end"],
                    services=services,
                )
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
        assert_mcp_after_recovery(result["mcp_after"])
        result["ok"] = True
        return result
    finally:
        _write_json_best_effort(output / "result.json", result)


def docker(*arguments: str, timeout: int = 30) -> str:
    try:
        result = run_docker(list(arguments), timeout=timeout)
    except (OSError, subprocess.SubprocessError):
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
        raise ReliabilityError("OBSERVABILITY_PROBE_HELPER_UNAVAILABLE")
    try:
        return docker(
            "exec", helper["id"], "wget", "-qO-", "-T", "5", url, timeout=10
        )
    except ReliabilityError as exc:
        raise ReliabilityError("OBSERVABILITY_BACKEND_UNAVAILABLE") from exc


def ready(
    state: dict | None = None, *, errors: dict[str, str] | None = None
) -> dict[str, bool]:
    state = inventory() if state is None else state
    result = {}
    for service, endpoint in ENDPOINTS.items():
        try:
            backend_get(endpoint, state)
            result[service] = True
        except ReliabilityError as exc:
            result[service] = False
            if errors is not None:
                errors[service] = str(exc)
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
    lines = backend_get("http://alloy:12345/metrics").splitlines()
    result = []
    for prefix in prefixes:
        result.extend([line for line in lines if line.startswith(prefix)][:100])
    return result


def emit(output: Path, *, count: int = 1) -> dict:
    """Пройти настоящий bootstrap и общую границу scheduler telemetry без игры."""
    from collections import deque
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from module.logging_context import logging_context
    from module.logger import logger
    from module.observability import scheduler_task_run
    from module.observability.bootstrap import (
        configure_application_observability,
        shutdown_application_observability,
    )
    from module.observability.tracing import get_current_trace_context, trace_operation
    from alas import AzurLaneAutoScript
    import numpy as np

    if not 1 <= count <= 256:
        raise ReliabilityError("OBSERVABILITY_EMISSION_LIMIT")
    marker = uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    # Уникальный resource отделяет synthetic series; игровой profile не изменяется.
    otlp_endpoint = os.environ.get(
        "AZURPILOT_OBSERVABILITY_OTLP_ENDPOINT", "http://127.0.0.1:4318"
    ).strip()
    os.environ.update(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_endpoint,
            "OTEL_RESOURCE_ATTRIBUTES": f"deployment.environment.name=probe-{marker}",
            "OTEL_TRACES_SAMPLER": "always_on",
            "OTEL_METRIC_EXPORT_INTERVAL": "1000",
            "OTEL_EXPORTER_OTLP_TIMEOUT": "1000",
        }
    )
    logger.reset_diagnostic_context()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
    script.config_name = "acceptance"
    script.config = SimpleNamespace(
        Error_LlmAnalysis=False,
        Error_SaveError=True,
        Error_SaveErrorCount=30,
    )
    script.device = SimpleNamespace(
        screenshot_deque=deque(
            [
                {
                    "time": datetime.now(timezone.utc),
                    "image": np.zeros((720, 1280, 3), dtype=np.uint8),
                }
            ],
            maxlen=1,
        )
    )
    started = time.monotonic()
    correlations = []
    try:
        if not configure_application_observability(
            logger, default_component="acceptance"
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
                logger.info(
                    "Синтетический контекст password=synthetic-secret marker=%s",
                    marker,
                )
                for index in range(count):
                    with trace_operation("azurpilot.acceptance.probe"):
                        logger.info(
                            "Проверка observability marker=%s index=%s", marker, index
                        )
                try:
                    raise RuntimeError(
                        f"Синтетический incident marker={marker} password=synthetic-secret"
                    )
                except RuntimeError:
                    logger.error(
                        "Контролируемая ошибка synthetic incident marker=%s", marker
                    )
                    script.save_error_log(error_root=output / "log" / "error")
                task.finish(True)
        action_seconds = time.monotonic() - started
    finally:
        shutdown_started = time.monotonic()
        flushed = shutdown_application_observability(logger, timeout_millis=3000)
        shutdown_seconds = time.monotonic() - shutdown_started
    incident_root = output / "log" / "error" / "acceptance"
    bundles = sorted(incident_root.iterdir()) if incident_root.is_dir() else []
    incident_folder = bundles[-1] if bundles else None
    incident_log = (
        incident_folder / "log.txt" if incident_folder is not None else None
    )
    incident_metadata = (
        incident_folder / "incident.json" if incident_folder is not None else None
    )
    incident_text = (
        incident_log.read_text(encoding="utf-8")
        if incident_log is not None and incident_log.is_file()
        else ""
    )
    result = {
        "marker": marker,
        "environment": f"probe-{marker}",
        "trace_ids": correlations,
        "count": count,
        "action_seconds": action_seconds,
        "shutdown_seconds": shutdown_seconds,
        "flush_completed": flushed,
        "local_log": bool(incident_text) and marker in incident_text,
        "incident_sanitized": "synthetic-secret" not in incident_text,
        "incident": incident_metadata is not None and incident_metadata.is_file(),
        "screenshots": len(list(incident_folder.glob("*.png")))
        if incident_folder is not None
        else 0,
        "normal_runtime_text_files": sorted(
            path.relative_to(output).as_posix()
            for path in output.rglob("*.txt")
            if "log/error" not in path.relative_to(output).as_posix()
        ),
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
            if not isinstance(payload, dict):
                result[service] = False
            elif service == "tempo":
                result[service] = bool(
                    payload.get("batches") or payload.get("resourceSpans")
                )
            else:
                data = payload.get("data")
                result[service] = isinstance(data, dict) and bool(data.get("result"))
        except (ReliabilityError, json.JSONDecodeError, TypeError, ValueError):
            result[service] = False
    return result


def _mcp_payload_is_error(payload: object) -> bool:
    if isinstance(payload, dict):
        if payload.get("isError") is True:
            return True
        return any(_mcp_payload_is_error(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_mcp_payload_is_error(value) for value in payload)
    return False


def _mcp_payload_text(payload: object) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _mcp_has_trace(payload: object, trace_id: str) -> bool:
    if isinstance(payload, dict):
        if payload.get("traceId") == trace_id:
            return True
        return any(_mcp_has_trace(value, trace_id) for value in payload.values())
    if isinstance(payload, list):
        return any(_mcp_has_trace(value, trace_id) for value in payload)
    return False


def _mcp_signal_nonempty(payload: object, *, signal: str, emission: dict) -> bool:
    if _mcp_payload_is_error(payload) or not isinstance(payload, dict):
        return False
    data = payload.get("data")
    text = _mcp_payload_text(payload)
    if signal == "tempo":
        return _mcp_has_trace(payload, emission["trace_ids"][0])
    if isinstance(data, list):
        data_nonempty = bool(data)
    elif isinstance(data, dict):
        data_nonempty = bool(data.get("result"))
    else:
        data_nonempty = False
    if not data_nonempty:
        return False
    expected = emission["marker"] if signal == "loki" else emission["environment"]
    return expected in text


def _mcp_error_names(result: dict) -> list[str]:
    value = result.get("unexpected_is_error", [])
    return list(value) if isinstance(value, list) else []


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
    result = {
        "query_layer_available": True,
        "unexpected_is_error": [],
        "health": {},
    }
    try:
        health_payload = _gateway_tool_call(*requests["health"])
        if _mcp_payload_is_error(health_payload):
            result["unexpected_is_error"].append("health")
            result["health_error"] = "MCP_HEALTH_IS_ERROR"
            return result
        health_results = (
            health_payload.get("results")
            if isinstance(health_payload, dict)
            else None
        )
        if not isinstance(health_results, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("uid"), str)
            for item in health_results
        ):
            result["health_error"] = "MCP_HEALTH_RESPONSE_INVALID"
            return result
        result["health"] = {
            item["uid"]: item.get("status") == "OK" for item in health_results
        }
    except Exception as exc:
        result["query_layer_available"] = False
        result["health_error"] = type(exc).__name__
        return result
    datasource_by_signal = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo"}
    for signal in MCP_BACKENDS:
        name, arguments = requests[signal]
        if not result["health"].get(datasource_by_signal[signal], False):
            result[signal] = {
                "responded": False,
                "nonempty": False,
                "source_unavailable": True,
            }
            continue
        try:
            payload = _gateway_tool_call(name, arguments)
            is_error = _mcp_payload_is_error(payload)
            if is_error:
                result["unexpected_is_error"].append(signal)
            result[signal] = {
                "responded": not is_error,
                "nonempty": _mcp_signal_nonempty(
                    payload, signal=signal, emission=emission
                ),
                "is_error": is_error,
            }
        except Exception as exc:
            result[signal] = {
                "responded": False,
                "nonempty": False,
                "error": type(exc).__name__,
            }
    if all(result["health"].get(service) is True for service in MCP_BACKENDS):
        result["operator_checks"] = {}
        for signal, (name, arguments) in operator_checks.items():
            try:
                payload = _gateway_tool_call(name, arguments)
                is_error = _mcp_payload_is_error(payload)
                if is_error:
                    result["unexpected_is_error"].append(signal)
                result[signal] = {
                    "responded": not is_error,
                    "nonempty": bool(payload) and not is_error,
                    "operation_ok": not is_error,
                    "is_error": is_error,
                }
                result["operator_checks"][signal] = result[signal]
            except Exception as exc:
                result[signal] = {
                    "responded": False,
                    "nonempty": False,
                    "operation_ok": False,
                    "error": type(exc).__name__,
                }
                result["operator_checks"][signal] = result[signal]
    else:
        result["operator_checks"] = {"skipped": "DATASOURCE_UNAVAILABLE"}
    return result


def assert_mcp_after_recovery(result: dict) -> None:
    """Сделать все post-recovery MCP reads обязательным acceptance gate."""
    if result.get("query_layer_available") is not True:
        raise ReliabilityError("OBSERVABILITY_MCP_NOT_RECOVERED")
    if _mcp_error_names(result):
        raise ReliabilityError("OBSERVABILITY_MCP_UNEXPECTED_ERROR")
    health = result.get("health")
    if not isinstance(health, dict) or not all(
        health.get(service) is True for service in MCP_BACKENDS
    ):
        raise ReliabilityError("OBSERVABILITY_MCP_NOT_RECOVERED")
    for signal in MCP_BACKENDS:
        item = result.get(signal)
        if (
            not isinstance(item, dict)
            or item.get("responded") is not True
            or item.get("nonempty") is not True
        ):
            raise ReliabilityError("OBSERVABILITY_MCP_NOT_RECOVERED")
    if not isinstance(result["tempo"], dict) or result["tempo"].get("nonempty") is not True:
        raise ReliabilityError("OBSERVABILITY_MCP_NOT_RECOVERED")
    checks = result.get("operator_checks")
    if not isinstance(checks, dict) or "skipped" in checks:
        raise ReliabilityError("OBSERVABILITY_MCP_NOT_RECOVERED")
    for name in MCP_OPERATOR_CHECKS:
        item = checks.get(name)
        if (
            not isinstance(item, dict)
            or item.get("responded") is not True
            or item.get("operation_ok") is not True
        ):
            raise ReliabilityError("OBSERVABILITY_MCP_NOT_RECOVERED")


def assert_mcp_partial_outage(result: dict, affected: set[str]) -> None:
    """Проверить expected affected/unaffected MCP behavior во время outage."""
    if result.get("query_layer_available") is not True:
        raise ReliabilityError("OBSERVABILITY_MCP_PARTIAL_QUERY_LAYER_FAILED")
    unexpected_errors = set(_mcp_error_names(result)) - affected
    if unexpected_errors:
        raise ReliabilityError("OBSERVABILITY_MCP_UNEXPECTED_ERROR")
    health = result.get("health")
    if not isinstance(health, dict) or not all(
        service in health for service in MCP_BACKENDS
    ):
        raise ReliabilityError("OBSERVABILITY_MCP_PARTIAL_OUTAGE_CONTRACT_FAILED")
    for signal in MCP_BACKENDS:
        item = result.get(signal)
        if signal in affected:
            if not isinstance(item, dict):
                raise ReliabilityError("OBSERVABILITY_MCP_PARTIAL_OUTAGE_CONTRACT_FAILED")
            if health.get(signal) is True:
                if item.get("is_error") is not True:
                    raise ReliabilityError(
                        "OBSERVABILITY_MCP_PARTIAL_OUTAGE_CONTRACT_FAILED"
                    )
            elif item.get("source_unavailable") is not True:
                raise ReliabilityError("OBSERVABILITY_MCP_PARTIAL_OUTAGE_CONTRACT_FAILED")
            continue
        if (
            health.get(signal) is not True
            or not isinstance(item, dict)
            or item.get("responded") is not True
            or item.get("nonempty") is not True
        ):
            raise ReliabilityError("OBSERVABILITY_MCP_UNAFFECTED_SIGNAL_FAILED")


def assert_direct_partial_signals(
    result: dict[str, bool], services: tuple[str, ...]
) -> None:
    """Проверить direct backend reads, когда Grafana query layer недоступен."""
    if "alloy" in services:
        return
    affected = set(services).intersection(MCP_BACKENDS)
    for signal in MCP_BACKENDS:
        expected = signal not in affected
        if result.get(signal) is not expected:
            raise ReliabilityError(
                "OBSERVABILITY_AFFECTED_SIGNAL_CONTRACT_FAILED"
                if not expected
                else "OBSERVABILITY_UNAFFECTED_SIGNAL_FAILED"
            )


_METRIC_VALUE_RE = re.compile(r"\s(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*$")
_EXPORTER_LABEL_RE = re.compile(r'(?:^|,)exporter="([^"]+)"(?:,|$)')


def _metric_values(metrics: list[str], prefixes: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for line in metrics:
        if not line.startswith(prefixes):
            continue
        match = _METRIC_VALUE_RE.search(line)
        if match is None:
            continue
        try:
            values.append(float(match.group(1)))
        except ValueError:
            continue
    return values


def _metric_lines(
    metrics: list[str],
    prefixes: tuple[str, ...],
    *,
    exporter: str | None = None,
) -> list[str]:
    lines = [line for line in metrics if line.startswith(prefixes)]
    if exporter is None:
        return lines
    matched = []
    for line in lines:
        labels = line.partition("{")[2].split("}", 1)[0]
        label_match = _EXPORTER_LABEL_RE.search(labels)
        if label_match is None:
            continue
        exporter_value = label_match.group(1).casefold()
        expected_exporter = exporter.casefold()
        if (
            exporter_value == expected_exporter
            or exporter_value.endswith(f".{expected_exporter}")
            or exporter_value.endswith(f"/{expected_exporter}")
        ):
            matched.append(line)
    if matched:
        return matched
    # Некоторые версии collector не добавляют exporter label. Сохраняем
    # tolerant contract для такого bounded endpoint, если labels отсутствуют.
    unlabeled = [line for line in lines if "{" not in line]
    return unlabeled


def assert_bounded_outage_metrics(
    metrics: list[str], *, services: tuple[str, ...]
) -> None:
    """Проверить bounded queue/WAL evidence без обязательной event-specific series."""
    if "alloy" in services:
        return
    bounded_prefixes = (
        "otelcol_exporter_queue_size{",
        "otelcol_exporter_queue_capacity{",
        "otelcol_exporter_enqueue_failed_",
        "otelcol_exporter_send_failed_",
        "otelcol_receiver_refused_",
        "prometheus_remote_storage_samples_pending{",
        "prometheus_remote_storage_samples_retries_total{",
        "prometheus_remote_storage_enqueue_retries_total{",
        "prometheus_remote_write_wal_samples_appended_total{",
    )
    if not any(line.startswith(bounded_prefixes) for line in metrics):
        raise ReliabilityError("OBSERVABILITY_BOUNDED_SIGNAL_EVIDENCE_MISSING")

    queue_prefixes = ("otelcol_exporter_queue_size{",)
    capacity_prefixes = ("otelcol_exporter_queue_capacity{",)
    failure_prefixes = (
        "otelcol_exporter_enqueue_failed_",
        "otelcol_exporter_send_failed_",
        "otelcol_receiver_refused_",
    )
    for exporter in {"loki", "tempo"}.intersection(services):
        queue_lines = _metric_lines(metrics, queue_prefixes, exporter=exporter)
        capacity_lines = _metric_lines(metrics, capacity_prefixes, exporter=exporter)
        failure_lines = _metric_lines(metrics, failure_prefixes, exporter=exporter)
        queue_values = _metric_values(queue_lines, queue_prefixes)
        capacity_values = _metric_values(capacity_lines, capacity_prefixes)
        if queue_values and capacity_values:
            if max(queue_values) > max(capacity_values):
                raise ReliabilityError("OBSERVABILITY_QUEUE_CAPACITY_EXCEEDED")
            continue
        if not failure_lines:
            raise ReliabilityError("OBSERVABILITY_BOUNDED_SIGNAL_EVIDENCE_MISSING")

    if "prometheus" in services:
        prometheus_prefixes = (
            "prometheus_remote_storage_samples_pending{",
            "prometheus_remote_storage_samples_retries_total{",
            "prometheus_remote_storage_enqueue_retries_total{",
            "prometheus_remote_write_wal_samples_appended_total{",
        )
        if not any(line.startswith(prometheus_prefixes) for line in metrics):
            raise ReliabilityError("OBSERVABILITY_BOUNDED_SIGNAL_EVIDENCE_MISSING")


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
            parser.error("Для emit/query/outage требуется --output")
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
