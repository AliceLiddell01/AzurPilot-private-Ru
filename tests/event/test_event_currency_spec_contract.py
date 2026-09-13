import pytest

from module.webui.event_currency import _shop_uses_runtime_currency


@pytest.mark.parametrize(
    "spec",
    [
        {"currencies": None, "shop_items": []},
        {"currencies": [], "shop_items": None},
        {
            "currencies": [{"runtime_token": "pt"}],
            "shop_items": [{"currency_id": None}],
        },
        {
            "currencies": [{"id": None, "runtime_token": "pt"}],
            "shop_items": [{}],
        },
    ],
)
def test_runtime_currency_link_fails_closed_for_incomplete_event_spec(spec):
    assert _shop_uses_runtime_currency(spec, "pt") is False


def test_runtime_currency_link_requires_real_matching_identifiers():
    spec = {
        "currencies": [{"id": 741, "runtime_token": "pt"}],
        "shop_items": [{"currency_id": 741}],
    }

    assert _shop_uses_runtime_currency(spec, "pt") is True
    assert _shop_uses_runtime_currency(spec, "urpt") is False
