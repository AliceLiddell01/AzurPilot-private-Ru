import pytest

import module.event_datamine.compiler as compiler_module
from module.event_datamine.compiler import EventCompiler
from module.shop_event.filter_identity import (
    FilterIdentityDataError,
    load_filter_identities,
    runtime_filter_token,
    validate_filter_identity_data,
)
from module.shop_event.selector import FILTER_REGEX


def _registry(entries=None, rules=None):
    return {
        "schema_version": 2,
        "entries": [] if entries is None else entries,
        "rules": [] if rules is None else rules,
    }


def test_packaged_filter_identity_registry_is_valid_and_supported():
    identities = load_filter_identities()

    assert identities
    assert all(FILTER_REGEX.fullmatch(value.lower()) for value in identities.values())


def test_filter_identity_registry_rejects_duplicate_and_unknown_fields():
    with pytest.raises(FilterIdentityDataError):
        validate_filter_identity_data(
            _registry(
                entries=[
                    {"item_type": 2, "item_id": 10, "filter": "Chip"},
                    {"item_type": 2, "item_id": 10, "filter": "Oil"},
                ]
            )
        )

    with pytest.raises(FilterIdentityDataError):
        validate_filter_identity_data(
            _registry(
                entries=[
                    {"item_type": 2, "item_id": 10, "filter": "Chip", "extra": True}
                ]
            )
        )


def test_filter_identity_registry_rejects_nonpositive_and_boolean_ids():
    for field, value in (("item_type", 0), ("item_id", -1), ("item_id", True)):
        entry = {"item_type": 2, "item_id": 10, "filter": "Chip"}
        entry[field] = value
        with pytest.raises(FilterIdentityDataError):
            validate_filter_identity_data(_registry(entries=[entry]))


def test_filter_identity_registry_rejects_unsupported_filter_token():
    with pytest.raises(FilterIdentityDataError, match="неподдерживаемый filter"):
        validate_filter_identity_data(
            _registry(
                entries=[
                    {"item_type": 2, "item_id": 10, "filter": "NotAFilter"}
                ]
            )
        )


def test_packaged_filter_rules_cover_generic_runtime_fallbacks():
    assert runtime_filter_token(4, 999999, name="Корабль", rarity=5) == "ShipSSR"
    assert runtime_filter_token(3, 999999, name="Снаряжение", rarity=5) == "EquipSSR"
    assert (
        runtime_filter_token(
            2,
            999999,
            name="Неизвестный ящик",
            source_path="Props/appearancebox/test",
        )
        == "SkinBox"
    )
    assert (
        runtime_filter_token(
            2,
            999999,
            name="General Blueprint - Series 8",
        )
        == "PRS8"
    )
    assert (
        runtime_filter_token(
            2,
            999999,
            name="Special General Blueprint - Series 8",
        )
        == "DRS8"
    )


def test_unsupported_explicit_runtime_filter_records_finding(monkeypatch):
    compiler = object.__new__(EventCompiler)
    compiler.findings = []
    monkeypatch.setattr(
        compiler_module,
        "runtime_filter_token",
        lambda *args, **kwargs: "NotAFilter",
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


def test_compiler_has_no_game_id_filter_mapping_or_filter_heuristics():
    assert not hasattr(compiler_module, "RUNTIME_FILTER_BY_GAME_ID")
    assert not hasattr(compiler_module, "_BLUEPRINT_SERIES")
