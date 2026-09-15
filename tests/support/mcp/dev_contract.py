from __future__ import annotations

from module.mcp_shared.versioning import load_mcp_bundle, source_revision
from tests.support.paths import REPOSITORY_ROOT


_DEV_SERVER = load_mcp_bundle(REPOSITORY_ROOT).servers["azurpilot-dev"]

EXPECTED_CONTRACT = {
    "contract_schema_version": _DEV_SERVER.contract_schema_version,
    "product_family": "AzurPilot",
    "server_name": _DEV_SERVER.name,
    "server_version": _DEV_SERVER.version,
    "source_revision": source_revision(),
    "dev_mcp_api_version": _DEV_SERVER.api_version,
    "tool_count": len(_DEV_SERVER.tool_names),
    "tool_catalog_sha256": _DEV_SERVER.tool_catalog_sha256,
    "authorization_scopes": list(_DEV_SERVER.authorization_scopes),
    "smoke_spec_schema_version": _DEV_SERVER.smoke_spec_schema_version,
    "smoke_result_schema_version": _DEV_SERVER.smoke_result_schema_version,
    "feature_flags": dict(_DEV_SERVER.feature_flags),
    "capability_families": list(_DEV_SERVER.capability_families),
    "result_outcomes": list(_DEV_SERVER.result_vocabulary),
    "capability_catalog_sha256": _DEV_SERVER.capability_catalog_sha256,
    "contract_revision": _DEV_SERVER.contract_revision,
}
