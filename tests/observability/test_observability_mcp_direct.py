from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dev_tools import observability_mcp as target


def test_grafana_direct_allowlist_blocks_mutations():
    assert "list_datasources" in target.GRAFANA_READ_ONLY_TOOLS
    assert target.GRAFANA_BLOCKED_TOOLS.isdisjoint(target.GRAFANA_READ_ONLY_TOOLS)


def test_unknown_tool_is_rejected_before_transport(monkeypatch):
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(target, "load_integration_config", fail_if_called)
    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_READ_ONLY_TOOL_DENIED"
    ):
        target.read_only_grafana_tool_call("arbitrary_tool", {})
    assert called is False


def test_arguments_and_provider_payload_are_bounded_and_redacted():
    arguments = target._bounded_arguments(
        {
            "query": "up",
            "token": "fixture-token",
            "nested": {"password": "fixture-password"},
        }
    )
    assert arguments["token"] == "<redacted>"
    assert arguments["nested"]["password"] == "<redacted>"
    assert "fixture-token" not in str(arguments)

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


def test_argument_limit_fails_closed():
    prefix = len(
        json.dumps({"query": ""}, ensure_ascii=False).encode("utf-8")
    )
    allowed = {"query": "x" * (target.MAX_ARGUMENT_BYTES - prefix)}
    assert len(json.dumps(allowed, ensure_ascii=False).encode("utf-8")) == (
        target.MAX_ARGUMENT_BYTES
    )
    assert target._bounded_arguments(allowed)["query"]

    with pytest.raises(
        target.ObservabilityMcpError, match="GRAFANA_ARGUMENTS_TOO_LARGE"
    ):
        target._bounded_arguments({"query": "x" * (target.MAX_ARGUMENT_BYTES + 1)})


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
