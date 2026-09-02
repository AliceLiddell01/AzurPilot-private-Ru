"""Standalone read-only Game MCP surface для игровых клиентов."""

from module.game_mcp.adapter import (
    GAME_MCP_TOOL_NAMES,
    GameMcpAdapter,
    GameMcpResponse,
)
from module.game_mcp.contract import (
    CONTRACT_SCHEMA_VERSION,
    GAME_MCP_API_VERSION,
    contract_payload,
    contract_result,
)

__all__ = (
    "CONTRACT_SCHEMA_VERSION",
    "GAME_MCP_API_VERSION",
    "GAME_MCP_TOOL_NAMES",
    "GameMcpAdapter",
    "GameMcpResponse",
    "contract_payload",
    "contract_result",
)
