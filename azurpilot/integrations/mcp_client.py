"""Ограниченный MCP transport для типизированных read-only адаптеров."""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import Mapping
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
    expected_tools: frozenset[str] = frozenset()
    tempo_tools: frozenset[str] = frozenset()
    missing_tempo_reason_code: str = "MCP_REQUIRED_TEMPO_TOOL_MISSING"
    toolset_drift_reason_code: str = "MCP_TOOLSET_DRIFT"

    def __post_init__(self) -> None:
        if not _IDENTIFIER_RE.fullmatch(self.probe_tool):
            raise ValueError("probe_tool имеет небезопасное имя")
        if self.probe_tool not in self.required_tools:
            raise ValueError("probe_tool должен входить в required_tools")
        if self.probe_tool in self.blocked_tools:
            raise ValueError("probe_tool не может быть write tool")
        if not self.tempo_tools.issubset(self.required_tools):
            raise ValueError("tempo_tools должны входить в required_tools")
        if self.expected_tools and not self.required_tools.issubset(self.expected_tools):
            raise ValueError("required_tools должны входить в expected_tools")


@dataclass(frozen=True, slots=True)
class McpProbeResult:
    state: IntegrationState
    reason_code: str
    tool_count: int | None = None
    selected_tool: str | None = None
    authenticated: bool | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FreshMcpClientPlan:
    """Bounded read-only contract for one independent MCP client session."""

    call_plan: McpCallPlan
    contract_tool: str
    expected_contract: Mapping[str, object]
    required_read_only_calls: tuple[tuple[str, dict[str, object]], ...] = ()

    def __post_init__(self) -> None:
        if not _IDENTIFIER_RE.fullmatch(self.contract_tool):
            raise ValueError("contract_tool имеет небезопасное имя")
        if self.call_plan.probe_tool != self.contract_tool:
            raise ValueError("contract_tool должен быть probe_tool call plan")
        if not isinstance(self.expected_contract, Mapping) or not self.expected_contract:
            raise ValueError("expected_contract должен быть непустым mapping")
        for tool_name, arguments in self.required_read_only_calls:
            if not _IDENTIFIER_RE.fullmatch(tool_name):
                raise ValueError("required read-only tool имеет небезопасное имя")
            if tool_name not in self.call_plan.required_tools:
                raise ValueError("required read-only tool отсутствует в required_tools")
            if tool_name in self.call_plan.blocked_tools:
                raise ValueError("required read-only tool не может быть write tool")
            if not isinstance(arguments, dict):
                raise TypeError("arguments read-only tool должны быть dict")


@dataclass(frozen=True, slots=True)
class FreshMcpClientResult:
    """Bounded evidence независимой SDK-сессии без raw MCP payload."""

    state: IntegrationState
    reason_code: str
    initialized: bool = False
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    source_revision: str | None = None
    tool_count: int | None = None
    tool_catalog_sha256: str | None = None
    capability_catalog_sha256: str | None = None
    contract_revision: str | None = None
    called_tools: tuple[str, ...] = ()
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


def validate_tool_catalog(
    plan: McpCallPlan, names: tuple[str, ...]
) -> tuple[str, str] | None:
    """Проверить negotiated catalog до любого вызова read-only tool."""

    actual = set(names)
    missing = set(plan.expected_tools).difference(actual)
    unexpected = actual.difference(plan.expected_tools) if plan.expected_tools else set()
    if unexpected:
        return plan.toolset_drift_reason_code, "toolset_drift"
    if missing:
        if plan.tempo_tools.intersection(missing):
            return plan.missing_tempo_reason_code, "tempo_tools_missing"
        return plan.toolset_drift_reason_code, "toolset_drift"
    required_missing = plan.required_tools.difference(actual)
    if required_missing:
        return "MCP_REQUIRED_READ_ONLY_TOOL_MISSING", "required_tool_missing"
    return None


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


def _structured_payload(result: object) -> Mapping[str, object] | None:
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    return structured if isinstance(structured, Mapping) else None


async def _accept_fresh_session(
    session: object,
    *,
    plan: FreshMcpClientPlan,
    timeout_seconds: float,
) -> FreshMcpClientResult:
    """Провести initialize/catalog/contract/read-only checks в одной новой session."""

    initialized = await asyncio.wait_for(session.initialize(), timeout=timeout_seconds)  # type: ignore[attr-defined]
    initialized_info = getattr(initialized, "server_info", None)
    server_name = getattr(initialized_info, "name", None)
    server_version = getattr(initialized_info, "version", None)
    protocol_version = getattr(initialized, "protocol_version", None)
    if protocol_version is None:
        protocol_version = getattr(session, "protocol_version", None)

    listed = await asyncio.wait_for(session.list_tools(), timeout=timeout_seconds)  # type: ignore[attr-defined]
    items = getattr(listed, "tools", None)
    names = _tool_names(items)
    if names is None:
        return FreshMcpClientResult(
            IntegrationState.INCOMPATIBLE,
            "MCP_FRESH_CLIENT_TOOL_CATALOG_INVALID",
            initialized=True,
            protocol_version=protocol_version,
        )
    catalog_error = validate_tool_catalog(plan.call_plan, names)
    if catalog_error is not None:
        reason_code, diagnostic = catalog_error
        return FreshMcpClientResult(
            IntegrationState.INCOMPATIBLE,
            f"MCP_FRESH_CLIENT_{reason_code}",
            initialized=True,
            protocol_version=protocol_version,
            server_name=server_name,
            server_version=server_version,
            tool_count=len(names),
            diagnostics=(diagnostic,),
        )

    contract_result = await asyncio.wait_for(
        session.call_tool(plan.contract_tool, {}),  # type: ignore[attr-defined]
        timeout=timeout_seconds,
    )
    if _result_has_error(contract_result):
        return FreshMcpClientResult(
            IntegrationState.UNAVAILABLE,
            "MCP_FRESH_CLIENT_CONTRACT_CALL_FAILED",
            initialized=True,
            protocol_version=protocol_version,
            server_name=server_name,
            server_version=server_version,
            tool_count=len(names),
            called_tools=(plan.contract_tool,),
        )
    structured = _structured_payload(contract_result)
    details = structured.get("details") if structured is not None else None
    contract = details.get("contract") if isinstance(details, Mapping) else None
    if not isinstance(contract, Mapping) or structured.get("ok") is not True:
        return FreshMcpClientResult(
            IntegrationState.INCOMPATIBLE,
            "MCP_FRESH_CLIENT_CONTRACT_PAYLOAD_INVALID",
            initialized=True,
            protocol_version=protocol_version,
            server_name=server_name,
            server_version=server_version,
            tool_count=len(names),
            called_tools=(plan.contract_tool,),
        )

    try:
        from module.mcp_shared.catalog import tool_catalog_sha256_from_tools

        catalog_hash = tool_catalog_sha256_from_tools(items)
    except (TypeError, ValueError):
        return FreshMcpClientResult(
            IntegrationState.INCOMPATIBLE,
            "MCP_FRESH_CLIENT_TOOL_CATALOG_INVALID",
            initialized=True,
            protocol_version=protocol_version,
            server_name=server_name,
            server_version=server_version,
            tool_count=len(names),
            called_tools=(plan.contract_tool,),
        )

    mismatches: list[str] = []
    for field, expected in plan.expected_contract.items():
        if field == "source_revision" and expected in (None, "unknown"):
            continue
        if contract.get(field) != expected:
            mismatches.append(f"contract.{field}")
    if contract.get("tool_count") != len(names):
        mismatches.append("contract.tool_count")
    if contract.get("tool_catalog_sha256") != catalog_hash:
        mismatches.append("contract.tool_catalog_sha256")
    if isinstance(server_name, str) and contract.get("server_name") != server_name:
        mismatches.append("server_info.name")
    if isinstance(server_version, str) and contract.get("server_version") != server_version:
        mismatches.append("server_info.version")
    if mismatches:
        return FreshMcpClientResult(
            IntegrationState.INCOMPATIBLE,
            "MCP_FRESH_CLIENT_CONTRACT_DRIFT",
            initialized=True,
            protocol_version=protocol_version,
            server_name=server_name,
            server_version=server_version,
            source_revision=(
                contract.get("source_revision")
                if isinstance(contract.get("source_revision"), str)
                else None
            ),
            tool_count=len(names),
            tool_catalog_sha256=(
                contract.get("tool_catalog_sha256")
                if isinstance(contract.get("tool_catalog_sha256"), str)
                else None
            ),
            capability_catalog_sha256=(
                contract.get("capability_catalog_sha256")
                if isinstance(contract.get("capability_catalog_sha256"), str)
                else None
            ),
            contract_revision=(
                contract.get("contract_revision")
                if isinstance(contract.get("contract_revision"), str)
                else None
            ),
            called_tools=(plan.contract_tool,),
            diagnostics=tuple(mismatches[:16]),
        )

    called_tools = [plan.contract_tool]
    for tool_name, arguments in plan.required_read_only_calls:
        result = await asyncio.wait_for(
            session.call_tool(tool_name, dict(arguments)),  # type: ignore[attr-defined]
            timeout=timeout_seconds,
        )
        payload = _structured_payload(result)
        if _result_has_error(result) or payload is None or payload.get("ok") is not True:
            return FreshMcpClientResult(
                IntegrationState.UNAVAILABLE,
                "MCP_FRESH_CLIENT_READ_ONLY_CALL_FAILED",
                initialized=True,
                protocol_version=protocol_version,
                server_name=server_name,
                server_version=server_version,
                source_revision=(
                    contract.get("source_revision")
                    if isinstance(contract.get("source_revision"), str)
                    else None
                ),
                tool_count=len(names),
                tool_catalog_sha256=catalog_hash,
                capability_catalog_sha256=(
                    contract.get("capability_catalog_sha256")
                    if isinstance(contract.get("capability_catalog_sha256"), str)
                    else None
                ),
                contract_revision=(
                    contract.get("contract_revision")
                    if isinstance(contract.get("contract_revision"), str)
                    else None
                ),
                called_tools=(*called_tools, tool_name),
                diagnostics=(tool_name,),
            )
        called_tools.append(tool_name)

    return FreshMcpClientResult(
        IntegrationState.READY,
        "MCP_FRESH_CLIENT_ACCEPTANCE_READY",
        initialized=True,
        protocol_version=protocol_version,
        server_name=server_name or (
            contract.get("server_name")
            if isinstance(contract.get("server_name"), str)
            else None
        ),
        server_version=server_version or (
            contract.get("server_version")
            if isinstance(contract.get("server_version"), str)
            else None
        ),
        source_revision=(
            contract.get("source_revision")
            if isinstance(contract.get("source_revision"), str)
            else None
        ),
        tool_count=len(names),
        tool_catalog_sha256=catalog_hash,
        capability_catalog_sha256=(
            contract.get("capability_catalog_sha256")
            if isinstance(contract.get("capability_catalog_sha256"), str)
            else None
        ),
        contract_revision=(
            contract.get("contract_revision")
            if isinstance(contract.get("contract_revision"), str)
            else None
        ),
        called_tools=tuple(called_tools),
    )


async def accept_fresh_stdio(
    *,
    command: str,
    args: tuple[str, ...],
    cwd: object,
    environment: dict[str, str],
    plan: FreshMcpClientPlan,
    timeout_seconds: float,
) -> FreshMcpClientResult:
    """Создать независимый stdio SDK client и вернуть bounded acceptance evidence."""

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
            return await _accept_fresh_session(
                session,
                plan=plan,
                timeout_seconds=timeout_seconds,
            )
    except TimeoutError:
        return FreshMcpClientResult(IntegrationState.UNAVAILABLE, "MCP_FRESH_CLIENT_TIMEOUT")
    except Exception as error:  # noqa: BLE001 - boundary exposes only type.
        return FreshMcpClientResult(
            IntegrationState.UNAVAILABLE,
            "MCP_FRESH_CLIENT_FAILED",
            diagnostics=(_safe_type_name(error),),
        )


def _call_error_state(
    result: object,
    *,
    credential_configured: bool,
    credential_required: bool = False,
) -> tuple[IntegrationState, str, bool | None]:
    if not _result_has_error(result):
        if _result_has_content(result):
            return IntegrationState.READY, "MCP_READ_ONLY_PROBE_READY", bool(
                credential_configured
            )
        return IntegrationState.DEGRADED, "MCP_READ_ONLY_RESULT_NOT_OBSERVABLE", None
    return (
        IntegrationState.UNAUTHENTICATED
        if credential_required and not credential_configured
        else IntegrationState.UNAVAILABLE,
        "MCP_READ_ONLY_AUTH_REQUIRED"
        if credential_required and not credential_configured
        else "MCP_READ_ONLY_PROBE_ERROR",
        False if credential_required and not credential_configured else None,
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
    credential_required: bool = False,
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
                catalog_error = validate_tool_catalog(plan, names)
                if catalog_error is not None:
                    reason_code, diagnostic = catalog_error
                    return McpProbeResult(
                        IntegrationState.INCOMPATIBLE,
                        reason_code,
                        tool_count=len(names),
                        diagnostics=(diagnostic,),
                    )
                result = await asyncio.wait_for(
                    session.call_tool(plan.probe_tool, dict(plan.arguments)),
                    timeout=timeout_seconds,
                )
                state, reason, authenticated = _call_error_state(
                    result,
                    credential_configured=credential_configured,
                    credential_required=credential_required,
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
    credential_required: bool = False,
) -> McpProbeResult:
    """Проверить конкретный streamable HTTP server с фиксированным read call."""

    try:
        validate_endpoint(endpoint)
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

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
            catalog_error = validate_tool_catalog(plan, names)
            if catalog_error is not None:
                reason_code, diagnostic = catalog_error
                return McpProbeResult(
                    IntegrationState.INCOMPATIBLE,
                    reason_code,
                    tool_count=len(names),
                    diagnostics=(diagnostic,),
                )
            result = await asyncio.wait_for(
                session.call_tool(plan.probe_tool, dict(plan.arguments)),
                timeout=timeout_seconds,
            )
            state, reason, authenticated = _call_error_state(
                result,
                credential_configured=credential_configured,
                credential_required=credential_required,
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
    "FreshMcpClientPlan",
    "FreshMcpClientResult",
    "McpCallPlan",
    "McpProbeResult",
    "accept_fresh_stdio",
    "probe_http",
    "probe_stdio",
    "validate_endpoint",
    "validate_tool_catalog",
]
