from __future__ import annotations

import json
from pathlib import Path

import pytest

from module.config.config_updater import ConfigUpdater

ROOT = Path(__file__).resolve().parents[1]


def test_existing_profile_receives_safe_disabled_defaults() -> None:
    updated = ConfigUpdater().config_update({})

    assert updated["Alas"]["FleetAutoScan"] == {
        "Mode": "disabled",
        "Fleets": [1, 2, 3, 4, 5, 6],
    }


@pytest.mark.parametrize(
    "fleet_autoscan",
    [
        {"Mode": "sometimes", "Fleets": [1, 2]},
        {"Mode": None, "Fleets": [1, 2]},
        {"Mode": "", "Fleets": [1, 2]},
        {"Mode": "daily", "Fleets": []},
        {"Mode": "daily", "Fleets": None},
        {"Mode": "daily", "Fleets": ""},
        {"Mode": "daily", "Fleets": [1, 7]},
    ],
)
def test_invalid_persisted_autoscan_config_fails_closed(fleet_autoscan) -> None:
    with pytest.raises(ValueError):
        ConfigUpdater().config_update(
            {"Alas": {"FleetAutoScan": fleet_autoscan}}
        )


def test_generated_contract_and_russian_i18n_are_complete() -> None:
    args = json.loads(
        (ROOT / "module/config/argument/args.json").read_text(encoding="utf-8")
    )
    template = json.loads(
        (ROOT / "config/template.json").read_text(encoding="utf-8")
    )
    ru = json.loads(
        (ROOT / "module/config/i18n/ru-RU.json").read_text(encoding="utf-8")
    )

    contract = args["Alas"]["FleetAutoScan"]
    assert contract["Mode"]["value"] == "disabled"
    assert contract["Mode"]["option"] == ["disabled", "every_start", "daily"]
    assert contract["Mode"]["strict"] is True
    assert contract["Fleets"]["type"] == "multiselect"
    assert contract["Fleets"]["value"] == [1, 2, 3, 4, 5, 6]
    assert contract["Fleets"]["strict"] is True
    assert template["Alas"]["FleetAutoScan"] == {
        "Mode": "disabled",
        "Fleets": [1, 2, 3, 4, 5, 6],
    }
    assert ru["FleetAutoScan"]["Mode"]["disabled"] == "Отключено"
    assert ru["FleetAutoScan"]["Mode"]["daily"] == "Один раз в день"
    assert ru["FleetAutoScan"]["Fleets"]["6"] == "Флот 6"
