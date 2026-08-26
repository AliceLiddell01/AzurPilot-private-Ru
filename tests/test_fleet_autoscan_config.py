from __future__ import annotations

import json
from pathlib import Path

import pytest

from module.config.config_updater import ConfigUpdater

ROOT = Path(__file__).resolve().parents[1]


def test_new_profile_receives_disabled_scheduler_defaults() -> None:
    updated = ConfigUpdater().config_update({})

    assert updated["FleetAutoScan"]["Scheduler"]["Enable"] is False
    assert updated["FleetAutoScan"]["FleetAutoScan"] == {
        "Fleets": [1, 2, 3, 4, 5, 6],
    }
    assert "FleetAutoScan" not in updated["Alas"]


@pytest.mark.parametrize(
    ("mode", "enabled"),
    [
        ("disabled", False),
        ("every_start", True),
        ("daily", True),
    ],
)
def test_legacy_mode_and_selection_migrate_once_to_scheduler(mode, enabled) -> None:
    updated = ConfigUpdater().config_update(
        {
            "Alas": {
                "FleetAutoScan": {
                    "Mode": mode,
                    "Fleets": [6, 2, 6],
                }
            }
        }
    )

    assert updated["FleetAutoScan"]["Scheduler"]["Enable"] is enabled
    assert updated["FleetAutoScan"]["FleetAutoScan"]["Fleets"] == [2, 6]
    assert "FleetAutoScan" not in updated["Alas"]


@pytest.mark.parametrize("mode", ["sometimes", None, ""])
def test_invalid_legacy_mode_fails_closed(mode) -> None:
    with pytest.raises(ValueError):
        ConfigUpdater().config_update(
            {
                "Alas": {
                    "FleetAutoScan": {
                        "Mode": mode,
                        "Fleets": [1, 2],
                    }
                }
            }
        )


@pytest.mark.parametrize(
    "fleets",
    [[], "1,2", [1, "2"], [True, 2], [0, 1], [1, 7]],
)
def test_invalid_legacy_selection_fails_closed(fleets) -> None:
    with pytest.raises(ValueError):
        ConfigUpdater().config_update(
            {
                "Alas": {
                    "FleetAutoScan": {
                        "Mode": "daily",
                        "Fleets": fleets,
                    }
                }
            }
        )


def test_generated_scheduler_contract_is_complete() -> None:
    args = json.loads(
        (ROOT / "module/config/argument/args.json").read_text(encoding="utf-8")
    )
    menu = json.loads(
        (ROOT / "module/config/argument/menu.json").read_text(encoding="utf-8")
    )
    template = json.loads(
        (ROOT / "config/template.json").read_text(encoding="utf-8")
    )

    contract = args["FleetAutoScan"]
    assert contract["Scheduler"]["Enable"]["value"] is False
    assert contract["Scheduler"]["Command"]["value"] == "FleetAutoScan"
    assert contract["Scheduler"]["FailureInterval"]["value"] == 120
    assert contract["FleetAutoScan"]["Fleets"]["type"] == "multiselect"
    assert contract["FleetAutoScan"]["Fleets"]["value"] == [1, 2, 3, 4, 5, 6]
    assert contract["FleetAutoScan"]["Fleets"]["strict"] is True
    assert "FleetAutoScan" in menu["Alas"]["tasks"]
    assert template["FleetAutoScan"]["Scheduler"]["Enable"] is False
    assert template["FleetAutoScan"]["FleetAutoScan"]["Fleets"] == [1, 2, 3, 4, 5, 6]
    assert "FleetAutoScan" not in template["Alas"]
