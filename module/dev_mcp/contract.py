"""Публичный read-only контракт совместимости AzurPilot Dev MCP."""

from __future__ import annotations

from collections.abc import Mapping

from module.dev_runtime.contracts import DEV_PROFILE
from module.dev_runtime.smoke import SMOKE_SCHEMA_VERSION, SMOKE_STATE_SCHEMA_VERSION

CONTRACT_SCHEMA_VERSION = 1
DEV_MCP_API_VERSION = 1
PRODUCT_FAMILY = "AzurPilot"

DEV_MCP_FEATURE_FLAGS = {
    "task_sandbox": True,
    "evidence_api": True,
    "universal_smoke_harness": True,
    "external_visual_evaluation": True,
}
DEV_MCP_CAPABILITY_FAMILIES = ("diagnostics", "evidence", "lifecycle", "smoke")


def contract_payload() -> dict[str, object]:
    """Вернуть только стабильные поля публичной границы совместимости."""

    return {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "product_family": PRODUCT_FAMILY,
        "dev_mcp_api_version": DEV_MCP_API_VERSION,
        "smoke_spec_schema_version": SMOKE_SCHEMA_VERSION,
        "smoke_result_schema_version": SMOKE_STATE_SCHEMA_VERSION,
        "profile": DEV_PROFILE,
        "feature_flags": dict(DEV_MCP_FEATURE_FLAGS),
        "capability_families": list(DEV_MCP_CAPABILITY_FAMILIES),
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


def contract_compatibility_issues(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> tuple[str, ...]:
    """Проверить требования пакета без догадок о несовместимых версиях."""

    issues: list[str] = []
    for field in (
        "contract_schema_version",
        "product_family",
        "dev_mcp_api_version",
        "smoke_spec_schema_version",
        "smoke_result_schema_version",
        "profile",
    ):
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

    return tuple(issues)


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "DEV_MCP_API_VERSION",
    "DEV_MCP_CAPABILITY_FAMILIES",
    "DEV_MCP_FEATURE_FLAGS",
    "PRODUCT_FAMILY",
    "contract_compatibility_issues",
    "contract_payload",
    "contract_result",
]
