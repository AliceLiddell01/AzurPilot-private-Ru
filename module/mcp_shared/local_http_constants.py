"""Канонические адреса конечных точек loopback MCP Dev/Game проекта."""

from types import MappingProxyType

LOCAL_HTTP_ENDPOINTS = MappingProxyType(
    {
        "azurpilot-dev": "http://127.0.0.1:8775/mcp",
        "azurpilot-game": "http://127.0.0.1:8776/mcp",
    }
)

__all__ = ("LOCAL_HTTP_ENDPOINTS",)
