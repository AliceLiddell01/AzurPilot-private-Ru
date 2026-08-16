"""Живое обновление runtime-состояния открытой страницы EventShop."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from module.config.deep import deep_get
from module.webui.app_dependencies import logger, run_js
from module.webui.app_types import WebUIMixinBase
from module.webui.event_shop_priority import load_event_shop_priority
from module.webui.event_source import load_event_user_state


_EVENT_SHOP_LIVE_INTERVAL = 2
_EVENT_SHOP_LIVE_PRIORITY_FIELDS = (
    "priorities",
    "purchased",
    "completed",
    "remaining",
    "target_baselines",
    "blocked",
    "pending",
)


class EventShopLiveMixin(WebUIMixinBase):
    """Обновлять PT и подтверждённое состояние магазина без перезагрузки страницы."""

    @staticmethod
    def _event_shop_live_plan_fingerprint(
        user_state: Mapping[str, Any],
        priority_state: Mapping[str, Any],
    ) -> str:
        selections = user_state.get("shop_selections")
        if not isinstance(selections, Mapping):
            selections = {}
        payload = {
            "source_event_id": str(user_state.get("source_event_id") or ""),
            "shop_selections": {
                str(row_id): value for row_id, value in selections.items()
            },
            "priority": {
                field: priority_state.get(field)
                for field in _EVENT_SHOP_LIVE_PRIORITY_FIELDS
            },
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _read_event_shop_live_state(self, event_id: str) -> dict[str, Any]:
        config = self.alas_config.read_file(self.alas_name)
        user_state = load_event_user_state(self.alas_name)
        priority_state = load_event_shop_priority(self.alas_name, event_id)
        return {
            "pt": deep_get(config, "Dashboard.Pt.Value", None),
            "plan": self._event_shop_live_plan_fingerprint(
                user_state,
                priority_state,
            ),
        }

    @staticmethod
    def _event_shop_live_balance_text(value: Any) -> str:
        if value is None:
            return "—"
        try:
            return f"{int(value):,}".replace(",", " ")
        except (TypeError, ValueError, OverflowError):
            return str(value)

    def _patch_event_shop_live_balance(self, value: Any) -> None:
        payload = json.dumps(
            self._event_shop_live_balance_text(value),
            ensure_ascii=False,
        )
        run_js(
            """
((value) => {
  const node = document.querySelector(".event-shop-v2-balance b");
  if (!node || node.textContent === value) return;
  node.textContent = value;
  node.classList.remove("event-shop-value-updated");
  requestAnimationFrame(() => {
    node.classList.add("event-shop-value-updated");
    window.setTimeout(
      () => node.classList.remove("event-shop-value-updated"),
      220,
    );
  });
})(%s);
"""
            % payload
        )

    def _remember_event_shop_live_state(self, event_id: str | None = None) -> None:
        event_id = str(
            event_id
            or getattr(self, "_event_shop_live_event_id", "")
            or ""
        )
        if not event_id:
            return
        try:
            self._event_shop_live_state = self._read_event_shop_live_state(event_id)
            self._event_shop_live_error = ""
        except (OSError, TypeError, ValueError) as exc:
            message = str(exc)
            if getattr(self, "_event_shop_live_error", "") != message:
                logger.warning(
                    f"[WebUI — магазин события] Не удалось запомнить live-state: {exc}"
                )
            self._event_shop_live_error = message

    def _event_shop_live_refresh(self) -> None:
        if not getattr(self, "visible", False):
            return
        if getattr(self, "page", "") != "EventShop":
            return
        if getattr(self, "_event_plan_active_task", "") != "EventShop":
            return
        event_id = str(getattr(self, "_event_shop_live_event_id", "") or "")
        if not event_id:
            return

        try:
            current = self._read_event_shop_live_state(event_id)
            self._event_shop_live_error = ""
        except (OSError, TypeError, ValueError) as exc:
            message = str(exc)
            if getattr(self, "_event_shop_live_error", "") != message:
                logger.warning(
                    f"[WebUI — магазин события] Live-refresh временно недоступен: {exc}"
                )
            self._event_shop_live_error = message
            return

        previous = getattr(self, "_event_shop_live_state", None)
        if not isinstance(previous, Mapping):
            self._event_shop_live_state = current
            return

        if current["plan"] != previous.get("plan"):
            self._event_shop_live_state = current
            self._refresh_event_plan_page()
            return

        if current["pt"] != previous.get("pt"):
            self._event_shop_live_state = current
            self._patch_event_shop_live_balance(current["pt"])

    def alas_set_group(self, task: str) -> None:
        super().alas_set_group(task)
        if task != "EventShop":
            return

        try:
            plan = self._event_plan()
            event = plan.get("event", {})
            event_id = str(event.get("id") or "") if isinstance(event, Mapping) else ""
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                f"[WebUI — магазин события] Не удалось запустить live-refresh: {exc}"
            )
            return
        if not event_id:
            return

        self._event_shop_live_event_id = event_id
        self._remember_event_shop_live_state(event_id)
        self.task_handler.add(
            self._event_shop_live_refresh,
            _EVENT_SHOP_LIVE_INTERVAL,
            True,
        )

    def _patch_event_shop_priority_values(
        self,
        *,
        event_id: str,
        row_id: str,
        live_key: str,
    ) -> None:
        super()._patch_event_shop_priority_values(
            event_id=event_id,
            row_id=row_id,
            live_key=live_key,
        )
        self._remember_event_shop_live_state(event_id)

    def _patch_event_shop_plan_values(
        self,
        identity: tuple[str, str, str, int, int],
        snapshot: Mapping[str, int],
    ) -> None:
        super()._patch_event_shop_plan_values(identity, snapshot)
        self._remember_event_shop_live_state()
