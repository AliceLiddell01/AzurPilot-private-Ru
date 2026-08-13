"""WebUI mixin: immutable datamine facts plus mutable user policy."""

from __future__ import annotations

from module.event_datamine.artifact import load_builtin_artifact
from module.logger import logger
from module.webui.app_dependencies import current_time, deep_get, toast
from module.webui.app_helpers import is_demo_mode
from module.webui.event_observation import dashboard_pt_observation
from module.webui.event_source import (
    load_event_plan_from_artifact,
    load_event_user_state,
    resolve_current_event_artifact,
    save_event_user_state,
)


class EventDatamineMixin:
    def _event_plan(self):
        config = self.alas_config.read_file(self.alas_name)
        now = current_time()
        if is_demo_mode():
            artifact = load_builtin_artifact("rose_tower.json")
        else:
            artifact, unavailable = resolve_current_event_artifact(
                server="EN", now=now
            )
            if artifact is None:
                assert unavailable is not None
                return unavailable
        spec = artifact["event_spec"]
        revision = str(spec.get("provenance", {}).get("revision") or "")
        dashboard_observation = dashboard_pt_observation(
            instance=self.alas_name,
            event_id=str(spec.get("id") or ""),
            server=str(spec.get("server") or "EN"),
            source_revision=revision,
            value=deep_get(config, "Dashboard.Pt.Value", None),
            recorded_at=deep_get(config, "Dashboard.Pt.Record", ""),
            now=now,
        )
        return load_event_plan_from_artifact(
            self.alas_name, artifact, dashboard_observation
        )

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
