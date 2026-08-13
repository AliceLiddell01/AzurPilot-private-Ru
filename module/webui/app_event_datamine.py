"""WebUI mixin: immutable datamine facts plus mutable user policy."""

from __future__ import annotations

from module.webui.app_dependencies import toast
from module.webui.app_helpers import is_demo_mode
from module.logger import logger
from module.webui.event_source import (
    load_builtin_event_plan,
    load_event_user_state,
    save_event_user_state,
)


class EventDatamineMixin:
    def _event_plan(self):
        return load_builtin_event_plan(self.alas_name)

    def _activate_generated_event_source(self) -> None:
        if is_demo_mode():
            toast(
                "В демонстрационном режиме изменение источника отключено.",
                color="warning",
            )
            return
        try:
            state = load_event_user_state(self.alas_name)
            state["explicit_empty"] = False
            save_event_user_state(self.alas_name, state)
        except (OSError, TypeError, ValueError) as exc:
            logger.exception(exc)
            toast(f"Не удалось выбрать сгенерированный источник: {exc}", color="error")
            return
        toast("Сгенерированный источник события выбран", color="success")
        self._refresh_event_plan_page()
