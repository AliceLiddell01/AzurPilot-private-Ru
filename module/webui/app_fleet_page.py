"""Отдельная страница состояния флотов и элементы ручного сканирования."""

from __future__ import annotations

from functools import cached_property
from typing import Any
from zoneinfo import ZoneInfo

from module.application.fleet_manual_scan import (
    FleetManualScanCommand,
    FleetManualScanStatus,
)
from module.application.fleet_page import (
    FleetPageViewModel,
    FleetRowViewModel,
    FleetSlotState,
    FleetSlotViewModel,
)
from module.formation.model import FleetSelection, SUPPORTED_SURFACE_FLEET_INDICES
from module.webui.app_dependencies import (
    ProcessManager,
    logger,
    pin,
    put_button,
    put_buttons,
    put_checkbox,
    put_html,
    put_row,
    put_scope,
    put_table,
    put_text,
    t,
    toast,
    use_scope,
)
from module.webui.app_lifecycle import build_fleet_page_runtime_context
from module.webui.app_types import WebUIMixinBase

_PAGE_NAME = "FleetPage"
_MANUAL_SELECTION_PIN = "FleetPage_ManualSelection"
_REFRESH_SECONDS = 2.0


def format_fleet_timestamp(value, timezone: ZoneInfo) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        raise ValueError("Fleet observed_at должен содержать timezone")
    return value.astimezone(timezone).strftime("%d.%m.%Y %H:%M:%S %Z")


def fleet_slot_text(slot: FleetSlotViewModel) -> str:
    if slot.state is FleetSlotState.EMPTY:
        return t("Gui.FleetPage.SlotEmpty")
    if slot.state is FleetSlotState.MATCHED:
        return slot.canonical_display_name or t("Gui.FleetPage.SlotUnknown")
    displayed = slot.displayed_name or t("Gui.FleetPage.SlotUnknown")
    if slot.state is FleetSlotState.UNRESOLVED:
        return t("Gui.FleetPage.SlotUnresolved", name=displayed)
    return t("Gui.FleetPage.SlotAmbiguous", name=displayed)


class FleetPageMixin(WebUIMixinBase):
    """Возможность страницы флотов, подключаемая к AlasGUI текущей сессии."""

    @cached_property
    def fleet_page_context(self):
        return build_fleet_page_runtime_context(require_ready=False)

    def _put_fleet_menu_button(self) -> None:
        put_buttons(
            [
                {
                    "label": t("Gui.FleetPage.Title"),
                    "value": _PAGE_NAME,
                    "color": "menu",
                }
            ],
            onclick=[self.ui_fleet_page],
        ).style(f"--menu-{_PAGE_NAME}--")

    def _fleet_page_is_current(self, instance: str) -> bool:
        return self.page == _PAGE_NAME and self.alas_name == instance

    @staticmethod
    def _select_all_manual_fleets() -> None:
        pin[_MANUAL_SELECTION_PIN] = list(SUPPORTED_SURFACE_FLEET_INDICES)

    @staticmethod
    def _clear_manual_fleets() -> None:
        pin[_MANUAL_SELECTION_PIN] = []

    def _submit_manual_scan(self, instance: str) -> None:
        if not self._fleet_page_is_current(instance):
            return
        try:
            raw = pin[_MANUAL_SELECTION_PIN]
            if not isinstance(raw, (list, tuple)):
                raise TypeError("Manual Fleet selection должна быть списком")
            selection = FleetSelection(tuple(raw))
        except (TypeError, ValueError):
            toast(t("Gui.FleetPage.InvalidSelection"), color="error")
            return

        try:
            submission = self.fleet_page_context.command_service.submit(
                instance,
                selection,
            )
        except Exception as exc:
            logger.exception(exc)
            toast(t("Gui.FleetPage.CommandSubmitFailed"), color="error")
            self._render_manual_action(None, available=False, instance=instance)
            return
        if submission.created:
            toast(t("Gui.FleetPage.CommandQueued"), color="success")
        else:
            toast(t("Gui.FleetPage.CommandAlreadyActive"), color="warning")
        self._refresh_fleet_page(instance)

    def _render_manual_action(
        self,
        command: FleetManualScanCommand | None,
        *,
        available: bool,
        instance: str,
    ) -> None:
        active = command is not None and command.status in {
            FleetManualScanStatus.PENDING,
            FleetManualScanStatus.RUNNING,
        }
        with use_scope("fleet_manual_action", clear=True):
            put_button(
                t("Gui.FleetPage.ScanSelected"),
                onclick=lambda: self._submit_manual_scan(instance),
                color="primary",
                disabled=active or not available,
            )

    @staticmethod
    def _command_error_text(error_code: str | None) -> str:
        known = {
            "physical_scan_failed": "Gui.FleetPage.ErrorPhysicalScan",
            "manual_scan_failed": "Gui.FleetPage.ErrorManualScan",
            "worker_interrupted": "Gui.FleetPage.ErrorWorkerInterrupted",
            "persistence_failed": "Gui.FleetPage.ErrorPersistence",
        }
        return t(known.get(error_code, "Gui.FleetPage.ErrorUnknown"))

    def _manual_status_text(
        self,
        command: FleetManualScanCommand | None,
        *,
        worker_running: bool,
    ) -> str:
        if command is None:
            return t("Gui.FleetPage.CommandNone")
        selected = ", ".join(map(str, command.selection.fleet_indices))
        if command.status is FleetManualScanStatus.PENDING:
            state = (
                t("Gui.FleetPage.CommandPending")
                if worker_running
                else t("Gui.FleetPage.CommandWaitingWorker")
            )
        elif command.status is FleetManualScanStatus.RUNNING:
            state = t("Gui.FleetPage.CommandRunning")
        elif command.status is FleetManualScanStatus.SUCCEEDED:
            state = t("Gui.FleetPage.CommandSucceeded")
        elif command.status is FleetManualScanStatus.PARTIAL:
            state = t("Gui.FleetPage.CommandPartial")
        else:
            state = t("Gui.FleetPage.CommandFailed")
        text = t("Gui.FleetPage.CommandStatus", state=state, fleets=selected)
        if command.error_code is not None:
            text = f"{text} · {self._command_error_text(command.error_code)}"
        return text

    def _render_command_status(
        self,
        command: FleetManualScanCommand | None,
        *,
        instance: str,
    ) -> None:
        worker_running = ProcessManager.is_running(instance)
        with use_scope("fleet_manual_status", clear=True):
            put_text(
                self._manual_status_text(command, worker_running=worker_running)
            ).style("font-size: .9rem; opacity: .82;")

    def _table_cell(self, text: str, *, state: str | None = None):
        output = put_text(text)
        if state:
            output.style(f"--fleet-slot-{state}--")
        return output

    def _row_outputs(self, row: FleetRowViewModel) -> list[Any]:
        if row.observed_at is None:
            cells = [
                self._table_cell(t("Gui.FleetPage.NoData"), state="no-data")
                for _ in range(6)
            ]
            observed = self._table_cell(t("Gui.FleetPage.NoData"), state="no-data")
            status = self._table_cell(t("Gui.FleetPage.StatusNoData"), state="no-data")
        else:
            cells = [
                self._table_cell(fleet_slot_text(slot), state=slot.state.value)
                for slot in row.slots
            ]
            observed = self._table_cell(
                format_fleet_timestamp(
                    row.observed_at,
                    self.fleet_page_context.runtime_timezone,
                )
            )
            status_key = (
                "Gui.FleetPage.StatusComplete"
                if row.complete
                else "Gui.FleetPage.StatusIncomplete"
            )
            status = self._table_cell(
                t(status_key),
                state="complete" if row.complete else "incomplete",
            )
        return [self._table_cell(str(row.fleet_index)), *cells, observed, status]

    def _render_fleet_table(self, model: FleetPageViewModel) -> None:
        headers = [
            t("Gui.FleetPage.ColumnFleet"),
            t("Gui.FleetPage.ColumnMain1"),
            t("Gui.FleetPage.ColumnMain2"),
            t("Gui.FleetPage.ColumnMain3"),
            t("Gui.FleetPage.ColumnVanguard1"),
            t("Gui.FleetPage.ColumnVanguard2"),
            t("Gui.FleetPage.ColumnVanguard3"),
            t("Gui.FleetPage.ColumnObservedAt"),
            t("Gui.FleetPage.ColumnStatus"),
        ]
        with use_scope("fleet_state_table", clear=True):
            put_table([headers, *(self._row_outputs(row) for row in model.rows)])

    def _render_fleet_summary(self, model: FleetPageViewModel) -> None:
        complete = sum(row.observed_at is not None and row.complete for row in model.rows)
        incomplete = sum(
            row.observed_at is not None and not row.complete for row in model.rows
        )
        no_data = len(model.rows) - complete - incomplete
        with use_scope("fleet_summary", clear=True):
            put_html(
                '<div class="fleet-summary-grid">'
                f'<div><strong>{complete}</strong><span>{t("Gui.FleetPage.SummaryComplete")}</span></div>'
                f'<div><strong>{incomplete}</strong><span>{t("Gui.FleetPage.SummaryIncomplete")}</span></div>'
                f'<div><strong>{no_data}</strong><span>{t("Gui.FleetPage.SummaryNoData")}</span></div>'
                '</div>'
            )

    def _render_load_error(self, instance: str) -> None:
        with use_scope("fleet_manual_status", clear=True):
            put_text(t("Gui.FleetPage.StorageUnavailable")).style(
                "--fleet-slot-incomplete--"
            )
        with use_scope("fleet_state_table", clear=True):
            put_html(
                '<div class="fleet-state-message fleet-state-message-error">'
                f'{t("Gui.FleetPage.StorageUnavailable")}'
                '</div>'
            )
        with use_scope("fleet_summary", clear=True):
            put_html(
                '<div class="fleet-summary-error">'
                f'{t("Gui.FleetPage.StorageUnavailable")}'
                '</div>'
            )
        self._render_manual_action(None, available=False, instance=instance)

    def _refresh_fleet_page(self, instance: str) -> None:
        if not self._fleet_page_is_current(instance):
            return
        try:
            model = self.fleet_page_context.query_service.view(instance)
        except Exception as exc:
            logger.exception(exc)
            self._render_load_error(instance)
            return
        self._render_command_status(model.manual_command, instance=instance)
        self._render_manual_action(
            model.manual_command,
            available=True,
            instance=instance,
        )
        self._render_fleet_table(model)
        self._render_fleet_summary(model)

    def _render_manual_controls(self, instance: str) -> None:
        put_html(
            '<div class="fleet-card-heading">'
            f'<span>{t("Gui.FleetPage.ManualTitle")}</span>'
            f'<small>{t("Gui.FleetPage.ManualHelp")}</small>'
            '</div>'
        )
        put_checkbox(
            _MANUAL_SELECTION_PIN,
            options=[
                {"label": str(index), "value": index}
                for index in SUPPORTED_SURFACE_FLEET_INDICES
            ],
            label=t("Gui.FleetPage.Fleets"),
            inline=True,
            value=list(SUPPORTED_SURFACE_FLEET_INDICES),
        )
        put_buttons(
            [
                {
                    "label": t("Gui.FleetPage.SelectAll"),
                    "value": "all",
                    "color": "secondary",
                },
                {
                    "label": t("Gui.FleetPage.ClearSelection"),
                    "value": "clear",
                    "color": "secondary",
                },
            ],
            onclick=[self._select_all_manual_fleets, self._clear_manual_fleets],
        )
        put_scope("fleet_manual_action")
        put_scope("fleet_manual_status")

    def _render_autoscan_controls(self, config) -> None:
        with use_scope("groups"):
            put_html(
                '<div class="fleet-card-heading">'
                f'<span>{t("Gui.FleetPage.AutoScanTitle")}</span>'
                f'<small>{t("Gui.FleetPage.AutoScanHelp")}</small>'
                '</div>'
            )
        scheduler_args = {
            name: definition
            for name, definition in self.ALAS_ARGS["FleetAutoScan"]["Scheduler"].items()
            if name in {"Enable", "PushNotification", "NextRun"}
        }
        self.set_group(
            ("Scheduler",),
            scheduler_args,
            config,
            "FleetAutoScan",
        )
        self.set_group(
            ("FleetAutoScan",),
            self.ALAS_ARGS["FleetAutoScan"]["FleetAutoScan"],
            config,
            "FleetAutoScan",
        )
        with use_scope("groups"):
            put_html(
                '<div class="fleet-scheduler-note">'
                f'{t("Gui.FleetPage.AutoScanBoundary")}'
                '</div>'
            )

    @use_scope("content", clear=True)
    def ui_fleet_page(self) -> None:
        instance = self.alas_name
        if not instance:
            return
        self.init_menu(name=_PAGE_NAME)
        self.set_title(t("Gui.FleetPage.Title"))
        put_scope("fleet_page_root").style("--fleet-page--")
        with use_scope("fleet_page_root"):
            put_row(
                [
                    put_scope("fleet_main"),
                    put_scope("groups").style("--fleet-scheduler-card--"),
                ],
                size="minmax(0, 1fr) minmax(330px, 360px)",
            ).style("--fleet-workspace--")
            with use_scope("fleet_main"):
                put_html(
                    '<section class="fleet-hero">'
                    f'<div class="fleet-eyebrow">{t("Gui.FleetPage.HeroKicker")}</div>'
                    f'<h2>{t("Gui.FleetPage.Title")}</h2>'
                    f'<p>{t("Gui.FleetPage.HeroLead")}</p>'
                    '</section>'
                )
                put_scope("fleet_summary")
                put_scope("fleet_manual_card").style("--fleet-manual-card--")
                put_scope(
                    "fleet_state_card",
                    [
                        put_html(
                            '<div class="fleet-card-heading fleet-state-heading">'
                            f'<span>{t("Gui.FleetPage.StateTitle")}</span>'
                            f'<small>{t("Gui.FleetPage.StateHelp")}</small>'
                            '</div>'
                        ),
                        put_scope("fleet_state_table"),
                    ],
                ).style("--fleet-state-card--")
            config = self.alas_config.read_file(instance)
            with use_scope("fleet_manual_card"):
                self._render_manual_controls(instance)
            self._render_autoscan_controls(config)
            with use_scope("fleet_summary"):
                put_html(
                    '<div class="fleet-state-message">'
                    f'{t("Gui.FleetPage.StateLoading")}'
                    '</div>'
                )
            with use_scope("fleet_state_table"):
                put_html(
                    '<div class="fleet-state-message">'
                    f'{t("Gui.FleetPage.StateLoading")}'
                    '</div>'
                )
        self._refresh_fleet_page(instance)
        self.task_handler.add(
            lambda: self._refresh_fleet_page(instance),
            _REFRESH_SECONDS,
            True,
        )


__all__ = [
    "FleetPageMixin",
    "fleet_slot_text",
    "format_fleet_timestamp",
]
