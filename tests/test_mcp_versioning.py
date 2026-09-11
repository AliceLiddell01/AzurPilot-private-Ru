from __future__ import annotations

import os
from pathlib import Path

import pytest

from module.dev_mcp.contract import contract_payload
from module.game_mcp.contract import contract_payload as game_contract_payload
from module.mcp_shared.versioning import (
    UNKNOWN_SOURCE_REVISION,
    SemVer,
    VersioningError,
    load_server_versions,
    parse_version_range,
    source_revision,
    version_satisfies,
)


def test_manifest_is_the_single_source_for_server_semver() -> None:
    versions = load_server_versions(Path(__file__).resolve().parents[1])

    assert versions == {"azurpilot-dev": "3.0.0", "azurpilot-game": "1.0.0"}
    assert contract_payload()["server_version"] == versions["azurpilot-dev"]
    assert game_contract_payload()["server_version"] == versions["azurpilot-game"]


def test_semver_precedence_follows_semver_without_build_metadata() -> None:
    ordered = [
        SemVer.parse(value)
        for value in (
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-rc.1",
            "1.0.0",
        )
    ]

    assert ordered == sorted(ordered)
    assert SemVer.parse("1.0.0+one") == SemVer.parse("1.0.0+two")
    assert hash(SemVer.parse("1.0.0+one")) == hash(SemVer.parse("1.0.0+two"))


@pytest.mark.parametrize("value", ["1.0", "01.0.0", "1.0.0-01", "1.0.0+"])
def test_invalid_semver_is_rejected(value: str) -> None:
    with pytest.raises(VersioningError):
        SemVer.parse(value)


def test_bounded_ranges_support_exact_compatibility_window() -> None:
    assert parse_version_range(">=3.0.0,<4.0.0")
    assert version_satisfies("3.0.0", ">=3.0.0,<4.0.0")
    assert not version_satisfies("4.0.0", ">=3.0.0,<4.0.0")
    assert version_satisfies("3.0.0", "=3.0.0")
    assert version_satisfies("3.0.0", "3.0.0")


@pytest.mark.parametrize("value", [">=3.0.0", ">3.0.0", "<4.0.0", "<=4.0.0"])
def test_unbounded_ranges_are_rejected(value: str) -> None:
    with pytest.raises(VersioningError):
        parse_version_range(value)


def test_source_revision_is_bounded_and_never_falls_back_to_environment_dump(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AZURPILOT_SOURCE_REVISION", raising=False)
    assert source_revision() == UNKNOWN_SOURCE_REVISION

    monkeypatch.setenv("AZURPILOT_SOURCE_REVISION", "A" * 40)
    assert source_revision() == "a" * 40

    monkeypatch.setenv("AZURPILOT_SOURCE_REVISION", os.environ.get("PATH", ""))
    assert source_revision() == UNKNOWN_SOURCE_REVISION
