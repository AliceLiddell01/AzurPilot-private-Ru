from types import SimpleNamespace

import pytest

from module.application.fleet_mapping import (
    physical_fleet_index,
    working_fleet_bindings,
    working_fleet_bindings_from_data,
)


def test_logical_fleet_maps_to_configured_physical_surface_fleet():
    config = SimpleNamespace(
        config_name="alas",
        task=SimpleNamespace(command="Main"),
        Fleet_Fleet1=4,
        Fleet_Fleet2=6,
        Fleet_FleetOrder="fleet1_mob_fleet2_boss",
    )

    bindings = working_fleet_bindings(config)

    assert physical_fleet_index(config, 1) == 4
    assert physical_fleet_index(config, 2) == 6
    assert [(item.role, item.physical_fleet_index) for item in bindings] == [
        ("mob", 4),
        ("boss", 6),
    ]


def test_working_mapping_from_profile_only_includes_active_all_role():
    data = {
        "Main": {
            "Fleet": {
                "Fleet1": 5,
                "Fleet2": 6,
                "FleetOrder": "fleet1_standby_fleet2_all",
            }
        }
    }

    bindings = working_fleet_bindings_from_data(data, "Main")

    assert len(bindings) == 1
    assert bindings[0].logical_fleet_index == 2
    assert bindings[0].physical_fleet_index == 6
    assert bindings[0].role == "all"


def test_working_mapping_rejects_duplicate_physical_fleet():
    with pytest.raises(ValueError, match="physical Fleet"):
        working_fleet_bindings_from_data(
            {
                "Main": {
                    "Fleet": {
                        "Fleet1": 3,
                        "Fleet2": 3,
                        "FleetOrder": "fleet1_mob_fleet2_boss",
                    }
                }
            },
            "Main",
        )


def test_working_mapping_rejects_zero_for_active_second_fleet():
    with pytest.raises(ValueError, match=r"диапазоне 1\.\.6"):
        working_fleet_bindings_from_data(
            {
                "Main": {
                    "Fleet": {
                        "Fleet1": 5,
                        "Fleet2": 0,
                        "FleetOrder": "fleet1_standby_fleet2_all",
                    }
                }
            },
            "Main",
        )
