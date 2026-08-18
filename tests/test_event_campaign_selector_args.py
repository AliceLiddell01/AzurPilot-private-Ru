import pytest

from module.event_datamine.campaign_selector import _configured_servers


@pytest.mark.parametrize(
    "args_data",
    [
        {"Event": []},
        {"Event": {"Campaign": "broken"}},
        {"Event": {"Campaign": {"Event": []}}},
    ],
)
def test_configured_servers_rejects_non_mapping_nested_nodes(args_data):
    assert _configured_servers("event_current", args_data=args_data) == set()


def test_configured_servers_reads_only_matching_server_options():
    args_data = {
        "Event": {
            "Campaign": {
                "Event": {
                    "option_en": ["event_current"],
                    "option_cn": ["event_other"],
                    "option_bold": ["event_current"],
                }
            }
        }
    }

    assert _configured_servers("event_current", args_data=args_data) == {"EN"}
