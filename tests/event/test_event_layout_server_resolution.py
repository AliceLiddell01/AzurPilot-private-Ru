import module.webui.app_event_layout as layout_module
from module.config.server import GLOBAL_PACKAGE
from module.webui.app_event_layout import EventLayoutMixin


def _config(package_name: str, selector: str = "event_test") -> dict:
    return {
        "Alas": {"Emulator": {"PackageName": package_name}},
        "Event": {"Campaign": {"Event": selector}},
    }


class _Layout(EventLayoutMixin):
    ALAS_ARGS = {
        "Event": {
            "Campaign": {
                "Event": {
                    "display": "show",
                    "option": [],
                    "option_en": ["event_test"],
                }
            }
        }
    }

    def _current_event_name(self, config):
        return "Тестовое событие"


def test_unknown_package_fails_closed_before_event_artifact_resolution(monkeypatch):
    layout = EventLayoutMixin()
    config = _config("com.example.unsupported")
    monkeypatch.setattr(layout_module, "is_demo_mode", lambda: False)
    monkeypatch.setattr(
        layout_module,
        "resolve_current_event_artifact",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Event artifact resolver не должен вызываться")
        ),
    )

    assert layout._event_server(config) is None
    assert layout._current_event_name(config) is None


def test_unknown_package_does_not_hide_event_selector():
    layout = _Layout()
    config = _config("com.example.unsupported")

    task_args, returned_config, event_name = layout._prepare_event_map_args(
        "Event", config
    )

    assert returned_config is config
    assert event_name is None
    assert task_args["Campaign"]["Event"]["display"] == "show"


def test_supported_selected_event_is_hidden_only_in_copied_args():
    layout = _Layout()
    config = _config(GLOBAL_PACKAGE)

    task_args, returned_config, event_name = layout._prepare_event_map_args(
        "Event", config
    )

    assert returned_config is config
    assert event_name == "Тестовое событие"
    assert task_args["Campaign"]["Event"]["display"] == "hide"
    assert layout.ALAS_ARGS["Event"]["Campaign"]["Event"]["display"] == "show"
