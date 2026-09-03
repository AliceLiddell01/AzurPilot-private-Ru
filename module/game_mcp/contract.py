"""Стабильный контракт standalone Game MCP read/control plane."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from types import MappingProxyType

CONTRACT_SCHEMA_VERSION = 1
GAME_MCP_API_VERSION = 1
PRODUCT_FAMILY = "AzurPilot"
GAME_MCP_READ_SCOPE = "azurpilot:game.read"
GAME_MCP_CONTROL_SCOPE = "azurpilot:game.control"
GAME_MCP_SCOPES = (GAME_MCP_READ_SCOPE, GAME_MCP_CONTROL_SCOPE)
GAME_MCP_READ_TOOL_NAMES = (
    "game_get_contract",
    "game_list_profiles",
    "game_get_profile_status",
    "game_get_resources",
    "game_get_current_task",
    "game_get_scheduler_queue",
    "game_list_tasks",
    "game_get_task_help",
    "game_get_fleet_state",
    "game_get_morale",
    "game_get_config",
    "game_get_recent_logs",
    "game_get_screenshot",
)
GAME_MCP_CONTROL_TOOL_NAMES = (
    "game_start_profile",
    "game_stop_profile",
    "game_trigger_task",
    "game_clear_scheduler_queue",
    "game_update_config",
    "game_restart_emulator",
    "game_restart_runtime",
    "game_login_runtime",
    "game_restart_adb",
)
GAME_MCP_TOOL_NAMES = GAME_MCP_READ_TOOL_NAMES + GAME_MCP_CONTROL_TOOL_NAMES
GAME_MCP_TOOL_REQUIRED_SCOPES = MappingProxyType(
    {
        **{name: GAME_MCP_READ_SCOPE for name in GAME_MCP_READ_TOOL_NAMES},
        **{name: GAME_MCP_CONTROL_SCOPE for name in GAME_MCP_CONTROL_TOOL_NAMES},
    }
)

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


def tool_catalog_sha256(tool_names: Iterable[str] = GAME_MCP_TOOL_NAMES) -> str:
    """Хешировать канонический каталог имён инструментов.

    Каноническая форма не зависит от порядка публикации: имена сортируются
    лексикографически, соединяются одним переводом строки без завершающего
    перевода строки и кодируются как UTF-8 перед расчётом SHA-256.
    """

    names = tuple(tool_names)
    if any(
        not isinstance(name, str) or not name or name != name.strip() for name in names
    ) or len(set(names)) != len(names):
        raise ValueError(
            "Каталог инструментов содержит некорректные или повторные имена."
        )
    canonical = "\n".join(sorted(names)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def contract_payload() -> dict[str, object]:
    """Вернуть только стабильные поля Game MCP контракта."""

    return {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "product_family": PRODUCT_FAMILY,
        "game_mcp_api_version": GAME_MCP_API_VERSION,
        "tool_count": len(GAME_MCP_TOOL_NAMES),
        "tool_catalog_sha256": tool_catalog_sha256(),
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


def contract_result(
    *, request_context: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Вернуть безопасный результат инструмента контракта."""

    details: dict[str, object] = {"contract": contract_payload()}
    if request_context is not None:
        details["request_context"] = dict(request_context)
    return {
        "ok": True,
        "code": "GAME_MCP_CONTRACT_READY",
        "message": "Контракт AzurPilot Game MCP готов",
        "state": "ready",
        "details": details,
    }


__all__ = (
    "CONTRACT_SCHEMA_VERSION",
    "GAME_MCP_API_VERSION",
    "GAME_MCP_CAPABILITY_FAMILIES",
    "GAME_MCP_CONTROL_SCOPE",
    "GAME_MCP_CONTROL_TOOL_NAMES",
    "GAME_MCP_FEATURE_FLAGS",
    "GAME_MCP_NO_ARGUMENT_TOOLS",
    "GAME_MCP_READ_SCOPE",
    "GAME_MCP_READ_TOOL_NAMES",
    "GAME_MCP_RESULT_STATES",
    "GAME_MCP_SCOPES",
    "GAME_MCP_TOOL_NAMES",
    "GAME_MCP_TOOL_REQUIRED_SCOPES",
    "PRODUCT_FAMILY",
    "contract_payload",
    "contract_result",
    "tool_catalog_sha256",
)
