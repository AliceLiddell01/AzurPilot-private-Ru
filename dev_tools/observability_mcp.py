"""Прямой read-only client для Grafana MCP.

Модуль оставляет Compose lifecycle и storage в соответствующих
infrastructure-сервисах. Здесь находится только bounded direct transport,
нужный observability acceptance и reliability checks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from azurpilot.integrations.adapters import (
    GRAFANA_BLOCKED_TOOLS,
    GRAFANA_READ_ONLY_TOOLS,
    GrafanaAdapter,
)
from azurpilot.integrations.config import load_integration_config
from azurpilot.tooling.process import safe_environment
from tools.paths import REPOSITORY_ROOT

MAX_RESULT_ITEMS = 128
MAX_RESULT_TEXT = 4096
MAX_ARGUMENT_BYTES = 64 * 1024
GRAFANA_DIRECT_TIMEOUT_SECONDS = 45
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SECRET_KEY_RE = re.compile(r"password|token|secret|authorization|cookie", re.IGNORECASE)


class ObservabilityMcpError(RuntimeError):
    """Безопасный код ошибки direct Grafana read-only client."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_value(value: object, *, key: str | None = None, depth: int = 0) -> object:
    if depth > 6:
        return "<depth-limit>"
    if key and _SECRET_KEY_RE.search(key):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_RESULT_TEXT]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= MAX_RESULT_ITEMS or not isinstance(raw_key, str) or not _KEY_RE.fullmatch(raw_key):
                continue
            result[raw_key[:128]] = _safe_value(raw_value, key=raw_key, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in value[:MAX_RESULT_ITEMS]]
    return str(value)[:MAX_RESULT_TEXT]


def _bounded_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(arguments, Mapping):
        raise ObservabilityMcpError("GRAFANA_ARGUMENTS_INVALID")
    try:
        raw_size = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ObservabilityMcpError("GRAFANA_ARGUMENTS_INVALID") from exc
    if raw_size > MAX_ARGUMENT_BYTES:
        raise ObservabilityMcpError("GRAFANA_ARGUMENTS_TOO_LARGE")
    result = _safe_value(arguments)
    if not isinstance(result, dict):
        raise ObservabilityMcpError("GRAFANA_ARGUMENTS_INVALID")
    return result


def _result_payload(result: object) -> dict[str, object]:
    is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    content = _safe_value(getattr(result, "content", []))
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, Mapping):
        payload = _safe_value(structured)
        if not isinstance(payload, dict):
            payload = {"data": payload}
    else:
        payload = {"data": content}
    payload["isError"] = is_error
    payload["content"] = content
    if "data" not in payload:
        payload["data"] = _safe_value(structured) if structured is not None else content
    return payload


async def _read_only_grafana_tool_call_async(
    tool_name: str,
    arguments: Mapping[str, object],
    *,
    repository_root: Path,
) -> dict[str, object]:
    if tool_name in GRAFANA_BLOCKED_TOOLS or tool_name not in GRAFANA_READ_ONLY_TOOLS:
        raise ObservabilityMcpError("GRAFANA_READ_ONLY_TOOL_DENIED")
    bounded_arguments = _bounded_arguments(arguments)
    config = load_integration_config(repository_root)
    adapter = GrafanaAdapter()
    command = adapter.build_command(repository_root, config)
    if command is None:
        raise ObservabilityMcpError("GRAFANA_DIRECT_ROUTE_NOT_CONFIGURED")
    executable, args, environment_values = command
    try:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        parameters = StdioServerParameters(
            command=executable,
            args=list(args),
            cwd=repository_root,
            env=safe_environment(environment_values),
        )
        async with (
            stdio_client(parameters, errlog=subprocess.DEVNULL) as (read_stream, write_stream),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=GRAFANA_DIRECT_TIMEOUT_SECONDS,
            ) as session,
        ):
            await asyncio.wait_for(
                session.initialize(), timeout=GRAFANA_DIRECT_TIMEOUT_SECONDS
            )
            listed = await asyncio.wait_for(
                session.list_tools(), timeout=GRAFANA_DIRECT_TIMEOUT_SECONDS
            )
            tool_items = getattr(listed, "tools", None)
            if not isinstance(tool_items, list) or len(tool_items) > MAX_RESULT_ITEMS:
                raise ObservabilityMcpError("GRAFANA_TOOL_CATALOG_INVALID")
            tool_names = {
                item.name
                for item in tool_items
                if isinstance(getattr(item, "name", None), str)
                and _KEY_RE.fullmatch(item.name)
            }
            if tool_name not in tool_names:
                raise ObservabilityMcpError("GRAFANA_READ_ONLY_TOOL_NOT_OBSERVABLE")
            result = await asyncio.wait_for(
                session.call_tool(tool_name, bounded_arguments),
                timeout=GRAFANA_DIRECT_TIMEOUT_SECONDS,
            )
    except ObservabilityMcpError:
        raise
    except TimeoutError as exc:
        raise ObservabilityMcpError("GRAFANA_DIRECT_PROBE_TIMEOUT") from exc
    except Exception as exc:  # noqa: BLE001 - bounded direct transport boundary.
        raise ObservabilityMcpError("GRAFANA_DIRECT_TOOL_CALL_FAILED") from exc
    return _result_payload(result)


def read_only_grafana_tool_call(
    tool_name: str,
    arguments: Mapping[str, object],
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    """Выполнить одну явно разрешённую Grafana read-only операцию."""

    return asyncio.run(
        _read_only_grafana_tool_call_async(
            tool_name, arguments, repository_root=repository_root.resolve()
        )
    )


def direct_grafana_status(repository_root: Path = REPOSITORY_ROOT) -> dict[str, object]:
    """Вернуть тот же typed status, что использует общий registry."""

    from azurpilot.integrations import IntegrationService

    result = IntegrationService().status_one("grafana", repository_root)
    details = result.details
    if details is None:
        return {"status": "unknown", "reason_code": "GRAFANA_STATUS_NOT_OBSERVABLE"}
    record = details.integrations[0]
    return record.model_dump(mode="json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Прямой read-only probe Grafana MCP.")
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--probe", action="store_true", help="Проверить один read-only вызов datasource.")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.probe:
        print(json.dumps(direct_grafana_status(arguments.repository_root), ensure_ascii=False, sort_keys=True))
        return 0
    try:
        payload = read_only_grafana_tool_call(
            "list_datasources", {}, repository_root=arguments.repository_root
        )
    except ObservabilityMcpError as error:
        print(json.dumps({"status": "unavailable", "reason_code": error.code}, ensure_ascii=False))
        return 2
    if payload.get("isError") is True:
        print(
            json.dumps(
                {"status": "unavailable", "reason_code": "GRAFANA_READ_ONLY_CALL_ERROR"},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps({"status": "ready", "tool": "list_datasources", "result": payload}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GRAFANA_BLOCKED_TOOLS",
    "GRAFANA_READ_ONLY_TOOLS",
    "MAX_ARGUMENT_BYTES",
    "MAX_RESULT_ITEMS",
    "MAX_RESULT_TEXT",
    "GRAFANA_DIRECT_TIMEOUT_SECONDS",
    "ObservabilityMcpError",
    "read_only_grafana_tool_call",
    "_read_only_grafana_tool_call_async",
    "direct_grafana_status",
    "main",
]
