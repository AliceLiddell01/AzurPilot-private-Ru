"""Публичный read-only контракт совместимости AzurPilot Dev MCP."""

from __future__ import annotations

from collections.abc import Mapping

from module.dev_runtime.smoke import (
    SMOKE_RESULT_SCHEMA_VERSION,
    SMOKE_SCHEMA_VERSION,
    SmokeOutcome,
)
from module.mcp_shared.catalog import (
    capability_catalog_sha256,
    contract_revision,
    tool_catalog_sha256_from_tools,
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
DEV_MCP_SERVER_VERSION = server_version(DEV_MCP_SERVER_NAME)
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

    # Импорт выполняется лениво: server.py импортирует этот модуль при старте,
    # поэтому ранний импорт каталога создал бы циклическую зависимость.
    from module.dev_mcp.adapter import DEV_MCP_TOOL_NAMES
    from module.dev_mcp.server import tool_definitions

    payload: dict[str, object] = {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "product_family": PRODUCT_FAMILY,
        "server_name": DEV_MCP_SERVER_NAME,
        "server_version": DEV_MCP_SERVER_VERSION,
        "source_revision": source_revision(),
        "dev_mcp_api_version": DEV_MCP_API_VERSION,
        "tool_count": len(DEV_MCP_TOOL_NAMES),
        "tool_catalog_sha256": tool_catalog_sha256_from_tools(tool_definitions()),
        "authorization_scopes": [DEV_MCP_REQUIRED_SCOPE],
        "smoke_spec_schema_version": SMOKE_SCHEMA_VERSION,
        "smoke_result_schema_version": SMOKE_RESULT_SCHEMA_VERSION,
        "feature_flags": dict(DEV_MCP_FEATURE_FLAGS),
        "capability_families": list(DEV_MCP_CAPABILITY_FAMILIES),
        "result_outcomes": list(DEV_MCP_RESULT_OUTCOMES),
    }
    payload["capability_catalog_sha256"] = capability_catalog_sha256(payload)
    payload["contract_revision"] = contract_revision(payload)
    return payload


def contract_result(*, request_context: Mapping[str, object] | None = None) -> dict[str, object]:
    """Вернуть безопасный результат read-only инструмента контракта."""

    details: dict[str, object] = {"contract": contract_payload()}
    if request_context is not None:
        details["request_context"] = dict(request_context)
    return {
        "ok": True,
        "code": "DEV_MCP_CONTRACT_READY",
        "message": "Контракт совместимости AzurPilot Dev MCP готов",
        "state": "ready",
        "session_id": None,
        "details": details,
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
    issues: list[str] = [] if compatible else ["server_version"]
    required_flags = expected.get("required_feature_flags_by_server")
    actual_flags = actual.get("feature_flags")
    expected_flags = (
        required_flags.get(server_name)
        if isinstance(required_flags, Mapping)
        else None
    )
    if isinstance(expected_flags, Mapping):
        if not isinstance(actual_flags, Mapping):
            issues.append(f"servers.{server_name}.feature_flags")
        else:
            for name, value in expected_flags.items():
                if actual_flags.get(name) is not value:
                    issues.append(f"servers.{server_name}.feature_flags.{name}")
    required_families = expected.get("required_capability_families_by_server")
    actual_families = actual.get("capability_families")
    expected_family_values = (
        required_families.get(server_name)
        if isinstance(required_families, Mapping)
        else None
    )
    if isinstance(expected_family_values, (list, tuple)) and (
        not isinstance(actual_families, (list, tuple))
        or any(value not in actual_families for value in expected_family_values)
    ):
        issues.append(f"servers.{server_name}.capability_families")
    required_vocabulary = expected.get("result_vocabulary_by_server")
    actual_vocabulary = actual.get("result_states", actual.get("result_outcomes"))
    expected_vocabulary = (
        required_vocabulary.get(server_name)
        if isinstance(required_vocabulary, Mapping)
        else None
    )
    if isinstance(expected_vocabulary, (list, tuple)) and (
        not isinstance(actual_vocabulary, (list, tuple))
        or any(value not in actual_vocabulary for value in expected_vocabulary)
    ):
        issues.append(f"servers.{server_name}.result_vocabulary")
    return tuple(dict.fromkeys(issues))


def server_bundle_drift_issues(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> tuple[str, ...]:
    """Проверить exact catalog/contract identity отдельно от SemVer range."""

    server_name = actual.get("server_name")
    if not isinstance(server_name, str):
        return ("server_identity",)
    expected_catalog = expected.get("servers")
    expected_metadata = (
        expected_catalog.get(server_name)
        if isinstance(expected_catalog, Mapping)
        else None
    )
    if not isinstance(expected_metadata, Mapping):
        return (f"servers.{server_name}",)
    actual_fields = {
        "api_version": (
            "dev_mcp_api_version"
            if server_name == DEV_MCP_SERVER_NAME
            else "game_mcp_api_version"
        ),
        "contract_schema_version": "contract_schema_version",
        "tool_count": "tool_count",
        "tool_catalog_sha256": "tool_catalog_sha256",
        "capability_catalog_sha256": "capability_catalog_sha256",
        "contract_revision": "contract_revision",
    }
    return tuple(
        f"servers.{server_name}.{field}"
        for field, actual_field in actual_fields.items()
        if field not in expected_metadata
        or actual_field not in actual
        or actual[actual_field] != expected_metadata[field]
    )


def contract_compatibility_issues(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> tuple[str, ...]:
    """Проверить требования пакета в пределах конкретного server contract.

    Верхнеуровневые поля compatibility bundle исторически принадлежат Dev
    MCP. Game MCP публикует собственные ``result_states`` и feature families,
    поэтому переносить Dev metadata в его сравнение нельзя.
    """

    issues: list[str] = list(server_compatibility_issues(expected, actual))
    # Эти identity-поля обязательны; необязательные version-поля сравниваются
    # ниже только при наличии.
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

    server_name = actual.get("server_name")
    if server_name == DEV_MCP_SERVER_NAME:
        for field in (
            "dev_mcp_api_version",
            "smoke_spec_schema_version",
            "smoke_result_schema_version",
        ):
            if field not in expected:
                continue
            expected_value = expected.get(field)
            actual_value = actual.get(field)
            if (
                type(actual_value) is not type(expected_value)
                or actual_value != expected_value
            ):
                issues.append(field)

        expected_flags = expected.get("required_feature_flags")
        actual_flags = actual.get("feature_flags")
        if not isinstance(expected_flags, Mapping) or not isinstance(actual_flags, Mapping):
            issues.append("feature_flags")
        else:
            for name, expected_value in expected_flags.items():
                actual_value = actual_flags.get(name)
                if (
                    type(actual_value) is not type(expected_value)
                    or actual_value != expected_value
                ):
                    issues.append(f"feature_flags.{name}")

        expected_families = expected.get("required_capability_families")
        actual_families = actual.get("capability_families")
        if (
            not isinstance(expected_families, (list, tuple))
            or not isinstance(actual_families, (list, tuple))
            or any(name not in actual_families for name in expected_families)
        ):
            issues.append("capability_families")

        expected_outcomes = expected.get("result_outcomes")
        actual_outcomes = actual.get("result_outcomes")
        if (
            not isinstance(expected_outcomes, (list, tuple))
            or not isinstance(actual_outcomes, (list, tuple))
            or any(name not in actual_outcomes for name in expected_outcomes)
        ):
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
    "DEV_MCP_SERVER_VERSION",
    "PRODUCT_FAMILY",
    "contract_compatibility_issues",
    "contract_payload",
    "contract_result",
    "server_bundle_drift_issues",
    "server_compatibility_issues",
]
