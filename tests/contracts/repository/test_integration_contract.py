from __future__ import annotations

import dev_tools.integration_contract_gate as gate
from tests.support.paths import REPOSITORY_ROOT


def test_permanent_direct_integration_contract_is_ready():
    payload = gate.check(REPOSITORY_ROOT)

    assert payload["ok"] is True
    assert payload["code"] == "INTEGRATION_CONTRACT_READY"
    assert tuple(payload["families"]) == gate.EXPECTED_FAMILIES
    assert all(value == "ready" for value in payload["checks"].values())
    assert payload["errors"] == []


def test_permanent_contract_has_no_retired_profile_paths():
    assert gate.RETIRED_PROFILE_PATHS
    assert all(
        not (REPOSITORY_ROOT / relative).exists()
        for relative in gate.RETIRED_PROFILE_PATHS
    )
