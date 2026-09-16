"""Ограниченный MCP transport для типизированных read-only адаптеров."""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit

from .contracts import IntegrationState

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_MAX_TOOLS = 256


def validate_endpoint(value: str, *, allow_http: bool = False) -> str:
    """Проверить endpoint без допуска URL credentials или control characters."""

    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("endpoint имеет неверный формат") from exc
    schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in schemes or not parsed.hostname:
        raise ValueError("endpoint должен использовать разрешённую схему")
    if parsed.username or parsed.password or any(ord(char) < 32 for char in value):
        raise ValueError("endpoint не должен содержать credentials или управляющие символы")
    if len(value) > 512:
        raise ValueError("endpoint превышает ограниченный размер")
    return value


@dataclass(frozen=True, slots=True)
class McpCallPlan:
    """План одной заранее известной read-only операции адаптера."""

    required_tools: frozenset[str]
    probe_tool: str
    arguments: dict[str, object]
    blocked_tools: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not _IDENTIFIER_RE.fullmatch(self.probe_tool):
            raise ValueError("probe_tool имеет небезопасное имя")
        if self.probe_tool not in self.required_tools:
            raise ValueError("probe_tool должен входить в required_tools")
        if self.probe_tool in self.blocked_tools:
            raise ValueError("probe_tool не может быть write tool")


@dataclass(frozen=True, slots=True)
class McpProbeResult:
    state: IntegrationState
    reason_code: str
    tool_count: int | None = None
    selected_tool: str | None = None
    authenticated: bool | None = None
    diagnostics: tuple[str, ...] = ()


def _safe_type_name(error: BaseException) -> str:
    name = type(error).__name__
    return name if _IDENTIFIER_RE.fullmatch(name) else "UnknownError"


def _tool_names(items: object) -> tuple[str, ...] | None:
    if not isinstance(items, list) or len(items) > _MAX_TOOLS:
        return None
    names: list[str] = []
    for item in items:
        name = getattr(item, "name", None)
        if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
            return None
        names.append(name)
    if len(names) != len(set(names)):
        return None
    return tuple(names)


def _result_has_error(result: object) -> bool:
    return bool(
        getattr(result, "is_error", False)
        or getattr(result, "isError", False)
    )


def _result_has_content(result: object) -> bool:
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return True
    content = getattr(result, "content", None)
    return isinstance(content, list) and bool(content)


def _call_error_state(
    result: object, *, credential_configured: bool
) -> tuple[IntegrationState, str, bool | None]:
    if not _result_has_error(result):
        if _result_has_content(result):
            return IntegrationState.READY, "MCP_READ_ONLY_PROBE_READY", bool(
                credential_configured
            )
        return IntegrationState.DEGRADED, "MCP_READ_ONLY_RESULT_NOT_OBSERVABLE", None
    return (
        IntegrationState.UNAUTHENTICATED
        if not credential_configured
        else IntegrationState.UNAVAILABLE,
        "MCP_READ_ONLY_AUTH_REQUIRED"
        if not credential_configured
        else "MCP_READ_ONLY_PROBE_ERROR",
        False if not credential_configured else None,
    )


async def probe_stdio(
    *,
    command: str,
    args: tuple[str, ...],
    cwd: object,
    environment: dict[str, str],
    plan: McpCallPlan,
    timeout_seconds: float,
    credential_configured: bool,
) -> McpProbeResult:
    """Проверить конкретный stdio server через SDK без generic tool dispatch."""

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    parameters = StdioServerParameters(
        command=command,
        args=list(args),
        cwd=cwd,
        env=environment,
    )
    try:
        async with (
            stdio_client(parameters, errlog=subprocess.DEVNULL) as (read_stream, write_stream),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timeout_seconds,
            ) as session,
        ):
                await asyncio.wait_for(session.initialize(), timeout=timeout_seconds)
                listed = await asyncio.wait_for(session.list_tools(), timeout=timeout_seconds)
                names = _tool_names(getattr(listed, "tools", None))
                if names is None:
                    return McpProbeResult(
                        IntegrationState.INCOMPATIBLE,
                        "MCP_TOOL_CATALOG_INVALID",
                    )
                missing = plan.required_tools.difference(names)
                if missing:
                    return McpProbeResult(
                        IntegrationState.INCOMPATIBLE,
                        "MCP_REQUIRED_READ_ONLY_TOOL_MISSING",
                        tool_count=len(names),
                        diagnostics=("required_tool_missing",),
                    )
                result = await asyncio.wait_for(
                    session.call_tool(plan.probe_tool, dict(plan.arguments)),
                    timeout=timeout_seconds,
                )
                state, reason, authenticated = _call_error_state(
                    result, credential_configured=credential_configured
                )
                return McpProbeResult(
                    state,
                    reason,
                    tool_count=len(names),
                    selected_tool=plan.probe_tool,
                    authenticated=authenticated,
                    diagnostics=(
                        ("write_tools_blocked",)
                        if plan.blocked_tools.intersection(names)
                        else ()
                    ),
                )
    except TimeoutError:
        return McpProbeResult(IntegrationState.UNAVAILABLE, "MCP_PROBE_TIMEOUT")
    except Exception as error:  # noqa: BLE001 - boundary exposes only type.
        return McpProbeResult(
            IntegrationState.UNAVAILABLE,
            "MCP_PROBE_FAILED",
            diagnostics=(_safe_type_name(error),),
        )


async def probe_http(
    *,
    endpoint: str,
    headers: dict[str, str],
    plan: McpCallPlan,
    timeout_seconds: float,
    credential_configured: bool,
) -> McpProbeResult:
    """Проверить конкретный streamable HTTP server с фиксированным read call."""

    validate_endpoint(endpoint)
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    try:
        async with httpx2.AsyncClient(
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as http_client, streamable_http_client(
            endpoint, http_client=http_client
        ) as (read_stream, write_stream), ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timeout_seconds,
        ) as session:
            await asyncio.wait_for(session.initialize(), timeout=timeout_seconds)
            listed = await asyncio.wait_for(
                session.list_tools(), timeout=timeout_seconds
            )
            names = _tool_names(getattr(listed, "tools", None))
            if names is None:
                return McpProbeResult(
                    IntegrationState.INCOMPATIBLE,
                    "MCP_TOOL_CATALOG_INVALID",
                )
            missing = plan.required_tools.difference(names)
            if missing:
                return McpProbeResult(
                    IntegrationState.INCOMPATIBLE,
                    "MCP_REQUIRED_READ_ONLY_TOOL_MISSING",
                    tool_count=len(names),
                    diagnostics=("required_tool_missing",),
                )
            result = await asyncio.wait_for(
                session.call_tool(plan.probe_tool, dict(plan.arguments)),
                timeout=timeout_seconds,
            )
            state, reason, authenticated = _call_error_state(
                result, credential_configured=credential_configured
            )
            return McpProbeResult(
                state,
                reason,
                tool_count=len(names),
                selected_tool=plan.probe_tool,
                authenticated=authenticated,
                diagnostics=(
                    ("write_tools_blocked",)
                    if plan.blocked_tools.intersection(names)
                    else ()
                ),
            )
    except TimeoutError:
        return McpProbeResult(IntegrationState.UNAVAILABLE, "MCP_PROBE_TIMEOUT")
    except Exception as error:  # noqa: BLE001 - boundary exposes only type.
        return McpProbeResult(
            IntegrationState.UNAVAILABLE,
            "MCP_HTTP_PROBE_FAILED",
            diagnostics=(_safe_type_name(error),),
        )


__all__ = [
    "McpCallPlan",
    "McpProbeResult",
    "probe_http",
    "probe_stdio",
    "validate_endpoint",
]
