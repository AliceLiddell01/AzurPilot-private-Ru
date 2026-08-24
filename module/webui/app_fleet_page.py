"""Standalone Fleet State page and manual scan controls."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cached_property
from typing import Any
from zoneinfo import ZoneInfo

from module.application.fleet_autoscan import FleetAutoScanConfig
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
from module.config.deep import deep_get
from module.formation.model import FleetSelection, SUPPORTED_SURFACE_FLEET_INDICES
from module.webui.app_dependencies import (
    ProcessManager,
    logger,
    pin,
    put_button,
    put_buttons,
    put_checkbox,
    put_html,
    put_scope,
    put_select,
    put_table,
    put_text,
    t,
    toast,
    use_scope,
)
from module.webui.app_types import WebUIMixinBase

_PAGE_NAME = "FleetPage"
_MANUAL_SELECTION_PIN = "FleetPage_ManualSelection"
_AUTOSCAN_MODE_PIN = "FleetPage_AutoScanMode"
_AUTOSCAN_FLEETS_PIN = "FleetPage_AutoScanFleets"
_REFRESH_SECONDS = 2.0


def load_fleet_autoscan_config(config: Mapping[str, Any]) -> FleetAutoScanConfig:
    """Read the existing Stage 2 config contract without a duplicate setting."""

    return FleetAutoScanConfig.from_raw(
        deep_get(config, "Alas.FleetAutoScan.Mode"),
        deep_get(config, "Alas.FleetAutoScan.Fleets"),
    )


def normalize_fleet_autoscan_update(
    mode: object,
    fleet_indices: object,
) -> dict[str, object]:
    config = FleetAutoScanConfig.from_raw(mode, fleet_indices)
    return {
        "Alas.FleetAutoScan.Mode": config.mode.value,
        "Alas.FleetAutoScan.Fleets": list(config.selection.fleet_indices),
    }


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
        return slot.canonical_name or t("Gui.FleetPage.SlotUnknown")
    displayed = slot.displayed_name or t("Gui.FleetPage.SlotUnknown")
    if slot.state is FleetSlotState.UNRESOLVED:
        return t("Gui.FleetPage.SlotUnresolved", name=displayed)
    return t("Gui.FleetPage.SlotAmbiguous", name=displayed)


class FleetPageMixin(WebUIMixinBase):
    """Fleet page capability composed into the session-scoped AlasGUI."""

    @cached_property
    def fleet_page_context(self):
        from module.persistence.runtime import build_runtime_fleet_page_context

        return build_runtime_fleet_page_context(require_ready=False)

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

    def _read_autoscan_config(self) -> FleetAutoScanConfig:
        return load_fleet_autoscan_config(
            self.alas_config.read_file(self.alas_name)
        )

    def _save_autoscan_config(self) -> None:
        try:
            changes = normalize_fleet_autoscan_update(
                pin[_AUTOSCAN_MODE_PIN],
                pin[_AUTOSCAN_FLEETS_PIN],
            )
        except (TypeError, ValueError):
            toast(t("Gui.FleetPage.InvalidSelection"), color="error")
            return

        self._save_config(changes, self.alas_name, self.alas_config)
        self.alas_config.load()
        try:
            persisted = self._read_autoscan_config()
        except Exception as exc:
            logger.exception(exc)
            toast(t("Gui.FleetPage.ConfigSaveFailed"), color="error")
            return
        expected = FleetAutoScanConfig.from_raw(
            changes["Alas.FleetAutoScan.Mode"],
            changes["Alas.FleetAutoScan.Fleets"],
        )
        if persisted != expected:
            toast(t("Gui.FleetPage.ConfigSaveFailed"), color="error")
            return
        toast(t("Gui.FleetPage.ConfigSaved"), color="success")

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

    def _render_load_error(self, instance: str) -> None:
        with use_scope("fleet_manual_status", clear=True):
            put_text(t("Gui.FleetPage.StorageUnavailable")).style(
                "--fleet-slot-incomplete--"
            )
        with use_scope("fleet_state_table", clear=True):
            put_text(t("Gui.FleetPage.StorageUnavailable"))
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

    def _render_manual_controls(self, instance: str) -> None:
        put_html(f"<h3>{t('Gui.FleetPage.ManualTitle')}</h3>")
        put_text(t("Gui.FleetPage.ManualHelp"))
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

    def _render_autoscan_controls(self) -> None:
        autoscan = self._read_autoscan_config()
        put_html(f"<h3>{t('Gui.FleetPage.AutoScanTitle')}</h3>")
        put_text(t("Gui.FleetPage.AutoScanHelp"))
        put_select(
            _AUTOSCAN_MODE_PIN,
            options=[
                {
                    "label": t("Gui.FleetPage.ModeDisabled"),
                    "value": "disabled",
                },
                {
                    "label": t("Gui.FleetPage.ModeEveryStart"),
                    "value": "every_start",
                },
                {"label": t("Gui.FleetPage.ModeDaily"), "value": "daily"},
            ],
            label=t("Gui.FleetPage.Mode"),
            value=autoscan.mode.value,
        )
        put_checkbox(
            _AUTOSCAN_FLEETS_PIN,
            options=[
                {"label": str(index), "value": index}
                for index in SUPPORTED_SURFACE_FLEET_INDICES
            ],
            label=t("Gui.FleetPage.Fleets"),
            inline=True,
            value=list(autoscan.selection.fleet_indices),
        )
        put_button(
            t("Gui.FleetPage.SaveAutoScan"),
            onclick=self._save_autoscan_config,
            color="primary",
        )
        put_text(t("Gui.FleetPage.AutoScanBoundary"))

    @use_scope("content", clear=True)
    def ui_fleet_page(self) -> None:
        instance = self.alas_name
        if not instance:
            return
        self.init_menu(name=_PAGE_NAME)
        self.set_title(t("Gui.FleetPage.Title"))
        put_scope("fleet_page_root").style("--fleet-page--")
        with use_scope("fleet_page_root"):
            self._render_manual_controls(instance)
            self._render_autoscan_controls()
            put_html(f"<h3>{t('Gui.FleetPage.StateTitle')}</h3>")
            put_scope("fleet_state_table")
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
    "load_fleet_autoscan_config",
    "normalize_fleet_autoscan_update",
]
