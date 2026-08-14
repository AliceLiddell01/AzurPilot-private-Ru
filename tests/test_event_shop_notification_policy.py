from module.shop_event.notification_policy import apply_event_shop_notification_policy


class FakeConfig:
    def __init__(self, push_on_error):
        self.push_on_error = push_on_error
        self.overrides = {}

    def cross_get(self, *, keys, default=False):
        assert keys == "EventShop.Scheduler.PushNotification"
        return self.push_on_error if self.push_on_error is not None else default

    def override(self, **kwargs):
        self.overrides.update(kwargs)


def test_event_shop_push_enabled_is_error_only():
    config = FakeConfig(True)

    enabled = apply_event_shop_notification_policy(config)

    assert enabled is True
    assert config.overrides["Scheduler_PushNotification"] is False
    assert "Error_OnePushConfig" not in config.overrides


def test_event_shop_push_disabled_suppresses_error_transport_too():
    config = FakeConfig(False)

    enabled = apply_event_shop_notification_policy(config)

    assert enabled is False
    assert config.overrides["Scheduler_PushNotification"] is False
    assert config.overrides["Error_OnePushConfig"] == "provider: null"
