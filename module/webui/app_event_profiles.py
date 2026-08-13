"""WebUI presentation for the cleaned Event menu and optional event profiles."""

from __future__ import annotations

from functools import partial
from typing import Any, Mapping, MutableMapping

from module.webui.app_dependencies import (
    close_popup,
    logger,
    pin,
    popup,
    put_button,
    put_buttons,
    put_collapse,
    put_html,
    put_input,
    put_row,
    put_scope,
    put_text,
    run_js,
    t,
    toast,
    use_scope,
)
from module.webui.app_helpers import is_demo_mode
from module.webui.app_types import WebUIMixinBase
from module.webui.event_config import mutate_event_config
from module.webui.event_profiles import (
    EVENT_MENU_LABEL,
    OPTIONAL_EVENT_PROFILE_SLOTS,
    add_event_profile,
    delete_event_profile,
    event_general_storage_for_display,
    event_task_label,
    event_task_visible,
    get_event_profile_metadata,
    next_available_event_profile_slot,
    rename_event_profile,
)


_EVENT_PROFILE_STYLE_ID = "azurpilot-event-profile-styles"
_EVENT_PROFILE_STYLE_HREF = "/static/assets/gui/css/event-profiles-alas.css"
_EVENT_PROFILE_MANAGER_SCOPE = "group_EventProfiles"
_EVENT_PROFILE_NAME_PIN = "event_profile_name"


class EventProfilesMixin(WebUIMixinBase):
    """Keep legacy event task IDs stable while presenting a compact Event UI."""

    def _ensure_event_profile_styles(self) -> None:
        run_js(
            f"""
            (function() {{
                if (document.getElementById('{_EVENT_PROFILE_STYLE_ID}')) return;
                const link = document.createElement('link');
                link.id = '{_EVENT_PROFILE_STYLE_ID}';
                link.rel = 'stylesheet';
                link.href = '{_EVENT_PROFILE_STYLE_HREF}';
                document.head.appendChild(link);
            }})();
            """
        )

    def _read_event_profile_config(self) -> MutableMapping[str, Any]:
        config = self.alas_config.read_file(self.alas_name)
        if not isinstance(config, MutableMapping):
            raise ValueError("Корневой элемент конфигурации должен быть объектом.")
        return config

    @staticmethod
    def _event_menu_task_label(
        config: Mapping[str, Any], task: str, fallback: str
    ) -> str:
        return event_task_label(config, task, fallback)

    def _render_event_aware_menu(self) -> None:
        """Render the current task menu without changing the active content page."""
        self._ensure_event_profile_styles()

        with use_scope("menu", clear=True):
            put_buttons(
                [
                    {
                        "label": t("Gui.MenuAlas.Overview"),
                        "value": "Overview",
                        "color": "menu",
                    }
                ],
                onclick=[self.alas_overview],
            ).style("--menu-Overview--")

            try:
                event_config = self._read_event_profile_config()
            except Exception as exc:
                logger.exception(exc)
                event_config = None

            for menu, task_data in self.ALAS_MENU.items():
                if task_data.get("page") == "tool":
                    _onclick = self.alas_daemon_overview
                else:
                    _onclick = self.alas_set_group

                tasks = list(task_data.get("tasks", []))
                if menu == "Event" and event_config is not None:
                    tasks = [
                        task for task in tasks if event_task_visible(event_config, task)
                    ]

                def task_label(task: str) -> str:
                    fallback = t(f"Task.{task}.name")
                    if menu == "Event" and event_config is not None:
                        return self._event_menu_task_label(
                            event_config, task, fallback
                        )
                    return fallback

                if task_data.get("menu") == "collapse":
                    task_btn_list = [
                        put_buttons(
                            [
                                {
                                    "label": task_label(task),
                                    "value": task,
                                    "color": "menu",
                                }
                            ],
                            onclick=_onclick,
                        ).style(f"--menu-{task}--")
                        for task in tasks
                    ]
                    title = (
                        EVENT_MENU_LABEL if menu == "Event" else t(f"Menu.{menu}.name")
                    )
                    put_collapse(title=title, content=task_btn_list)
                else:
                    title = (
                        EVENT_MENU_LABEL if menu == "Event" else t(f"Menu.{menu}.name")
                    )
                    put_html(
                        '<div class="hr-task-group-box">'
                        '<span class="hr-task-group-line"></span>'
                        f'<span class="hr-task-group-text">{title}</span>'
                        '<span class="hr-task-group-line"></span>'
                        "</div>"
                    )
                    for task in tasks:
                        put_buttons(
                            [
                                {
                                    "label": task_label(task),
                                    "value": task,
                                    "color": "menu",
                                }
                            ],
                            onclick=_onclick,
                        ).style(f"--menu-{task}--").style("padding-left: 0.75rem")

    def alas_set_menu(self) -> None:
        """Render the normal menu, with a compact presentation for Event tasks."""
        self._render_event_aware_menu()
        self.alas_overview()

    def set_group(self, group, arg_dict, config, task: str) -> int:
        """Hide WebUI-only profile metadata from the generic task-status widget."""
        if task == "EventGeneral" and group and group[0] == "Storage":
            visible_storage = event_general_storage_for_display(config)
            if not visible_storage:
                return 0

            display_config = dict(config)
            event_general = dict(config.get("EventGeneral", {}))
            storage_group = dict(event_general.get("Storage", {}))
            storage_group["Storage"] = visible_storage
            event_general["Storage"] = storage_group
            display_config["EventGeneral"] = event_general
            return super().set_group(group, arg_dict, display_config, task)

        return super().set_group(group, arg_dict, config, task)

    def alas_set_group(self, task: str) -> None:
        """Render a task page and apply Event-specific display names/actions."""
        super().alas_set_group(task)

        if task not in {
            "EventGeneral",
            "Event",
            "Event2",
            "Event3",
            "EventShop",
        }:
            return

        try:
            config = self._read_event_profile_config()
        except Exception as exc:
            logger.exception(exc)
            return

        fallback = t(f"Task.{task}.name")
        self.set_title(self._event_menu_task_label(config, task, fallback))
        if task == "EventGeneral":
            with use_scope("groups"):
                put_scope(_EVENT_PROFILE_MANAGER_SCOPE)
            with use_scope(_EVENT_PROFILE_MANAGER_SCOPE, clear=True):
                self._render_event_profile_manager(config)

    def _mutate_event_profile_config(self, mutation, verify, success_message: str):
        """Serialize profile CRUD and verify the exact fields written to disk."""
        if is_demo_mode():
            toast(
                "В демонстрационном режиме изменение профилей отключено.",
                color="warning",
            )
            return None
        try:
            result = mutate_event_config(
                self.alas_config,
                self.alas_name,
                mutation,
                verify=verify,
            )
            self.alas_config.load()
        except ValueError as exc:
            toast(str(exc), color="error")
            return None
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось сохранить ивентовые профили: {exc}", color="error")
            return None
        logger.info(f"[WebUI — Ивент] {success_message}")
        toast(success_message, color="success")
        return result

    def _refresh_event_profile_ui(self) -> None:
        """Refresh only Event-owned scopes after a profile mutation."""
        try:
            config = self._read_event_profile_config()
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось обновить интерфейс профилей: {exc}", color="error")
            return

        self._render_event_aware_menu()
        self.active_button("menu", "EventGeneral")
        with use_scope(_EVENT_PROFILE_MANAGER_SCOPE, clear=True):
            self._render_event_profile_manager(config)

    def _event_profile_name_popup(
        self,
        *,
        title: str,
        label: str,
        confirm_label: str,
        confirm_callback,
        value: str = "",
        placeholder: str = "",
    ) -> None:
        """Show a scoped non-blocking modal instead of PyWebIO's global input form."""
        self._ensure_event_profile_styles()
        popup(
            title,
            [
                put_html('<span class="event-profile-dialog-marker" aria-hidden="true"></span>'),
                put_input(
                    _EVENT_PROFILE_NAME_PIN,
                    label=label,
                    value=value,
                    placeholder=placeholder,
                ),
                put_buttons(
                    [
                        {
                            "label": confirm_label,
                            "value": "confirm",
                            "color": "primary",
                        },
                        {
                            "label": "Отмена",
                            "value": "cancel",
                            "color": "light",
                        },
                    ],
                    onclick=[confirm_callback, close_popup],
                ),
            ],
            closable=True,
            implicit_close=True,
        )

    def _confirm_add_event_profile(self) -> None:
        name = pin[_EVENT_PROFILE_NAME_PIN]

        def mutation(config):
            slot = add_event_profile(config, name)
            return slot, get_event_profile_metadata(config)[slot]["name"]

        def verify(config, result):
            slot, expected_name = result
            return get_event_profile_metadata(config).get(slot, {}).get("name") == expected_name

        if self._mutate_event_profile_config(
            mutation,
            verify,
            "Дополнительный ивентовый профиль добавлен.",
        ) is not None:
            close_popup()
            self._refresh_event_profile_ui()

    def _add_event_profile(self) -> None:
        try:
            config = self._read_event_profile_config()
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось прочитать конфигурацию: {exc}", color="error")
            return

        if next_available_event_profile_slot(config) is None:
            toast(
                "Достигнут лимит: доступно не более двух дополнительных ивентовых профилей.",
                color="warning",
            )
            return

        self._event_profile_name_popup(
            title="Добавить доп. ивентовый профиль",
            label="Название профиля",
            confirm_label="Добавить",
            confirm_callback=self._confirm_add_event_profile,
            placeholder="Например: Фарм D3",
        )

    def _confirm_rename_event_profile(self, slot: str) -> None:
        name = pin[_EVENT_PROFILE_NAME_PIN]

        def mutation(config):
            rename_event_profile(config, slot, name)
            return get_event_profile_metadata(config)[slot]["name"]

        def verify(config, expected_name):
            return get_event_profile_metadata(config).get(slot, {}).get("name") == expected_name

        if self._mutate_event_profile_config(
            mutation,
            verify,
            "Название ивентового профиля изменено.",
        ) is not None:
            close_popup()
            self._refresh_event_profile_ui()

    def _rename_event_profile(self, slot: str) -> None:
        try:
            config = self._read_event_profile_config()
            profiles = get_event_profile_metadata(config)
            current_name = profiles.get(slot, {}).get("name", "")
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось прочитать конфигурацию: {exc}", color="error")
            return

        self._event_profile_name_popup(
            title="Переименовать доп. ивентовый профиль",
            label="Новое название",
            confirm_label="Сохранить",
            confirm_callback=partial(self._confirm_rename_event_profile, slot),
            value=current_name,
        )

    def _confirm_delete_event_profile(self, slot: str) -> None:
        def mutation(config):
            delete_event_profile(config, slot)
            return slot

        def verify(config, deleted_slot):
            return (
                deleted_slot not in get_event_profile_metadata(config)
                and not bool(
                    config.get(deleted_slot, {}).get("Scheduler", {}).get("Enable", False)
                )
            )

        if self._mutate_event_profile_config(
            mutation,
            verify,
            "Дополнительный ивентовый профиль удалён.",
        ) is not None:
            close_popup()
            self._refresh_event_profile_ui()

    def _delete_event_profile(self, slot: str) -> None:
        try:
            config = self._read_event_profile_config()
            profile_name = self._event_menu_task_label(
                config, slot, t(f"Task.{slot}.name")
            )
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось прочитать конфигурацию: {exc}", color="error")
            return

        self._ensure_event_profile_styles()
        popup(
            f"Удалить профиль «{profile_name}»?",
            [
                put_html('<span class="event-profile-dialog-marker" aria-hidden="true"></span>'),
                put_text(
                    "Профиль исчезнет из меню, его задача будет отключена. "
                    "Остальные настройки слота сохранятся для возможного повторного использования."
                ),
                put_buttons(
                    [
                        {
                            "label": "Удалить",
                            "value": "confirm",
                            "color": "danger",
                        },
                        {
                            "label": "Отмена",
                            "value": "cancel",
                            "color": "light",
                        },
                    ],
                    onclick=[
                        partial(self._confirm_delete_event_profile, slot),
                        close_popup,
                    ],
                ),
            ],
            closable=True,
            implicit_close=True,
        )

    def _render_event_profile_manager(self, config: Mapping[str, Any]) -> None:
        profiles = get_event_profile_metadata(config)

        put_text("Дополнительные ивентовые профили")
        put_text(
            "Основная ивентовая карта доступна всегда. Здесь можно создать до двух "
            "независимых дополнительных профилей с собственными настройками карты, "
            "флотов, морали, ограничений и расписания."
        )
        put_html('<hr class="hr-group">')

        if not profiles:
            put_text("Дополнительные профили не созданы.").style(
                "font-size: .85rem; opacity: .72; margin: .35rem 0;"
            )

        for slot in OPTIONAL_EVENT_PROFILE_SLOTS:
            profile = profiles.get(slot)
            if profile is None:
                continue
            put_row(
                [
                    put_text(profile["name"]),
                    put_buttons(
                        [
                            {
                                "label": "Переименовать",
                                "value": "rename",
                                "color": "primary",
                            },
                            {
                                "label": "Удалить",
                                "value": "delete",
                                "color": "danger",
                            },
                        ],
                        onclick=[
                            partial(self._rename_event_profile, slot),
                            partial(self._delete_event_profile, slot),
                        ],
                    ),
                ],
                size="minmax(0, 1fr) auto",
            ).style("margin: .45rem 0;")

        if next_available_event_profile_slot(config) is not None:
            put_button(
                "Добавить доп. ивентовый профиль",
                onclick=self._add_event_profile,
                color="primary",
                disabled=is_demo_mode(),
            ).style("margin-top: .45rem;")
        else:
            put_text(
                "Достигнут лимит: доступно не более двух дополнительных ивентовых профилей."
            ).style("font-size: .85rem; opacity: .72; margin-top: .45rem;")
