from pathlib import Path
from unittest.mock import patch

import pytest

from module.webui import app_event_profiles
from module.webui.app_event_profiles import EventProfilesMixin
from module.webui.event_profiles import (
    add_event_profile,
    delete_event_profile,
    event_general_storage_for_display,
    event_task_label,
    event_task_visible,
    get_event_profile_metadata,
    next_available_event_profile_slot,
    rename_event_profile,
)


def _config():
    return {
        "EventGeneral": {"Storage": {"Storage": {}}},
        "Event2": {
            "Scheduler": {"Enable": False},
            "Campaign": {"Name": "d3"},
        },
        "Event3": {
            "Scheduler": {"Enable": False},
            "Campaign": {"Name": "sp"},
        },
        "Raid": {"Scheduler": {"Enable": False}},
        "WarArchives": {"Scheduler": {"Enable": False}},
    }


def test_default_event_menu_keeps_only_regular_entries_visible():
    config = _config()

    assert event_task_visible(config, "EventGeneral") is True
    assert event_task_visible(config, "Event") is True
    assert event_task_visible(config, "EventShop") is True
    assert event_task_visible(config, "Event2") is False
    assert event_task_visible(config, "Event3") is False
    assert event_task_visible(config, "Raid") is False
    assert event_task_visible(config, "WarArchives") is False


def test_enabled_legacy_tasks_remain_visible_for_safe_disable():
    config = _config()
    config["Event2"]["Scheduler"]["Enable"] = True
    config["Raid"]["Scheduler"]["Enable"] = True

    assert event_task_visible(config, "Event2") is True
    assert event_task_visible(config, "Raid") is True
    assert event_task_label(config, "Event2", "legacy") == "Доп. ивентовый профиль 1"


def test_add_rename_delete_profile_keeps_stable_task_id():
    config = _config()

    slot = add_event_profile(config, "  Фарм   D3  ")
    assert slot == "Event2"
    assert get_event_profile_metadata(config) == {"Event2": {"name": "Фарм D3"}}
    assert event_task_label(config, "Event2", "legacy") == "Фарм D3"

    rename_event_profile(config, slot, "Прокачка флота")
    assert get_event_profile_metadata(config)["Event2"]["name"] == "Прокачка флота"

    config["Event2"]["Scheduler"]["Enable"] = True
    delete_event_profile(config, slot)
    assert get_event_profile_metadata(config) == {}
    assert config["Event2"]["Scheduler"]["Enable"] is False
    assert event_task_visible(config, "Event2") is False


def test_delete_profile_preserves_slot_settings_for_reuse():
    config = _config()
    slot = add_event_profile(config, "Фарм D3")

    delete_event_profile(config, slot)

    assert config["Event2"]["Campaign"]["Name"] == "d3"


def test_only_two_optional_profiles_are_available():
    config = _config()

    assert add_event_profile(config, "Первый") == "Event2"
    assert add_event_profile(config, "Второй") == "Event3"
    assert next_available_event_profile_slot(config) is None

    with pytest.raises(ValueError, match="не более двух"):
        add_event_profile(config, "Третий")


def test_duplicate_and_reserved_profile_names_are_rejected():
    config = _config()
    add_event_profile(config, "Фарм D3")

    with pytest.raises(ValueError, match="уже существует"):
        add_event_profile(config, "фарм d3")

    with pytest.raises(ValueError, match="постоянным пунктом"):
        add_event_profile(config, "Ивентовая карта")


def test_profile_metadata_is_hidden_from_visible_task_storage():
    config = _config()
    add_event_profile(config, "Фарм D3")

    assert event_general_storage_for_display(config) == {}

    config["EventGeneral"]["Storage"]["Storage"]["RuntimeState"] = {
        "last_check": "2026-08-12 22:00:00"
    }

    assert event_general_storage_for_display(config) == {
        "RuntimeState": {"last_check": "2026-08-12 22:00:00"}
    }


class ProfilePopupProbe(EventProfilesMixin):
    def _ensure_event_profile_styles(self):
        pass


def test_event_profile_editor_uses_scoped_non_blocking_popup():
    css = Path("assets/gui/css/event-profiles-alas.css").read_text(encoding="utf-8")
    callback = object()
    probe = ProfilePopupProbe()

    with patch.object(
        app_event_profiles, "put_html", return_value="marker"
    ) as put_html, patch.object(
        app_event_profiles, "put_input", return_value="name-input"
    ) as put_input, patch.object(
        app_event_profiles, "put_buttons", return_value="actions"
    ) as put_buttons, patch.object(app_event_profiles, "popup") as popup:
        probe._event_profile_name_popup(
            title="Профиль",
            label="Имя",
            confirm_label="Сохранить",
            confirm_callback=callback,
            value="Текущий",
            placeholder="Новый",
        )

    put_html.assert_called_once_with(
        '<span class="event-profile-dialog-marker" aria-hidden="true"></span>'
    )
    put_input.assert_called_once_with(
        "event_profile_name",
        label="Имя",
        value="Текущий",
        placeholder="Новый",
    )
    assert put_buttons.call_args.kwargs["onclick"][0] is callback
    popup.assert_called_once_with(
        "Профиль",
        ["marker", "name-input", "actions"],
        closable=True,
        implicit_close=True,
    )

    assert "#input-container" not in css
    assert ".modal-content:has(.event-profile-dialog-marker)" in css
