from __future__ import annotations

from module.dev_mcp.adapter import DEV_MCP_TOOL_NAMES
from module.dev_mcp.server import tool_definitions
from module.mcp_shared.catalog import (
    capability_catalog_sha256,
    contract_revision,
    tool_catalog_sha256_from_tools,
)
from module.mcp_shared.versioning import server_version, source_revision

EXPECTED_CONTRACT = {
    "contract_schema_version": 1,
    "product_family": "AzurPilot",
    "server_name": "azurpilot-dev",
    "server_version": server_version("azurpilot-dev"),
    "source_revision": source_revision(),
    "dev_mcp_api_version": 3,
    "tool_count": len(DEV_MCP_TOOL_NAMES),
    "tool_catalog_sha256": tool_catalog_sha256_from_tools(tool_definitions()),
    "authorization_scopes": ["azurpilot:dev"],
    "smoke_spec_schema_version": 2,
    "smoke_result_schema_version": 2,
    "feature_flags": {
        "task_sandbox": True,
        "evidence_api": True,
        "universal_smoke_harness": True,
        "external_visual_evaluation": True,
        "runtime_control": True,
        "game_lifecycle": True,
        "emulator_lifecycle": True,
        "adb_maintenance": True,
        "game_observations": True,
        "database_diagnostics": True,
        "database_repairs": False,
    },
    "capability_families": [
        "diagnostics",
        "evidence",
        "lifecycle",
        "smoke",
        "runtime_control",
        "game",
        "database",
    ],
    "result_outcomes": [
        "PASS",
        "PRODUCT_FAILED",
        "PRECONDITION_FAILED",
        "HARNESS_FAILED",
        "EVIDENCE_INCOMPLETE",
        "TIMEOUT",
        "INVALIDATED",
        "CANCELLED",
    ],
}
EXPECTED_CONTRACT["capability_catalog_sha256"] = capability_catalog_sha256(
    EXPECTED_CONTRACT
)
EXPECTED_CONTRACT["contract_revision"] = contract_revision(EXPECTED_CONTRACT)
