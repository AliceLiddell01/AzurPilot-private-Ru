"""Обновление обзорной панели WebUI."""

from time import monotonic

from module.webui.app_dependencies import (
    Function,
    LogRes,
    clear,
    current_time,
    datetime,
    deep_get,
    get_dashboard_scope_id,
    get_group_scope_id,
    logger,
    put_button,
    put_column,
    put_html,
    put_row,
    put_scope,
    put_text,
    re,
    t,
    time_delta,
    use_scope,
)
from module.webui.app_helpers import timedelta_to_text
from module.webui.app_types import WebUIMixinBase
from module.webui.event_source import load_current_event_plan

_EVENT_CURRENCY_BALANCE_GROUP = "EventCurrencyBalance"
_EVENT_PT_TOTAL_LABEL_KEY = "Gui.Dashboard.EventPtTotal"
_EVENT_CURRENCY_BALANCE_LABEL_KEY = "Gui.Dashboard.EventCurrencyBalance"
_EVENT_CURRENCY_BALANCE_CACHE_TTL_SECONDS = 5.0


def _empty_event_currency_balance_group():
    """Вернуть безопасное неизвестное значение текущего баланса ивента."""

    return {"Value": None, "Record": None, "Color": "^00BFFF"}


def _event_currency_balance_group(config):
    """Сформировать строку Dashboard из текущего доказанного баланса EventShop."""

    plan = load_current_event_plan(
        str(getattr(config, "config_name", "") or ""),
        server=str(getattr(config, "SERVER", "EN") or "EN").upper(),
    )
    progress = plan.get("progress", {}) if isinstance(plan, dict) else {}
    value = progress.get("current_pt") if isinstance(progress, dict) else None
    status = str(progress.get("status") or "") if isinstance(progress, dict) else ""
    observed_at = (
        str(progress.get("observed_at") or "") if isinstance(progress, dict) else ""
    )

    if status != "observed" or not isinstance(value, int) or value < 0:
        return _empty_event_currency_balance_group()

    try:
        record = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        record = None
    return {"Value": value, "Record": record, "Color": "^00BFFF"}


def _dashboard_group_label(group_name):
    """Вернуть подпись строки без смешения накопительного PT и текущего баланса."""

    if group_name == "Pt":
        return t(_EVENT_PT_TOTAL_LABEL_KEY)
    if group_name == _EVENT_CURRENCY_BALANCE_GROUP:
        return t(_EVENT_CURRENCY_BALANCE_LABEL_KEY)
    return t(f"Gui.Dashboard.{group_name}")


def _dashboard_groups_with_event_balance(groups):
    """Вставить текущий баланс сразу после накопительного PT."""

    result = []
    for group_name in groups:
        result.append(group_name)
        if group_name == "Pt":
            result.append(_EVENT_CURRENCY_BALANCE_GROUP)
    return result


class DashboardMixin(WebUIMixinBase):
    """Обновлять задачи и ресурсы на обзорной панели WebUI."""

    def _event_currency_balance_group_cached(self):
        """Получить баланс ивента без чтения состояния на каждом тике Dashboard."""

        config = self.alas_config
        cache_key = (
            str(getattr(config, "config_name", "") or ""),
            str(getattr(config, "SERVER", "EN") or "EN").upper(),
        )
        loaded_at = monotonic()
        cache = getattr(self, "_event_currency_balance_cache", None)
        if isinstance(cache, dict):
            cached_key = cache.get("key")
            cached_at = cache.get("loaded_at")
            cached_group = cache.get("group")
            if (
                cached_key == cache_key
                and isinstance(cached_at, (int, float))
                and loaded_at - cached_at < _EVENT_CURRENCY_BALANCE_CACHE_TTL_SECONDS
                and isinstance(cached_group, dict)
            ):
                return cached_group

        try:
            group = _event_currency_balance_group(config)
        except Exception as exc:
            logger.warning(
                f"[Dashboard] Не удалось получить текущий баланс валюты ивента: {exc}"
            )
            group = _empty_event_currency_balance_group()

        self._event_currency_balance_cache = {
            "key": cache_key,
            "loaded_at": loaded_at,
            "group": group,
        }
        return group

    def alas_update_overview_task(self) -> None:
        if not self.visible:
            return
        self.alas_config.load()
        self.alas_config.get_next_task()

        if len(self.alas_config.pending_task) >= 1:
            if self.alas.alive:
                running = self.alas_config.pending_task[:1]
                pending = self.alas_config.pending_task[1:]
            else:
                running = []
                pending = self.alas_config.pending_task[:]
        else:
            running = []
            pending = []
        waiting = self.alas_config.waiting_task

        snapshot = {
            "running": tuple((task.command, task.next_run) for task in running),
            "pending": tuple((task.command, task.next_run) for task in pending),
            "waiting": tuple((task.command, task.next_run) for task in waiting),
            "alive": self.alas.alive,
        }
        if self._overview_snapshot == snapshot:
            return
        self._overview_snapshot = snapshot

        def put_task(func: Function):
            with use_scope(f"overview-task_{func.command}"):
                put_column(
                    [
                        put_text(t(f"Task.{func.command}.name")).style("--arg-title--"),
                        put_text(str(func.next_run)).style("--arg-help--"),
                    ],
                    size="auto auto",
                )
                put_button(
                    label=t("Gui.Button.Setting"),
                    onclick=lambda: self.alas_set_group(func.command),
                    color="off",
                )

        clear("running_tasks")
        clear("pending_tasks")
        clear("waiting_tasks")
        with use_scope("running_tasks"):
            if running:
                for task in running:
                    put_task(task)
            else:
                put_text(t("Gui.Overview.NoTask")).style("--overview-notask-text--")
        with use_scope("pending_tasks"):
            if pending:
                for task in pending:
                    put_task(task)
            else:
                put_text(t("Gui.Overview.NoTask")).style("--overview-notask-text--")
        with use_scope("waiting_tasks"):
            if waiting:
                for task in waiting:
                    put_task(task)
            else:
                put_text(t("Gui.Overview.NoTask")).style("--overview-notask-text--")

    def _update_dashboard(self, num=None, groups_to_display=None):
        x = 0
        _num = 10000 if num is None else num
        _arg_group = (
            self._log.dashboard_arg_group
            if groups_to_display is None
            else groups_to_display
        )
        _arg_group = _dashboard_groups_with_event_balance(_arg_group)
        time_now = current_time().replace(microsecond=0)
        for group_name in _arg_group:
            if group_name == _EVENT_CURRENCY_BALANCE_GROUP:
                group = self._event_currency_balance_group_cached()
            else:
                group = LogRes(self.alas_config).group(group_name)
            if group is None:
                continue

            value = str(group["Value"])
            value_total = ""
            if "Limit" in group.keys():
                value_limit = f" / {group['Limit']}"
            elif "Total" in group.keys():
                value_total = f" ({group['Total']})"
                value_limit = ""
            elif group_name == "Pt":
                value_limit = " / " + re.sub(
                    r'[,.\'"，。]',
                    "",
                    str(
                        deep_get(
                            self.alas_config.data, "EventGeneral.EventGeneral.PtLimit"
                        )
                    ),
                )
                if value_limit == " / 0":
                    value_limit = ""
            else:
                value_limit = ""
                value_total = ""

            value_time = group["Record"]
            if value_time is None or value_time == datetime(2020, 1, 1, 0, 0, 0):
                value_time = datetime(2023, 1, 1, 0, 0, 0)

            # Нормализуем временную зону синтетического наблюдения к часам Dashboard.
            if value_time != datetime(2023, 1, 1, 0, 0, 0):
                if value_time.tzinfo is not None and time_now.tzinfo is None:
                    value_time = value_time.astimezone().replace(tzinfo=None)
                elif value_time.tzinfo is None and time_now.tzinfo is not None:
                    value_time = value_time.replace(tzinfo=time_now.tzinfo)
                elif value_time.tzinfo is not None and time_now.tzinfo is not None:
                    value_time = value_time.astimezone(time_now.tzinfo)

            # Формируем давность данных; неизвестное значение явно показываем как отсутствие данных.
            if value_time == datetime(2023, 1, 1, 0, 0, 0):
                value = t("Gui.Dashboard.NoData")
                delta = timedelta_to_text()
            else:
                delta = timedelta_to_text(time_delta(value_time - time_now))

            if group_name not in self._log.last_display_time.keys():
                self._log.last_display_time[group_name] = ""
            if (
                self._log.last_display_time[group_name] == delta
                and not self._log.first_display
            ):
                continue
            self._log.last_display_time[group_name] = delta

            value_limit = "" if value == t("Gui.Dashboard.NoData") else value_limit
            value_total = "" if value == t("Gui.Dashboard.NoData") else value_total
            limit_style = (
                "--dashboard-limit--" if value_limit else "--dashboard-total--"
            )
            value_limit = value_limit if value_limit else value_total

            # Старые профили могут не содержать цвет; это не должно прерывать Dashboard.
            color_value = deep_get(group, "Color") or ""
            _color = f"background-color:{color_value.replace('^', '#')}"
            color = f'<div class="status-point" style={_color}>'
            scope_id = get_dashboard_scope_id(group_name)
            with use_scope(scope_id, clear=True):
                put_row(
                    [
                        put_html(color),
                        put_scope(
                            get_group_scope_id(group_name),
                            [
                                put_column(
                                    [
                                        put_row(
                                            [
                                                put_text(value).style(
                                                    "--dashboard-value--"
                                                ),
                                                put_text(value_limit).style(
                                                    limit_style
                                                ),
                                            ],
                                        ).style(
                                            "grid-template-columns:min-content auto;align-items: baseline;"
                                        ),
                                        put_text(
                                            _dashboard_group_label(group_name)
                                            + " - "
                                            + delta
                                        ).style("---dashboard-help--"),
                                    ],
                                    size="auto auto",
                                ),
                            ],
                        ),
                    ],
                    size="20px 1fr",
                ).style("height: 1fr")
            x += 1
            if x >= _num:
                break
        if self._log.first_display:
            self._log.first_display = False

    def alas_update_dashboard(self, _clear=False):
        if not self.visible:
            return
        with use_scope("dashboard", clear=_clear):
            if not self._log.display_dashboard:
                self._update_dashboard(
                    num=5, groups_to_display=["Oil", "Coin", "Gem", "Pt"]
                )
            elif self._log.display_dashboard:
                self._update_dashboard()
