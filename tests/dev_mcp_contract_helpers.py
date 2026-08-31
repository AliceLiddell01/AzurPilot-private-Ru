from __future__ import annotations

EXPECTED_CONTRACT = {
    "contract_schema_version": 1,
    "product_family": "AzurPilot",
    "dev_mcp_api_version": 2,
    "smoke_spec_schema_version": 1,
    "smoke_result_schema_version": 1,
    "feature_flags": {
        "task_sandbox": True,
        "evidence_api": True,
        "universal_smoke_harness": True,
        "external_visual_evaluation": True,
        "runtime_control": True,
        "game_lifecycle": True,
        "emulator_lifecycle": True,
        "adb_maintenance": True,
    },
    "capability_families": ["diagnostics", "evidence", "lifecycle", "smoke", "runtime_control"],
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
