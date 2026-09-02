"""Стабильный контракт standalone Game MCP read/control plane."""

from __future__ import annotations

from types import MappingProxyType

CONTRACT_SCHEMA_VERSION = 1
GAME_MCP_API_VERSION = 1
PRODUCT_FAMILY = "AzurPilot"
GAME_MCP_READ_SCOPE = "azurpilot:game.read"
GAME_MCP_CONTROL_SCOPE = "azurpilot:game.control"
GAME_MCP_SCOPES = (GAME_MCP_READ_SCOPE, GAME_MCP_CONTROL_SCOPE)

GAME_MCP_FEATURE_FLAGS = MappingProxyType(
    {
        "read_only": False,
        "stateless": True,
        "multi_profile": True,
        "control_plane": True,
        "lifecycle_control": True,
        "task_control": True,
        "scheduler_mutation": True,
        "configuration_write": True,
        "emulator_control": True,
        "adb_control": True,
        "fleet_state": True,
        "morale": True,
        "resources": True,
        "configuration_read": True,
        "runtime_logs": True,
        "screenshots": True,
        "remote_streamable_http": True,
        "no_database_diagnostics": True,
        "no_arbitrary_filesystem_read": True,
        "no_arbitrary_sql": True,
        "no_dev_runtime": True,
        "no_generic_action_api": True,
    }
)
GAME_MCP_CAPABILITY_FAMILIES = (
    "profiles",
    "runtime",
    "resources",
    "tasks",
    "configuration",
    "logs",
    "screenshots",
    "control",
    "lifecycle_control",
    "task_control",
    "scheduler_mutation",
    "configuration_write",
    "emulator_control",
    "adb_control",
    "fleet_state",
    "morale",
)
GAME_MCP_NO_ARGUMENT_TOOLS = frozenset(
    {"game_get_contract", "game_list_profiles", "game_list_tasks"}
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
    "scheduled",
)


def contract_payload() -> dict[str, object]:
    """Вернуть только стабильные поля Game MCP контракта."""

    return {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "product_family": PRODUCT_FAMILY,
        "game_mcp_api_version": GAME_MCP_API_VERSION,
        "authorization_scopes": list(GAME_MCP_SCOPES),
        "feature_flags": dict(GAME_MCP_FEATURE_FLAGS),
        "capability_families": list(GAME_MCP_CAPABILITY_FAMILIES),
        "result_states": list(GAME_MCP_RESULT_STATES),
        "read_only_guarantees": [
            "no_database_diagnostics",
            "no_arbitrary_filesystem_read",
            "no_arbitrary_sql",
            "no_dev_runtime",
            "no_generic_action_api",
        ],
        "control_guarantees": [
            "explicit_profile_for_mutations",
            "no_automatic_mutation_retry",
            "sensitive_config_write_denied",
            "postcondition_required",
        ],
    }


def contract_result() -> dict[str, object]:
    """Вернуть безопасный результат инструмента контракта."""

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
    "GAME_MCP_CONTROL_SCOPE",
    "GAME_MCP_FEATURE_FLAGS",
    "GAME_MCP_NO_ARGUMENT_TOOLS",
    "GAME_MCP_READ_SCOPE",
    "GAME_MCP_RESULT_STATES",
    "GAME_MCP_SCOPES",
    "PRODUCT_FAMILY",
    "contract_payload",
    "contract_result",
)
