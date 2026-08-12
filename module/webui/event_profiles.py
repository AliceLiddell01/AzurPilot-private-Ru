"""Pure state helpers for the WebUI event-profile presentation layer.

The legacy runtime task IDs remain stable. This module only decides which
Event-group entries are visible in WebUI and stores user-facing names for the
optional Event2/Event3 slots inside EventGeneral.Storage.Storage.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping


EVENT_MENU_LABEL = "Ивент"
EVENT_TASK_LABELS = {
    "EventGeneral": "Общие настройки ивента",
    "Event": "Ивентовая карта",
    "EventShop": "Магазин ивента",
}
OPTIONAL_EVENT_PROFILE_SLOTS = ("Event2", "Event3")
OPTIONAL_EVENT_PROFILE_DEFAULT_LABELS = {
    "Event2": "Доп. ивентовый профиль 1",
    "Event3": "Доп. ивентовый профиль 2",
}

# These task IDs are intentionally retained in task/config/runtime. They are
# reusable handlers for uncommon event formats, not dead code. WebUI hides
# them by default and exposes an already-enabled legacy task so the user can
# still reach and disable it safely.
SPECIAL_EVENT_PRESET_TASKS = frozenset(
    {
        "Raid",
        "RaidScuttle",
        "Hospital",
        "Coalition",
        "CoalitionScuttle",
        "MaritimeEscort",
        "WarArchives",
    }
)

_EVENT_PROFILE_STORAGE_KEY = "EventProfileMenu"
_MAX_PROFILE_NAME_LENGTH = 48


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mutable_mapping(value: Any) -> MutableMapping[str, Any] | None:
    return value if isinstance(value, MutableMapping) else None


def scheduler_enabled(config: Mapping[str, Any], task: str) -> bool:
    task_data = _mapping(config.get(task))
    scheduler = _mapping(task_data.get("Scheduler"))
    return scheduler.get("Enable") is True


def _event_general_storage(config: Mapping[str, Any]) -> Mapping[str, Any]:
    event_general = _mapping(config.get("EventGeneral"))
    storage_group = _mapping(event_general.get("Storage"))
    return _mapping(storage_group.get("Storage"))


def event_general_storage_for_display(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return task storage without WebUI-only event-profile metadata."""
    visible = dict(_event_general_storage(config))
    visible.pop(_EVENT_PROFILE_STORAGE_KEY, None)
    return visible


def get_event_profile_metadata(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    storage = _event_general_storage(config)
    raw_profiles = _mapping(storage.get(_EVENT_PROFILE_STORAGE_KEY))

    profiles: dict[str, dict[str, str]] = {}
    for slot in OPTIONAL_EVENT_PROFILE_SLOTS:
        raw = _mapping(raw_profiles.get(slot))
        name = raw.get("name")
        if isinstance(name, str) and name.strip():
            profiles[slot] = {"name": " ".join(name.split())}
        elif slot in raw_profiles:
            profiles[slot] = {"name": OPTIONAL_EVENT_PROFILE_DEFAULT_LABELS[slot]}
    return profiles


def event_task_visible(config: Mapping[str, Any], task: str) -> bool:
    if task in OPTIONAL_EVENT_PROFILE_SLOTS:
        return task in get_event_profile_metadata(config) or scheduler_enabled(config, task)
    if task in SPECIAL_EVENT_PRESET_TASKS:
        return scheduler_enabled(config, task)
    return True


def event_task_label(config: Mapping[str, Any], task: str, fallback: str) -> str:
    if task in EVENT_TASK_LABELS:
        return EVENT_TASK_LABELS[task]
    if task in OPTIONAL_EVENT_PROFILE_SLOTS:
        profile = get_event_profile_metadata(config).get(task)
        if profile is not None:
            return profile["name"]
        return OPTIONAL_EVENT_PROFILE_DEFAULT_LABELS[task]
    return fallback


def normalize_event_profile_name(name: Any) -> str:
    value = " ".join(str(name or "").split())
    if not value:
        raise ValueError("Название профиля не может быть пустым.")
    if len(value) > _MAX_PROFILE_NAME_LENGTH:
        raise ValueError(
            f"Название профиля не должно быть длиннее {_MAX_PROFILE_NAME_LENGTH} символов."
        )
    return value


def validate_event_profile_name(
    config: Mapping[str, Any], name: Any, *, current_slot: str | None = None
) -> str | None:
    try:
        value = normalize_event_profile_name(name)
    except ValueError as exc:
        return str(exc)

    reserved = {label.casefold() for label in EVENT_TASK_LABELS.values()}
    if value.casefold() in reserved:
        return "Это название уже используется постоянным пунктом раздела «Ивент»."

    for slot, profile in get_event_profile_metadata(config).items():
        if slot == current_slot:
            continue
        if profile["name"].casefold() == value.casefold():
            return "Дополнительный ивентовый профиль с таким названием уже существует."
    return None


def next_available_event_profile_slot(config: Mapping[str, Any]) -> str | None:
    profiles = get_event_profile_metadata(config)
    for slot in OPTIONAL_EVENT_PROFILE_SLOTS:
        if slot not in profiles and not scheduler_enabled(config, slot):
            return slot
    return None


def _ensure_profile_storage(config: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    event_general = _mutable_mapping(config.get("EventGeneral"))
    if event_general is None:
        event_general = {}
        config["EventGeneral"] = event_general

    storage_group = _mutable_mapping(event_general.get("Storage"))
    if storage_group is None:
        storage_group = {}
        event_general["Storage"] = storage_group

    storage = _mutable_mapping(storage_group.get("Storage"))
    if storage is None:
        storage = {}
        storage_group["Storage"] = storage

    profiles = _mutable_mapping(storage.get(_EVENT_PROFILE_STORAGE_KEY))
    if profiles is None:
        profiles = {}
        storage[_EVENT_PROFILE_STORAGE_KEY] = profiles
    return profiles


def add_event_profile(config: MutableMapping[str, Any], name: Any) -> str:
    slot = next_available_event_profile_slot(config)
    if slot is None:
        raise ValueError("Можно добавить не более двух дополнительных ивентовых профилей.")

    error = validate_event_profile_name(config, name)
    if error is not None:
        raise ValueError(error)

    profiles = _ensure_profile_storage(config)
    profiles[slot] = {"name": normalize_event_profile_name(name)}
    return slot


def rename_event_profile(config: MutableMapping[str, Any], slot: str, name: Any) -> None:
    if slot not in OPTIONAL_EVENT_PROFILE_SLOTS:
        raise ValueError(f"Неизвестный слот дополнительного ивентового профиля: {slot}")
    if not event_task_visible(config, slot):
        raise ValueError("Дополнительный ивентовый профиль не существует.")

    error = validate_event_profile_name(config, name, current_slot=slot)
    if error is not None:
        raise ValueError(error)

    profiles = _ensure_profile_storage(config)
    profiles[slot] = {"name": normalize_event_profile_name(name)}


def delete_event_profile(config: MutableMapping[str, Any], slot: str) -> None:
    if slot not in OPTIONAL_EVENT_PROFILE_SLOTS:
        raise ValueError(f"Неизвестный слот дополнительного ивентового профиля: {slot}")

    profiles = _ensure_profile_storage(config)
    profiles.pop(slot, None)

    task_data = _mutable_mapping(config.get(slot))
    if task_data is None:
        task_data = {}
        config[slot] = task_data
    scheduler = _mutable_mapping(task_data.get("Scheduler"))
    if scheduler is None:
        scheduler = {}
        task_data["Scheduler"] = scheduler
    scheduler["Enable"] = False
