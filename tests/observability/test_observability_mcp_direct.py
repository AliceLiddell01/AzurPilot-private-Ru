"""Проверки прямого read-only client общего Grafana MCP HTTP service.

Тесты не требуют docker, сети и реальных секретов: транспортный helper всегда
подменяется, а общий маршрут собирается из DEFAULTS integration config.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from azurpilot.integrations import mcp_client
from azurpilot.integrations.config import IntegrationConfig
from azurpilot.integrations.mcp_client import McpToolCallResult
from dev_tools import observability_mcp as target

CALLER_TOKEN_ENV = "AZURPILOT_GRAFANA_MCP_CALLER_TOKEN"
SHARED_ENDPOINT = "http://127.0.0.1:8777/mcp"
FIXTURE_CALLER_TOKEN = "fixture-caller-token"


def _configure_shared_route(
    monkeypatch: pytest.MonkeyPatch, config: object | None = None
) -> None:
    """Настроить общий HTTP маршрут без provider credential в окружении клиента."""

    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setenv(CALLER_TOKEN_ENV, FIXTURE_CALLER_TOKEN)
    resolved = IntegrationConfig({}, {}) if config is None else config
    monkeypatch.setattr(target, "load_integration_config", lambda _root: resolved)


def _transport_returning(result: McpToolCallResult):
    async def transport(**_kwargs: object) -> McpToolCallResult:
        return result

    return transport


def _transport_raising(error: BaseException):
    async def transport(**_kwargs: object) -> McpToolCallResult:
        raise error

    return transport


def test_grafana_direct_allowlist_blocks_mutations():
    assert "list_datasources" in target.GRAFANA_READ_ONLY_TOOLS
    assert {"query_loki_logs", "query_prometheus"} <= target.GRAFANA_READ_ONLY_TOOLS
    # Tempo reads входят в read-only каталог общего Grafana MCP HTTP service.
    assert {"get_tempo_trace", "search_tempo_traces"} <= target.GRAFANA_READ_ONLY_TOOLS
    assert "grafana_api_request" in target.GRAFANA_BLOCKED_TOOLS
    assert "grafana_api_request" not in target.GRAFANA_READ_ONLY_TOOLS
    assert target.GRAFANA_BLOCKED_TOOLS.isdisjoint(target.GRAFANA_READ_ONLY_TOOLS)


def test_direct_client_reuses_shared_http_transport():
    assert target.call_http_tool is mcp_client.call_http_tool
    # Per-call контейнер больше не существует: маршрут принадлежит общему сервису.
    assert not hasattr(target, "build_command")


def test_unknown_tool_is_rejected_before_transport(monkeypatch):
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("транспорт не должен вызываться")

    monkeypatch.setattr(target, "load_integration_config", fail_if_called)
    monkeypatch.setattr(target, "call_http_tool", fail_if_called)
    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_READ_ONLY_TOOL_DENIED"
    ):
        target.read_only_grafana_tool_call("arbitrary_tool", {})
    assert called is False


def test_known_generic_api_is_rejected_before_transport(monkeypatch):
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("транспорт не должен вызываться")

    monkeypatch.setattr(target, "load_integration_config", fail_if_called)
    monkeypatch.setattr(target, "call_http_tool", fail_if_called)
    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_READ_ONLY_TOOL_DENIED"
    ):
        target.read_only_grafana_tool_call("grafana_api_request", {})
    assert called is False


def test_tool_call_uses_shared_http_endpoint_and_caller_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _configure_shared_route(monkeypatch)
    captured: dict[str, object] = {}

    async def transport(**kwargs: object) -> McpToolCallResult:
        captured.update(kwargs)
        return McpToolCallResult(
            tool_names=("list_datasources", "get_tempo_trace"),
            structured_content={"results": [{"uid": "prometheus", "status": "OK"}]},
        )

    monkeypatch.setattr(target, "call_http_tool", transport)
    payload = target.read_only_grafana_tool_call(
        "list_datasources", {"limit": 1}, repository_root=tmp_path
    )

    assert captured["endpoint"] == SHARED_ENDPOINT
    assert captured["headers"] == {"Authorization": f"Bearer {FIXTURE_CALLER_TOKEN}"}
    assert captured["tool_name"] == "list_datasources"
    assert captured["arguments"] == {"limit": 1}
    assert captured["timeout_seconds"] == target.GRAFANA_DIRECT_TIMEOUT_SECONDS
    assert captured["plan"] is not None
    assert payload["results"][0]["uid"] == "prometheus"
    assert payload["isError"] is False
    assert FIXTURE_CALLER_TOKEN not in json.dumps(payload, ensure_ascii=False)


def test_missing_caller_token_is_typed_state_without_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv(CALLER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        target, "load_integration_config", lambda _root: IntegrationConfig({}, {})
    )
    monkeypatch.setattr(
        target,
        "call_http_tool",
        _transport_raising(AssertionError("транспорт не должен вызываться")),
    )

    with pytest.raises(
        target.ObservabilityMcpError,
        match="INTEGRATION_CALLER_TOKEN_NOT_CONFIGURED",
    ):
        target.read_only_grafana_tool_call(
            "list_datasources", {}, repository_root=tmp_path
        )


def test_non_loopback_endpoint_is_typed_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _configure_shared_route(
        monkeypatch,
        IntegrationConfig({"grafana": {"endpoint": "http://example.test:8777/mcp"}}, {}),
    )
    monkeypatch.setattr(
        target,
        "call_http_tool",
        _transport_raising(AssertionError("транспорт не должен вызываться")),
    )

    with pytest.raises(
        target.ObservabilityMcpError,
        match="INTEGRATION_SHARED_ENDPOINT_NOT_LOOPBACK",
    ):
        target.read_only_grafana_tool_call(
            "list_datasources", {}, repository_root=tmp_path
        )


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (TimeoutError(), "GRAFANA_DIRECT_PROBE_TIMEOUT"),
        (RuntimeError("protocol failure"), "GRAFANA_DIRECT_TOOL_CALL_FAILED"),
    ],
)
def test_transport_failures_keep_public_error_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: BaseException,
    expected_code: str,
):
    _configure_shared_route(monkeypatch)
    monkeypatch.setattr(target, "call_http_tool", _transport_raising(error))

    with pytest.raises(target.ObservabilityMcpError, match=expected_code):
        target.read_only_grafana_tool_call(
            "list_datasources", {}, repository_root=tmp_path
        )


def test_invalid_transport_catalog_keeps_direct_client_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _configure_shared_route(monkeypatch)
    monkeypatch.setattr(
        target,
        "call_http_tool",
        _transport_returning(
            McpToolCallResult(catalog_reason_code="MCP_TOOL_CATALOG_INVALID")
        ),
    )

    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_TOOL_CATALOG_INVALID"
    ):
        target.read_only_grafana_tool_call(
            "list_datasources", {}, repository_root=tmp_path
        )


def test_adapter_catalog_reason_code_is_passed_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _configure_shared_route(monkeypatch)
    monkeypatch.setattr(
        target,
        "call_http_tool",
        _transport_returning(
            McpToolCallResult(
                catalog_reason_code="GRAFANA_TEMPO_TOOL_UNAVAILABLE",
                diagnostics=("tempo_tools_missing",),
            )
        ),
    )

    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_TEMPO_TOOL_UNAVAILABLE"
    ):
        target.read_only_grafana_tool_call(
            "list_datasources", {}, repository_root=tmp_path
        )


def test_tool_absent_from_negotiated_catalog_is_not_observable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _configure_shared_route(monkeypatch)
    monkeypatch.setattr(
        target,
        "call_http_tool",
        _transport_returning(McpToolCallResult(tool_names=("query_prometheus",))),
    )

    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_READ_ONLY_TOOL_NOT_OBSERVABLE"
    ):
        target.read_only_grafana_tool_call(
            "list_datasources", {}, repository_root=tmp_path
        )


def test_probe_reports_read_only_call_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _configure_shared_route(monkeypatch)
    monkeypatch.setattr(
        target,
        "call_http_tool",
        _transport_returning(
            McpToolCallResult(tool_names=("list_datasources",), is_error=True)
        ),
    )

    assert target.main(["--repository-root", str(tmp_path), "--probe"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "unavailable",
        "reason_code": "GRAFANA_READ_ONLY_CALL_ERROR",
    }


def test_probe_ready_payload_has_no_caller_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _configure_shared_route(monkeypatch)
    monkeypatch.setattr(
        target,
        "call_http_tool",
        _transport_returning(
            McpToolCallResult(
                tool_names=("list_datasources",),
                structured_content={"results": [{"uid": "prometheus", "status": "OK"}]},
            )
        ),
    )

    assert target.main(["--repository-root", str(tmp_path), "--probe"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "ready"
    assert FIXTURE_CALLER_TOKEN not in output


def test_arguments_and_provider_payload_are_bounded_and_redacted():
    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_ARGUMENTS_INVALID"
    ):
        target._bounded_arguments(
            {
                "query": "up",
                "token": "fixture-token",
                "nested": {"password": "fixture-password"},
            }
        )

    assert target._bounded_arguments({"query": "up"}) == {"query": "up"}

    result = target._result_payload(
        SimpleNamespace(
            is_error=False,
            content=[SimpleNamespace(text="safe")],
            structured_content={
                "results": [],
                "authorization": "fixture-token",
            },
        )
    )
    assert result["authorization"] == "<redacted>"
    assert "fixture-token" not in str(result)


def test_argument_sanitization_cannot_change_call_payload():
    arguments = {"query": "x", "limit": 5}
    bounded = target._bounded_arguments(arguments)
    assert bounded == arguments
    assert bounded is not arguments

    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_ARGUMENTS_INVALID"
    ):
        target._bounded_arguments({"query": "x", "nested": {"token": "secret"}})


def test_argument_limit_fails_closed():
    prefix = len(
        json.dumps({"query": ""}, ensure_ascii=False).encode("utf-8")
    )
    allowed = {"query": "x" * (target.MAX_ARGUMENT_BYTES - prefix - 1)}
    assert target._bounded_arguments(allowed)["query"]

    assert len(json.dumps(allowed, ensure_ascii=False).encode("utf-8")) == (
        target.MAX_ARGUMENT_BYTES - 1
    )

    exact = {"query": "x" * (target.MAX_ARGUMENT_BYTES - prefix)}
    assert len(json.dumps(exact, ensure_ascii=False).encode("utf-8")) == target.MAX_ARGUMENT_BYTES
    assert target._bounded_arguments(exact)["query"]

    over_limit = {"query": "x" * (target.MAX_ARGUMENT_BYTES - prefix + 1)}
    assert len(json.dumps(over_limit, ensure_ascii=False).encode("utf-8")) == (
        target.MAX_ARGUMENT_BYTES + 1
    )
    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_ARGUMENTS_TOO_LARGE"
    ):
        target._bounded_arguments(over_limit)


def test_result_payload_keeps_structured_results_shape():
    result = target._result_payload(
        SimpleNamespace(
            is_error=False,
            content=[],
            structured_content={
                "results": [{"uid": "prometheus", "status": "OK"}]
            },
        )
    )
    assert result["results"][0]["uid"] == "prometheus"
    assert result["isError"] is False
