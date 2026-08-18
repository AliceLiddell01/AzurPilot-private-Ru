import pytest

import module.event_datamine.compiler as compiler_module
from module.event_datamine.compiler import EventCompiler
from module.shop_event.filter_identity import (
    FilterIdentityDataError,
    load_filter_identities,
    validate_filter_identity_data,
)
from module.shop_event.selector import FILTER_REGEX


def test_packaged_filter_identity_registry_is_valid_and_supported():
    identities = load_filter_identities()

    assert identities
    assert all(FILTER_REGEX.fullmatch(token.lower()) for token in identities.values())


def test_filter_identity_registry_rejects_duplicate_and_unknown_fields():
    with pytest.raises(FilterIdentityDataError):
        validate_filter_identity_data(
            {
                "schema_version": 1,
                "entries": [
                    {"item_type": 2, "item_id": 10, "token": "Chip"},
                    {"item_type": 2, "item_id": 10, "token": "Oil"},
                ],
            }
        )

    with pytest.raises(FilterIdentityDataError):
        validate_filter_identity_data(
            {
                "schema_version": 1,
                "entries": [
                    {"item_type": 2, "item_id": 10, "token": "Chip", "extra": True}
                ],
            }
        )


def test_filter_identity_registry_rejects_nonpositive_and_boolean_ids():
    for field, value in (("item_type", 0), ("item_id", -1), ("item_id", True)):
        entry = {"item_type": 2, "item_id": 10, "token": "Chip"}
        entry[field] = value
        with pytest.raises(FilterIdentityDataError):
            validate_filter_identity_data(
                {"schema_version": 1, "entries": [entry]}
            )


def test_unsupported_explicit_runtime_filter_records_finding(monkeypatch):
    compiler = object.__new__(EventCompiler)
    compiler.findings = []
    monkeypatch.setattr(
        compiler_module,
        "runtime_filter_token",
        lambda item_type, item_id: "NotAFilter",
    )

    result = compiler._runtime_filter(
        item_type=2,
        item_id=10,
        name="Тестовый товар",
        rarity=None,
        source_path="",
        path="shop.7.event_shop_filter",
    )

    assert result == ""
    assert len(compiler.findings) == 1
    finding = compiler.findings[0]
    assert finding.code == "runtime_filter_unsupported"
    assert finding.severity == "warning"
    assert finding.path == "shop.7.event_shop_filter"
    assert "NotAFilter" in finding.message


def test_compiler_has_no_game_id_filter_mapping():
    assert not hasattr(compiler_module, "RUNTIME_FILTER_BY_GAME_ID")
