"""Локальный stdio-сервер standalone Game MCP read/control plane."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
from contextlib import redirect_stdout
from threading import Lock
from typing import Any

import anyio
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool,
    ToolAnnotations,
)

from module.application.game_validation import INVALID_NAME_CHARS, MAX_NAME_LENGTH
from module.formation.model import SUPPORTED_SURFACE_FLEET_INDICES
from module.game_mcp.adapter import (
    GAME_MCP_TOOL_NAMES,
    GAME_MCP_TOOL_REQUIRED_SCOPES,
    GameMcpAdapter,
    GameMcpResponse,
)
from module.game_mcp.contract import (
    CONTRACT_SCHEMA_VERSION,
    GAME_MCP_API_VERSION,
    GAME_MCP_CONTROL_SCOPE,
    GAME_MCP_NO_ARGUMENT_TOOLS,
    GAME_MCP_READ_SCOPE,
    GAME_MCP_SCOPES,
)
from module.mcp_shared.auth import current_access_token

SERVER_NAME = "azurpilot-game"
SERVER_VERSION = str(GAME_MCP_API_VERSION)
GAME_MCP_COMMAND = "uv"
GAME_MCP_ARGS = ("run", "--locked", "--no-sync", "python", "-m", "module.game_mcp")
GAME_MCP_REQUIRED_SCOPE = GAME_MCP_READ_SCOPE
# Legacy-граф использует builtins.print и может инициализировать Rich handler
# на stdout. Глобальная блокировка нужна, чтобы такой перехват не пересекался
# с другим MCP Server в одном процессе; сам adapter дополнительно сериализует
# backend-вызовы, поэтому отменённый worker не может пересечься с новым чтением.
_LEGACY_STDOUT_LOCK = Lock()

_SELECTOR_FORBIDDEN = "".join(
    re.escape(char)
    for char in sorted(INVALID_NAME_CHARS - {"\x00"})
) + r"\x00-\x1f\x7f"
_SELECTOR_CHARACTER = rf"[^{_SELECTOR_FORBIDDEN}]"
_SELECTOR_EDGE = rf"[^\s{_SELECTOR_FORBIDDEN}]"
_PROFILE_PATTERN = (
    rf"^{_SELECTOR_EDGE}"
    rf"(?:{_SELECTOR_CHARACTER}{{0,{MAX_NAME_LENGTH - 2}}}"
    rf"{_SELECTOR_EDGE})?$"
)
_PROFILE_INPUT = {
    "type": "object",
    "properties": {
        "profile": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_NAME_LENGTH,
            "pattern": _PROFILE_PATTERN,
        }
    },
    "required": ["profile"],
    "additionalProperties": False,
}
_TASK_INPUT = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_NAME_LENGTH,
            "pattern": _PROFILE_PATTERN,
        }
    },
    "required": ["task"],
    "additionalProperties": False,
}
_FLEET_SELECTION_INPUT = {
    "type": "object",
    "properties": {
        "profile": _PROFILE_INPUT["properties"]["profile"],
        "fleet_indices": {
            "type": "array",
            "items": {
                "type": "integer",
                "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
            },
            "minItems": 1,
            "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
            "uniqueItems": True,
        },
    },
    "required": ["profile", "fleet_indices"],
    "additionalProperties": False,
}
_CONFIG_INPUT = {
    "type": "object",
    "properties": {
        "profile": _PROFILE_INPUT["properties"]["profile"],
        "task": _TASK_INPUT["properties"]["task"],
    },
    "required": ["profile"],
    "additionalProperties": False,
}
_LOG_INPUT = {
    "type": "object",
    "properties": {
        "profile": _PROFILE_INPUT["properties"]["profile"],
        "lines": {"type": "integer", "minimum": 0, "maximum": 200, "default": 50},
    },
    "required": ["profile"],
    "additionalProperties": False,
}
_CONFIG_VALUE_DEFS = {
    "configValue": {
        "anyOf": [
            {"type": "null"},
            {"type": "boolean"},
            {
                "type": "integer",
                "minimum": -10**12,
                "maximum": 10**12,
            },
            {
                "type": "number",
                "minimum": -10**12,
                "maximum": 10**12,
            },
            {"type": "string", "maxLength": 4096},
            {
                "type": "array",
                "maxItems": 256,
                "items": {"$ref": "#/$defs/configValue"},
            },
            {
                "type": "object",
                "maxProperties": 256,
                "propertyNames": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_NAME_LENGTH,
                },
                "additionalProperties": {"$ref": "#/$defs/configValue"},
            },
        ]
    }
}
_CONFIG_UPDATE_INPUT = {
    "$defs": _CONFIG_VALUE_DEFS,
    "type": "object",
    "properties": {
        "profile": _PROFILE_INPUT["properties"]["profile"],
        "task": _TASK_INPUT["properties"]["task"],
        "group": _TASK_INPUT["properties"]["task"],
        "argument": _TASK_INPUT["properties"]["task"],
        "value": {"$ref": "#/$defs/configValue"},
    },
    "required": ["profile", "task", "group", "argument", "value"],
    "additionalProperties": False,
}
_TRIGGER_INPUT = {
    "type": "object",
    "properties": {
        "profile": _PROFILE_INPUT["properties"]["profile"],
        "task": _TASK_INPUT["properties"]["task"],
    },
    "required": ["profile", "task"],
    "additionalProperties": False,
}
_JSON_VALUE = {
    "type": ["array", "boolean", "integer", "number", "null", "object", "string"]
}
_PROFILE_OUTPUT = {
    "type": "object",
    "properties": {
        "profile": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_NAME_LENGTH,
        }
    },
    "required": ["profile"],
    "additionalProperties": False,
}
_TASK_OUTPUT = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_NAME_LENGTH,
        },
        "display_name": {"type": "string", "maxLength": 4096},
        "help": {"type": "string", "maxLength": 4096},
    },
    "required": ["name", "display_name", "help"],
    "additionalProperties": False,
}
_TASK_METADATA_SCALAR = {
    "type": ["boolean", "integer", "number", "null", "string"],
    "maxLength": 4096,
}
_TASK_METADATA_VALUE = {
    "oneOf": [
        _TASK_METADATA_SCALAR,
        {
            "type": "array",
            "maxItems": 512,
            "items": _TASK_METADATA_SCALAR,
        },
    ]
}
_TASK_OPTION_OUTPUT = {
    "type": "object",
    "properties": {
        "value": _TASK_METADATA_SCALAR,
        "display_name": {"type": "string", "maxLength": 4096},
    },
    "required": ["value", "display_name"],
    "additionalProperties": False,
}
_TASK_ARGUMENT_OUTPUT = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_LENGTH},
        "display_name": {"type": "string", "maxLength": 4096},
        "help": {"type": "string", "maxLength": 4096},
        "input_type": {"type": "string", "minLength": 1, "maxLength": 128},
        "default": _TASK_METADATA_VALUE,
        "options": {
            "type": "array",
            "maxItems": 256,
            "items": _TASK_OPTION_OUTPUT,
        },
    },
    "required": [
        "name",
        "display_name",
        "help",
        "input_type",
        "default",
        "options",
    ],
    "additionalProperties": False,
}
_TASK_GROUP_OUTPUT = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_LENGTH},
        "display_name": {"type": "string", "maxLength": 4096},
        "help": {"type": "string", "maxLength": 4096},
        "arguments": {
            "type": "array",
            "maxItems": 256,
            "items": _TASK_ARGUMENT_OUTPUT,
        },
    },
    "required": ["name", "display_name", "help", "arguments"],
    "additionalProperties": False,
}
_TASK_METADATA_OUTPUT = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_LENGTH},
        "display_name": {"type": "string", "maxLength": 4096},
        "help": {"type": "string", "maxLength": 4096},
        "groups": {
            "type": "array",
            "maxItems": 256,
            "items": _TASK_GROUP_OUTPUT,
        },
    },
    "required": ["name", "display_name", "help", "groups"],
    "additionalProperties": False,
}
_RESOURCE_OUTPUT = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "maxLength": 4096},
        "label": {"type": "string", "maxLength": 4096},
        "value": _JSON_VALUE,
        "limit": _JSON_VALUE,
        "total": _JSON_VALUE,
        "last_update": _JSON_VALUE,
    },
    "required": ["key", "label", "value"],
    "additionalProperties": False,
}
_SCHEDULER_ENTRY_OUTPUT = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "maxLength": 4096},
        "next_run": {"type": ["string", "null"], "maxLength": 128},
    },
    "required": ["task", "next_run"],
    "additionalProperties": False,
}
_FLEET_SLOT_OUTPUT = {
    "type": "object",
    "properties": {
        "side": {"type": ["string", "null"], "maxLength": 64},
        "position": {"type": "integer", "minimum": 1, "maximum": 3},
        "occupied": {"type": ["boolean", "null"]},
        "identity_status": {"type": ["string", "null"], "maxLength": 64},
        "raw_name_ocr": {"type": "string", "maxLength": 4096},
        "displayed_name": {"type": "string", "maxLength": 4096},
        "canonical_name": {"type": "string", "maxLength": 4096},
        "canonical_identity": {"type": "string", "maxLength": 128},
        "ship_form": {"type": "string", "maxLength": 64},
    },
    "required": ["side", "position", "occupied", "identity_status"],
    "additionalProperties": False,
}
_FLEET_SNAPSHOT_OUTPUT = {
    "type": "object",
    "properties": {
        "fleet_index": {
            "type": "integer",
            "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
        },
        "complete": {"type": "boolean"},
        "occupied_count": {"type": "integer", "minimum": 0, "maximum": 6},
        "catalog_fingerprint": {"type": "string", "maxLength": 128},
        "slots": {
            "type": "array",
            "items": _FLEET_SLOT_OUTPUT,
            "maxItems": 6,
        },
    },
    "required": [
        "fleet_index",
        "complete",
        "occupied_count",
        "catalog_fingerprint",
        "slots",
    ],
    "additionalProperties": False,
}
_FLEET_OBSERVATION_OUTPUT = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 36, "maxLength": 36},
        "run_id": {"type": "string", "minLength": 36, "maxLength": 36},
        "idempotency_key": {"type": "string", "maxLength": 128},
        "fleet_index": {
            "type": "integer",
            "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
        },
        "observed_at": {"type": "string", "maxLength": 128},
        "snapshot": _FLEET_SNAPSHOT_OUTPUT,
    },
    "required": [
        "id",
        "run_id",
        "idempotency_key",
        "fleet_index",
        "observed_at",
        "snapshot",
    ],
    "additionalProperties": False,
}
_MORALE_RECOVERY_OUTPUT = {
    "type": "object",
    "properties": {
        "recovery_per_hour": {"type": ["string", "null"]},
        "recovery_ceiling": {"type": ["string", "null"]},
        "source": {"type": "string", "maxLength": 4096},
    },
    "required": ["recovery_per_hour", "recovery_ceiling", "source"],
    "additionalProperties": False,
}
_MORALE_SLOT_OUTPUT = {
    "type": "object",
    "properties": {
        "fleet_index": {
            "type": "integer",
            "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
        },
        "side": {"type": ["string", "null"], "maxLength": 64},
        "position": {"type": "integer", "minimum": 1, "maximum": 3},
        "occupied": {"type": ["boolean", "null"]},
        "identity_status": {"type": ["string", "null"], "maxLength": 64},
        "knowledge": {"type": "string", "maxLength": 64},
        "baseline": {"type": ["string", "null"]},
        "current": {"type": ["string", "null"]},
        "recovery": {
            "oneOf": [_MORALE_RECOVERY_OUTPUT, {"type": "null"}],
        },
        "observed_at": {"type": ["string", "null"], "maxLength": 128},
        "source": {"type": ["string", "null"], "maxLength": 4096},
        "morale_observation_id": {"type": ["string", "null"], "maxLength": 36},
        "location": {"type": ["string", "null"], "maxLength": 64},
        "dorm_scan_id": {"type": ["string", "null"], "maxLength": 36},
        "canonical_identity": {"type": "string", "maxLength": 128},
        "canonical_name": {"type": "string", "maxLength": 4096},
        "ship_form": {"type": "string", "maxLength": 64},
    },
    "required": [
        "fleet_index",
        "side",
        "position",
        "occupied",
        "identity_status",
        "knowledge",
        "baseline",
        "current",
        "recovery",
        "observed_at",
        "source",
        "morale_observation_id",
        "location",
        "dorm_scan_id",
    ],
    "additionalProperties": False,
}
_MORALE_FLEET_OUTPUT = {
    "type": "object",
    "properties": {
        "fleet_index": {
            "type": "integer",
            "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
        },
        "formation_observation_id": {
            "type": ["string", "null"],
            "maxLength": 36,
        },
        "formation_observed_at": {"type": ["string", "null"], "maxLength": 128},
        "slots": {"type": "array", "items": _MORALE_SLOT_OUTPUT, "maxItems": 6},
    },
    "required": [
        "fleet_index",
        "formation_observation_id",
        "formation_observed_at",
        "slots",
    ],
    "additionalProperties": False,
}
_CONTRACT_OUTPUT = {
    "type": "object",
    "properties": {
        "contract_schema_version": {
            "type": "integer",
            "const": CONTRACT_SCHEMA_VERSION,
        },
        "product_family": {"type": "string", "maxLength": 128},
        "game_mcp_api_version": {
            "type": "integer",
            "const": GAME_MCP_API_VERSION,
        },
        "tool_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 256,
        },
        "tool_catalog_sha256": {
            "type": "string",
            "pattern": r"^[a-f0-9]{64}$",
        },
        "authorization_scopes": {
            "type": "array",
            "minItems": 2,
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 128},
        },
        "feature_flags": {
            "type": "object",
            "maxProperties": 32,
            "additionalProperties": {"type": "boolean"},
        },
        "capability_families": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "maxLength": 128},
        },
        "result_states": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "maxLength": 64},
        },
        "read_only_guarantees": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "maxLength": 128},
        },
        "control_guarantees": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "maxLength": 128},
        },
    },
    "required": [
        "contract_schema_version",
        "product_family",
        "game_mcp_api_version",
        "tool_count",
        "tool_catalog_sha256",
        "authorization_scopes",
        "feature_flags",
        "capability_families",
        "result_states",
        "read_only_guarantees",
        "control_guarantees",
    ],
    "additionalProperties": False,
}
_REQUEST_CONTEXT_OUTPUT = {
    "type": "object",
    "properties": {
        "transport": {
            "type": "string",
            "enum": ["local_stdio", "remote_http"],
        },
        "authenticated": {"type": "boolean"},
        "local_authority": {"type": "boolean"},
        "granted_scopes": {
            "type": "array",
            "maxItems": len(GAME_MCP_SCOPES),
            "uniqueItems": True,
            "items": {
                "type": "string",
                "enum": list(GAME_MCP_SCOPES),
            },
        },
        "read_allowed": {"type": "boolean"},
        "control_allowed": {"type": "boolean"},
    },
    "required": [
        "transport",
        "authenticated",
        "local_authority",
        "granted_scopes",
        "read_allowed",
        "control_allowed",
    ],
    "additionalProperties": False,
}
_DETAILS_COMMON_OUTPUT = {"tool": {"type": "string", "maxLength": 128}}
_FAILURE_CAUSE_OUTPUT = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "maxLength": 128},
        "message": {"type": "string", "maxLength": 4096},
        "details": {
            "type": "object",
            "maxProperties": 32,
            "additionalProperties": _JSON_VALUE,
        },
    },
    "required": ["code", "message", "details"],
    "additionalProperties": False,
}
_PROFILE_DETAILS_OUTPUT = {
    "profile": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_LENGTH},
}
_SELECTION_OUTPUT = {
    "type": "array",
    "minItems": 1,
    "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
    "uniqueItems": True,
    "items": {
        "type": "integer",
        "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
    },
}
_MISSING_FLEET_INDICES_OUTPUT = {
    "type": "array",
    "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
    "uniqueItems": True,
    "items": {
        "type": "integer",
        "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
    },
}
_SCREENSHOT_OUTPUT = {
    "type": "object",
    "properties": {
        "mime": {"type": "string", "enum": ["image/png", "image/jpeg"]},
        "width": {"type": "integer", "minimum": 1, "maximum": 8192},
        "height": {"type": "integer", "minimum": 1, "maximum": 8192},
        "byte_size": {"type": "integer", "minimum": 1, "maximum": 4194304},
        "sha256": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
    },
    "required": ["mime", "width", "height", "byte_size", "sha256"],
    "additionalProperties": False,
}


def _details_output(properties: dict[str, Any]) -> dict[str, Any]:
    """Собрать schema только для полей details конкретного инструмента."""

    return {
        "type": "object",
        "properties": {**_DETAILS_COMMON_OUTPUT, **properties},
        "maxProperties": 32,
        "additionalProperties": False,
    }


_COMMON_OUTPUT_PROPERTIES = {
    "ok": {"type": "boolean"},
    "code": {"type": "string", "maxLength": 128},
    "message": {"type": "string", "maxLength": 4096},
    "state": {"type": "string", "maxLength": 64},
}


def _output_schema(
    details: dict[str, Any],
    *,
    include_failure_cause: bool = False,
) -> dict[str, Any]:
    common_details = dict(_DETAILS_COMMON_OUTPUT)
    if include_failure_cause:
        common_details["cause"] = _FAILURE_CAUSE_OUTPUT
    return {
        "type": "object",
        "properties": {
            **_COMMON_OUTPUT_PROPERTIES,
            "details": {
                "type": "object",
                "properties": {**common_details, **details},
                "maxProperties": 32,
                "additionalProperties": False,
            },
        },
        "required": ["ok", "code", "message", "state", "details"],
        "additionalProperties": False,
    }


_OUTPUT_SCHEMAS = {
    "game_get_contract": _output_schema(
        {"contract": _CONTRACT_OUTPUT, "request_context": _REQUEST_CONTEXT_OUTPUT}
    ),
    "game_list_profiles": _output_schema(
        {
            "profiles": {
                "type": "array",
                "maxItems": 256,
                "items": _PROFILE_OUTPUT,
            }
        }
    ),
    "game_get_profile_status": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "running": {"type": "boolean"},
            "state": {"type": "string", "maxLength": 64},
        }
    ),
    "game_get_resources": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "resources": {
                "type": "array",
                "maxItems": 256,
                "items": _RESOURCE_OUTPUT,
            },
        }
    ),
    "game_get_current_task": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "task": {"type": ["string", "null"], "maxLength": 4096},
        }
    ),
    "game_get_scheduler_queue": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "entries": {
                "type": "array",
                "maxItems": 512,
                "items": _SCHEDULER_ENTRY_OUTPUT,
            },
        }
    ),
    "game_list_tasks": _output_schema(
        {"tasks": {"type": "array", "maxItems": 512, "items": _TASK_OUTPUT}}
    ),
    "game_get_task_help": _output_schema({"task": _TASK_METADATA_OUTPUT}),
    "game_get_fleet_state": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "selection": _SELECTION_OUTPUT,
            "observations": {
                "type": "array",
                "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
                "items": _FLEET_OBSERVATION_OUTPUT,
            },
            "missing_fleet_indices": _MISSING_FLEET_INDICES_OUTPUT,
            "coverage_complete": {"type": "boolean"},
            "snapshots_complete": {"type": "boolean"},
        }
    ),
    "game_get_morale": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "selection": _SELECTION_OUTPUT,
            "projected_at": {"type": "string", "maxLength": 128},
            "fleets": {
                "type": "array",
                "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
                "items": _MORALE_FLEET_OUTPUT,
            },
        }
    ),
    "game_get_config": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "task": {"type": ["string", "null"], "maxLength": 4096},
            "config": {
                "type": "object",
                "maxProperties": 256,
                "additionalProperties": _JSON_VALUE,
            },
        }
    ),
    "game_get_recent_logs": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "lines": {
                "type": "array",
                "maxItems": 200,
                "items": {"type": "string"},
            },
            "truncated": {"type": "boolean"},
        }
    ),
    "game_get_screenshot": _output_schema(
        {**_PROFILE_DETAILS_OUTPUT, "screenshot": _SCREENSHOT_OUTPUT}
    ),
    "game_start_profile": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "outcome": {
                "type": "string",
                "enum": ["started", "already_running"],
            },
        },
        include_failure_cause=True,
    ),
    "game_stop_profile": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "outcome": {
                "type": "string",
                "enum": ["stopped", "already_stopped"],
            },
        },
        include_failure_cause=True,
    ),
    "game_trigger_task": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "task": {"type": "string", "maxLength": MAX_NAME_LENGTH},
            "scheduled_at": {"type": "string", "maxLength": 128},
            "verified": {"type": "boolean", "const": True},
        },
        include_failure_cause=True,
    ),
    "game_clear_scheduler_queue": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "cleared_tasks": {
                "type": "array",
                "maxItems": 512,
                "items": {"type": "string", "maxLength": MAX_NAME_LENGTH},
            },
            "cleared_count": {"type": "integer", "minimum": 0, "maximum": 512},
            "verified": {"type": "boolean", "const": True},
        },
        include_failure_cause=True,
    ),
    "game_update_config": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "task": {"type": "string", "maxLength": MAX_NAME_LENGTH},
            "group": {"type": "string", "maxLength": MAX_NAME_LENGTH},
            "argument": {"type": "string", "maxLength": MAX_NAME_LENGTH},
            "verified": {"type": "boolean", "const": True},
        },
        include_failure_cause=True,
    ),
    "game_restart_emulator": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "verified": {"type": "boolean", "const": True},
        },
        include_failure_cause=True,
    ),
    "game_restart_runtime": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "verified": {"type": "boolean", "const": True},
            "emulator_verified": {"type": "boolean", "const": True},
            "adb_ready": {"type": "boolean", "const": True},
            "game_running": {"type": "boolean", "const": True},
            "game_foreground": {"type": "boolean", "const": True},
            "phase": {
                "type": "string",
                "enum": ["emulator_restart", "game_start"],
            },
        },
        include_failure_cause=True,
    ),
    "game_login_runtime": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "verified": {"type": "boolean", "const": True},
            "adb_ready": {"type": "boolean", "const": True},
            "game_running": {"type": "boolean", "const": True},
            "game_foreground": {"type": "boolean", "const": True},
            "logged_in": {"type": "boolean", "const": True},
            "main": {"type": "boolean", "const": True},
            "phase": {"type": "string", "enum": ["login"]},
        },
        include_failure_cause=True,
    ),
    "game_restart_adb": _output_schema(
        {
            **_PROFILE_DETAILS_OUTPUT,
            "verified": {"type": "boolean", "const": True},
        },
        include_failure_cause=True,
    ),
}
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_MUTATION_ANNOTATIONS = {
    "game_start_profile": ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    "game_stop_profile": ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
    "game_trigger_task": ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    "game_clear_scheduler_queue": ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
    "game_update_config": ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
    "game_restart_emulator": ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
    "game_restart_runtime": ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
    "game_login_runtime": ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
    "game_restart_adb": ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
}


def _tool(name: str, description: str, input_schema: dict[str, Any]) -> Tool:
    return Tool(
        name=name,
        description=description,
        inputSchema=input_schema,
        outputSchema=_OUTPUT_SCHEMAS[name],
        annotations=_MUTATION_ANNOTATIONS.get(name, _READ_ONLY),
        _meta={
            "securitySchemes": [
                {
                    "type": "oauth2",
                    "scopes": [GAME_MCP_TOOL_REQUIRED_SCOPES[name]],
                }
            ]
        },
    )


def tool_definitions() -> list[Tool]:
    """Вернуть детерминированный каталог read и control инструментов."""

    descriptions = {
        "game_get_contract": "Получить стабильный контракт AzurPilot Game MCP read/control plane.",
        "game_list_profiles": "Перечислить канонические профили AzurPilot без путей и секретов.",
        "game_get_profile_status": "Получить статус выбранного профиля AzurPilot.",
        "game_get_resources": "Получить ограниченный снимок игровых ресурсов выбранного профиля.",
        "game_get_current_task": "Получить текущую задачу запущенного профиля.",
        "game_get_scheduler_queue": "Получить read-only очередь scheduler выбранного профиля.",
        "game_list_tasks": "Получить каталог игровых задач и краткую локализованную справку.",
        "game_get_task_help": "Получить bounded metadata и справку одной игровой задачи.",
        "game_get_fleet_state": "Получить сохранённый Fleet State выбранного профиля без физического сканирования.",
        "game_get_morale": "Получить exact, projected или unknown morale выбранного профиля.",
        "game_get_config": "Получить ограниченный и redacted снимок конфигурации профиля.",
        "game_get_recent_logs": "Получить ограниченный и sanitized tail журнала профиля.",
        "game_get_screenshot": "Получить bounded MCP image content выбранного профиля без ввода.",
        "game_start_profile": "Запустить выбранный профиль с подтверждением lifecycle postcondition.",
        "game_stop_profile": "Остановить выбранный профиль с подтверждением lifecycle postcondition.",
        "game_trigger_task": "Поставить generated scheduler task выбранного профиля в очередь.",
        "game_clear_scheduler_queue": "Очистить только generated scheduler queue выбранного профиля.",
        "game_update_config": "Изменить один разрешённый нечувствительный параметр config с readback-проверкой.",
        "game_restart_emulator": "Перезапустить эмулятор выбранного профиля с подтверждением результата.",
        "game_restart_runtime": "Перезапустить эмулятор и вернуть настроенную игру на передний план с подтверждением результата.",
        "game_login_runtime": "Выполнить существующий login flow и подтвердить logged-in main UI без запуска scheduler или полного профиля.",
        "game_restart_adb": "Перезапустить ADB для выбранного профиля после проверки ownership target.",
    }
    schemas = {
        **{
            name: {"type": "object", "properties": {}, "additionalProperties": False}
            for name in GAME_MCP_NO_ARGUMENT_TOOLS
        },
        "game_get_profile_status": _PROFILE_INPUT,
        "game_get_resources": _PROFILE_INPUT,
        "game_get_current_task": _PROFILE_INPUT,
        "game_get_scheduler_queue": _PROFILE_INPUT,
        "game_get_task_help": _TASK_INPUT,
        "game_get_fleet_state": _FLEET_SELECTION_INPUT,
        "game_get_morale": _FLEET_SELECTION_INPUT,
        "game_get_config": _CONFIG_INPUT,
        "game_get_recent_logs": _LOG_INPUT,
        "game_get_screenshot": _PROFILE_INPUT,
        "game_start_profile": _PROFILE_INPUT,
        "game_stop_profile": _PROFILE_INPUT,
        "game_trigger_task": _TRIGGER_INPUT,
        "game_clear_scheduler_queue": _PROFILE_INPUT,
        "game_update_config": _CONFIG_UPDATE_INPUT,
        "game_restart_emulator": _PROFILE_INPUT,
        "game_restart_runtime": _PROFILE_INPUT,
        "game_login_runtime": _PROFILE_INPUT,
        "game_restart_adb": _PROFILE_INPUT,
    }
    return [
        _tool(name, descriptions[name], schemas[name]) for name in GAME_MCP_TOOL_NAMES
    ]


def _text_call_result(response: dict[str, object]) -> CallToolResult:
    text = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=response,
        isError=response.get("ok") is not True,
    )


def _screenshot_call_result(response: GameMcpResponse) -> CallToolResult:
    """Собрать официальный MCP response с native image content."""

    if response.image is None or response.mime_type is None:
        return _text_call_result(response.structured)
    image = ImageContent(
        type="image",
        data=base64.b64encode(response.image).decode("ascii"),
        mimeType=response.mime_type,
    )
    summary = TextContent(
        type="text",
        text=json.dumps(response.structured, ensure_ascii=False, separators=(",", ":")),
    )
    return CallToolResult(
        content=[image, summary],
        structuredContent=response.structured,
        isError=response.structured.get("ok") is not True,
    )


def create_server(
    adapter: GameMcpAdapter | None = None,
    *,
    abandon_on_cancel: bool = False,
    redirect_legacy_stdout: bool = True,
) -> Server:
    """Создать MCP Server без сборки backend и без подключения к источникам.

    `cache_hints` намеренно не задаются: в используемом MCP SDK 2.1.1
    применяются безопасные defaults `ttlMs=0` и `cacheScope=private`. Для
    profile runtime data, logs, morale и screenshots это сохраняет актуальность
    и изоляцию данных.
    """

    bound_adapter = adapter if adapter is not None else GameMcpAdapter()

    async def handle_list_tools(_context: Any, _params: Any) -> ListToolsResult:
        return ListToolsResult(tools=tool_definitions())

    async def handle_call_tool(
        _context: Any,
        params: CallToolRequestParams,
    ) -> CallToolResult:
        name = params.name
        arguments = params.arguments
        required_scope = GAME_MCP_TOOL_REQUIRED_SCOPES.get(name)
        access_token = current_access_token()
        if (
            required_scope is not None
            and access_token is not None
            and required_scope not in access_token.scopes
        ):
            return _text_call_result(
                {
                    "ok": False,
                    "code": "GAME_MCP_UNAUTHORIZED",
                    "message": "Недостаточно полномочий для этого инструмента Game MCP.",
                    "state": "failed",
                    "details": {"tool": name},
                }
            )

        def call_adapter() -> dict[str, object] | GameMcpResponse:
            # Legacy-читатели печатают диагностику; stdout занят MCP JSON-RPC.
            if not redirect_legacy_stdout:
                return bound_adapter.call(name, arguments)
            with _LEGACY_STDOUT_LOCK, redirect_stdout(sys.stderr):
                return bound_adapter.call(name, arguments)

        response = await anyio.to_thread.run_sync(
            call_adapter,
            abandon_on_cancel=abandon_on_cancel,
        )
        if isinstance(response, GameMcpResponse):
            return _screenshot_call_result(response)
        if isinstance(response, dict):
            return _text_call_result(response)
        return _text_call_result(
            {
                "ok": False,
                "code": "GAME_MCP_INTERNAL_ERROR",
                "message": "Game MCP вернул некорректный ответ",
                "state": "failed",
                "details": {},
            }
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


async def run_server(adapter: GameMcpAdapter | None = None) -> None:
    """Запустить единственный локальный транспорт Game MCP — stdio."""

    bound_adapter = adapter if adapter is not None else GameMcpAdapter()
    server = create_server(bound_adapter)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(
                    notification_options=NotificationOptions(),
                ),
            )
    finally:
        close = getattr(bound_adapter, "close", None)
        if callable(close):
            close()


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        return


__all__ = (
    "GAME_MCP_ARGS",
    "GAME_MCP_COMMAND",
    "GAME_MCP_CONTROL_SCOPE",
    "GAME_MCP_REQUIRED_SCOPE",
    "GAME_MCP_SCOPES",
    "SERVER_NAME",
    "SERVER_VERSION",
    "create_server",
    "main",
    "run_server",
    "tool_definitions",
)
