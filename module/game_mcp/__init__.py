"""Standalone read/control Game MCP surface для игровых клиентов."""

from module.game_mcp.adapter import (
    GAME_MCP_CONTROL_TOOL_NAMES,
    GAME_MCP_READ_TOOL_NAMES,
    GAME_MCP_TOOL_NAMES,
    GameMcpAdapter,
    GameMcpResponse,
)
from module.game_mcp.contract import (
    CONTRACT_SCHEMA_VERSION,
    GAME_MCP_API_VERSION,
    GAME_MCP_CONTROL_SCOPE,
    GAME_MCP_READ_SCOPE,
    GAME_MCP_SCOPES,
    contract_payload,
    contract_result,
)

__all__ = (
    "CONTRACT_SCHEMA_VERSION",
    "GAME_MCP_API_VERSION",
    "GAME_MCP_CONTROL_SCOPE",
    "GAME_MCP_CONTROL_TOOL_NAMES",
    "GAME_MCP_READ_SCOPE",
    "GAME_MCP_READ_TOOL_NAMES",
    "GAME_MCP_SCOPES",
    "GAME_MCP_TOOL_NAMES",
    "GameMcpAdapter",
    "GameMcpResponse",
    "contract_payload",
    "contract_result",
)
