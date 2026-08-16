from types import SimpleNamespace

import module.webui.event_shop_priority as priority
from module.webui.event_shop_priority import (
    load_event_shop_priority,
    prepare_event_shop_runtime_items,
    set_event_shop_priority,
)


class FakeConfig:
    config_name = "target-baseline-first-scan"
    SERVER = "EN"

    def __init__(self):
        self.overrides = {}

    def override(self, **kwargs):
        self.overrides.update(kwargs)


def test_new_goal_first_observation_starts_from_current_remaining(monkeypatch, tmp_path):
    spec = {
        "id": "event-first-scan",
        "server": "EN",
        "currencies": [{"id": 1, "runtime_token": "pt"}],
        "shop_items": [
            {
                "row_id": 11,
                "event_shop_filter": "Chip",
                "price": 15,
                "stock": 100,
                "currency_id": 1,
                "amount": 1,
            }
        ],
    }
    config = FakeConfig()
    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)
    monkeypatch.setattr(
        priority,
        "_selected_targets",
        lambda _config, event_id: {"11": 10} if event_id == spec["id"] else {},
    )

    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)
    runtime = SimpleNamespace(
        group="Chip",
        sub_genre="",
        tier="",
        price=15,
        total_count=100,
        count=90,
        cost="pt",
        amount=1,
    )

    prepared = prepare_event_shop_runtime_items(config, [runtime], root=tmp_path)
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == [runtime]
    assert config.overrides["EventShop_CustomFilter"] == "Chip:10"
    assert state["target_baselines"]["11"] == 90
