from contextlib import contextmanager
from unittest.mock import patch

from module.webui.app_event_shop_safety import EventShopSafetyMixin
from module.webui.app_event_shop_v2 import EventShopV2Mixin


class SafetyProbe(EventShopSafetyMixin):
    def __init__(self):
        self.calls = []

    def _render_event_shop_safety_status(self, config):
        self.calls.append(("safety", config))


def test_first_v2_layout_appends_safety_status_to_plan_scope():
    probe = SafetyProbe()
    config = {"EventShop": {"Scheduler": {"Enable": False}}}
    scopes = []

    def fake_layout(self, *, task, group_map, config):
        self.calls.append(("layout", task, group_map, config))

    @contextmanager
    def fake_scope(name, clear=False):
        scopes.append((name, clear))
        yield

    with (
        patch.object(EventShopV2Mixin, "_render_event_shop_layout", fake_layout),
        patch("module.webui.app_event_shop_safety.use_scope", fake_scope),
    ):
        probe._render_event_shop_layout(
            task="EventShop",
            group_map={"Scheduler": object()},
            config=config,
        )

    assert probe.calls[0][0] == "layout"
    assert probe.calls[1] == ("safety", config)
    assert scopes == [("group_EventShopPlan", False)]
