"""Fail-closed Global/EN boundary for the OOBE wizard."""

from html import escape

from pywebio.output import put_html
from pywebio.pin import pin, pin_on_change

import module.webui.lang as lang
from module.config.server import to_package
from module.webui.oobe_base import CSS, OOBE_ROOT, OOBEWizard as _OOBEWizard
from module.webui.pin import put_select


class OOBEWizard(_OOBEWizard):
    """Expose only the Global package and escape OOBE review values."""

    def __init__(self, gui):
        super().__init__(gui)
        # `ap` is reserved for smoke/acceptance runs and stays hidden from WebUI.
        self.config_name = "alas"

    def _step_emulator(self):
        put_html(
            f'<h2 class="oobe-section-title">{lang.t("Gui.OOBE.EmulatorTitle")}</h2>'
            f'<p class="oobe-section-hint">{lang.t("Gui.OOBE.EmulatorHint")}</p>'
        )
        serial_options = self._serial_options()
        if self.emulator_serial == "127.0.0.1:5555":
            detected = [item["value"] for item in serial_options if item.get("detected")]
            if detected:
                self.emulator_serial = detected[0]
        put_select(
            name="oobe_serial_select",
            label=lang.t("Gui.OOBE.EmulatorSerial"),
            value=self.emulator_serial,
            options=[
                {key: value for key, value in item.items() if key in ("label", "value")}
                for item in serial_options
            ],
        )
        pin_on_change(
            "oobe_serial_select",
            onchange=lambda serial: self._on_emulator_serial_changed(serial),
        )
        put_html('<div style="height:12px"></div>')
        package = to_package(self.package_name)
        put_select(
            name="oobe_package",
            label=lang.t("Gui.OOBE.EmulatorPackage"),
            value=package,
            options=[
                {
                    "label": f'{lang.t("Gui.OOBE.ServerEN")} ({package})',
                    "value": package,
                },
            ],
        )
        put_html('<hr class="oobe-divider">')
        self._render_footer(
            on_next=lambda _: self._collect_emulator_and_go(1),
            on_back=lambda _: self._collect_emulator_and_go(-1),
        )

    def _collect_emulator_and_go(self, direction):
        self._sync_emulator_pin_values()
        self.package_name = to_package(self.package_name)
        if not self.emulator_serial:
            self.emulator_serial = "auto"
        self.emulator_serial = str(self.emulator_serial)
        if direction > 0:
            self._next_step()
        else:
            self._prev_step()

    def _sync_emulator_pin_values(self):
        try:
            serial = pin.oobe_serial_select
        except Exception:
            serial = None
        if serial:
            self.emulator_serial = str(serial)

        try:
            package = pin.oobe_package
        except Exception:
            package = None
        self.package_name = to_package(str(package or self.package_name))

    def _step_review(self):
        put_html(
            f'<h2 class="oobe-section-title">{lang.t("Gui.OOBE.ReviewTitle")}</h2>'
            f'<p class="oobe-section-hint">{lang.t("Gui.OOBE.ReviewHint")}</p>'
        )
        server_display = {"en": "EN"}.get(self.server, self.server)
        items = [
            (lang.t("Gui.OOBE.ReviewConfigName"), self.config_name),
            (lang.t("Gui.OOBE.ReviewServer"), server_display),
            (lang.t("Gui.OOBE.ReviewSerial"), self.emulator_serial),
            (lang.t("Gui.OOBE.ReviewPackage"), self.package_name),
            (lang.t("Emulator.ServerName.name"), self.server_name),
        ]
        rows = "".join(
            f"<tr><td>{escape(str(key))}</td><td>{escape(str(value))}</td></tr>"
            for key, value in items
        )
        put_html(f'<table class="oobe-review-table">{rows}</table>')
        put_html('<hr class="oobe-divider">')
        self._render_footer(
            next_label=lang.t("Gui.OOBE.ButtonCreate"),
            next_color="success",
            on_next=lambda _: self._create_config_and_finish(),
        )
