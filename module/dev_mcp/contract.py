"""Публичный read-only контракт совместимости AzurPilot Dev MCP."""

from __future__ import annotations

from collections.abc import Mapping

from module.dev_runtime.smoke import (
    SMOKE_SCHEMA_VERSION,
    SMOKE_STATE_SCHEMA_VERSION,
    SmokeOutcome,
)
from module.mcp_shared.versioning import (
    server_version,
    source_revision,
    version_satisfies,
)

CONTRACT_SCHEMA_VERSION = 1
DEV_MCP_API_VERSION = 3
PRODUCT_FAMILY = "AzurPilot"
DEV_MCP_SERVER_NAME = "azurpilot-dev"
DEV_MCP_REQUIRED_SCOPE = "azurpilot:dev"

DEV_MCP_FEATURE_FLAGS = {
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
}
DEV_MCP_CAPABILITY_FAMILIES = (
    "diagnostics",
    "evidence",
    "lifecycle",
    "smoke",
    "runtime_control",
    "game",
    "database",
)
DEV_MCP_RESULT_OUTCOMES = tuple(outcome.value for outcome in SmokeOutcome)


def contract_payload() -> dict[str, object]:
    """Вернуть только стабильные поля публичной границы совместимости."""

    return {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "product_family": PRODUCT_FAMILY,
        "server_name": DEV_MCP_SERVER_NAME,
        "server_version": server_version(DEV_MCP_SERVER_NAME),
        "source_revision": source_revision(),
        "dev_mcp_api_version": DEV_MCP_API_VERSION,
        "smoke_spec_schema_version": SMOKE_SCHEMA_VERSION,
        "smoke_result_schema_version": SMOKE_STATE_SCHEMA_VERSION,
        "feature_flags": dict(DEV_MCP_FEATURE_FLAGS),
        "capability_families": list(DEV_MCP_CAPABILITY_FAMILIES),
        "result_outcomes": list(DEV_MCP_RESULT_OUTCOMES),
    }


def contract_result() -> dict[str, object]:
    """Вернуть безопасный результат read-only инструмента контракта."""

    return {
        "ok": True,
        "code": "DEV_MCP_CONTRACT_READY",
        "message": "Контракт совместимости AzurPilot Dev MCP готов",
        "state": "ready",
        "session_id": None,
        "details": {"contract": contract_payload()},
    }


def server_compatibility_issues(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> tuple[str, ...]:
    """Проверить identity и bounded version range конкретного MCP-сервера."""

    expected_servers = expected.get("required_mcp_servers")
    if not isinstance(expected_servers, Mapping) or not expected_servers:
        return ("required_mcp_servers",)

    server_name = actual.get("server_name")
    server_version_value = actual.get("server_version")
    if not isinstance(server_name, str) or not isinstance(server_version_value, str):
        return ("server_identity",)

    expected_range = expected_servers.get(server_name)
    if not isinstance(expected_range, str):
        return ("server_name",)
    try:
        compatible = version_satisfies(server_version_value, expected_range)
    except ValueError:
        compatible = False
    return () if compatible else ("server_version",)


def contract_compatibility_issues(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> tuple[str, ...]:
    """Проверить требования пакета без догадок о несовместимых версиях."""

    issues: list[str] = list(server_compatibility_issues(expected, actual))
    for field in ("contract_schema_version", "product_family"):
        expected_value = expected.get(field)
        actual_value = actual.get(field)
        if (
            field not in expected
            or expected_value is None
            or field not in actual
            or type(actual_value) is not type(expected_value)
            or actual_value != expected_value
        ):
            issues.append(field)

    for field in (
        "dev_mcp_api_version",
        "smoke_spec_schema_version",
        "smoke_result_schema_version",
    ):
        if field not in expected:
            continue
        expected_value = expected.get(field)
        actual_value = actual.get(field)
        if type(actual_value) is not type(expected_value) or actual_value != expected_value:
            issues.append(field)

    expected_flags = expected.get("required_feature_flags")
    actual_flags = actual.get("feature_flags")
    if not isinstance(expected_flags, Mapping) or not isinstance(actual_flags, Mapping):
        issues.append("feature_flags")
    else:
        for name, expected_value in expected_flags.items():
            actual_value = actual_flags.get(name)
            if type(actual_value) is not type(expected_value) or actual_value != expected_value:
                issues.append(f"feature_flags.{name}")

    expected_families = expected.get("required_capability_families")
    actual_families = actual.get("capability_families")
    if not isinstance(expected_families, (list, tuple)) or not isinstance(actual_families, (list, tuple)):
        issues.append("capability_families")
    else:
        missing = [name for name in expected_families if name not in actual_families]
        if missing:
            issues.append("capability_families")

    expected_outcomes = expected.get("result_outcomes")
    actual_outcomes = actual.get("result_outcomes")
    if not isinstance(expected_outcomes, (list, tuple)) or not isinstance(actual_outcomes, (list, tuple)):
        issues.append("result_outcomes")
    else:
        missing = [name for name in expected_outcomes if name not in actual_outcomes]
        if missing:
            issues.append("result_outcomes")

    return tuple(issues)


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "DEV_MCP_API_VERSION",
    "DEV_MCP_CAPABILITY_FAMILIES",
    "DEV_MCP_FEATURE_FLAGS",
    "DEV_MCP_REQUIRED_SCOPE",
    "DEV_MCP_RESULT_OUTCOMES",
    "DEV_MCP_SERVER_NAME",
    "PRODUCT_FAMILY",
    "contract_compatibility_issues",
    "contract_payload",
    "contract_result",
    "server_compatibility_issues",
]
