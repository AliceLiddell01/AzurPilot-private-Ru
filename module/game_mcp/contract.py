"""Стабильный контракт standalone Game MCP read plane."""

from __future__ import annotations

CONTRACT_SCHEMA_VERSION = 1
GAME_MCP_API_VERSION = 1
PRODUCT_FAMILY = "AzurPilot"

GAME_MCP_FEATURE_FLAGS = {
    "read_only": True,
    "stateless": True,
    "multi_profile": True,
    "fleet_state": True,
    "morale": True,
    "resources": True,
    "configuration_read": True,
    "runtime_logs": True,
    "screenshots": True,
    "remote_streamable_http": True,
}
GAME_MCP_CAPABILITY_FAMILIES = (
    "profiles",
    "runtime",
    "resources",
    "tasks",
    "configuration",
    "logs",
    "screenshots",
    "fleet_state",
    "morale",
)
GAME_MCP_RESULT_STATES = (
    "ready",
    "running",
    "stopped",
    "warning",
    "updating",
    "partial",
    "unknown",
    "not_running",
    "unavailable",
    "failed",
)


def contract_payload() -> dict[str, object]:
    """Вернуть только стабильные поля Game MCP контракта."""

    return {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "product_family": PRODUCT_FAMILY,
        "game_mcp_api_version": GAME_MCP_API_VERSION,
        "feature_flags": dict(GAME_MCP_FEATURE_FLAGS),
        "capability_families": list(GAME_MCP_CAPABILITY_FAMILIES),
        "result_states": list(GAME_MCP_RESULT_STATES),
        "read_only_guarantees": [
            "no_lifecycle_control",
            "no_config_mutation",
            "no_task_trigger",
            "no_database_diagnostics",
            "no_arbitrary_filesystem_read",
            "no_arbitrary_sql",
        ],
    }


def contract_result() -> dict[str, object]:
    """Вернуть безопасный результат read-only инструмента контракта."""

    return {
        "ok": True,
        "code": "GAME_MCP_CONTRACT_READY",
        "message": "Контракт AzurPilot Game MCP готов",
        "state": "ready",
        "details": {"contract": contract_payload()},
    }


__all__ = (
    "CONTRACT_SCHEMA_VERSION",
    "GAME_MCP_API_VERSION",
    "GAME_MCP_CAPABILITY_FAMILIES",
    "GAME_MCP_FEATURE_FLAGS",
    "GAME_MCP_RESULT_STATES",
    "PRODUCT_FAMILY",
    "contract_payload",
    "contract_result",
)
