"""Инструменты WebUI, не являющиеся источниками фактов Event UI."""

from module.webui.app_dependencies import (
    BinarySwitchButton,
    ProcessManager,
    RichLog,
    base64,
    cast,
    logger,
    put_html,
    put_scope,
    put_text,
    t,
    use_scope,
)
from module.webui.app_types import WebUIMixinBase


class EventToolsMixin(WebUIMixinBase):
    """Оставляет только автономный Operation Siren simulator."""

    def _os_simulator(self):
        self.simulator.set_config(self.alas_config)
        self._last_os_simulator_figure = None

        if self._simulator_logger_pm is None:

            class SimulatorLogger:
                def __init__(self):
                    self.renderables = []
                    self.renderables_max_length = 2000
                    self.renderables_reduce_length = 1000
                    self.renderables_total = 0

            self._simulator_logger_pm = SimulatorLogger()

        pm = self._simulator_logger_pm
        import logging

        class ListHandler(logging.Handler):
            is_webui_simulator_handler: bool = True

            def emit(self, record: logging.LogRecord) -> None:
                msg = self.format(record)
                pm.renderables.append(msg + "\n")
                pm.renderables_total += 1
                if len(pm.renderables) > pm.renderables_max_length:
                    del pm.renderables[: pm.renderables_reduce_length]

        for handler in self.simulator.logger.handlers[:]:
            if getattr(handler, "is_webui_simulator_handler", False):
                self.simulator.logger.removeHandler(handler)

        handler = ListHandler()
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        self.simulator.logger.addHandler(handler)

        put_scope(
            "scheduler-bar",
            [
                put_text(t("Task.OpsiSimulator.name")).style(
                    "font-size: 1.25rem; margin: auto .5rem auto;"
                ),
                put_scope("scheduler_btn"),
            ],
        )
        put_scope("figure_display")
        put_scope(
            "logs",
            [
                put_scope(
                    "log-bar",
                    [
                        put_text(t("Gui.Overview.Log")).style(
                            "font-size: 1.25rem; margin: auto .5rem auto;"
                        ),
                        put_scope("log-bar-btns", [put_scope("log_scroll_btn")]),
                    ],
                ),
                put_scope("log-container", [put_scope("log", [put_html("")])]),
            ],
        )

        switch_scheduler = BinarySwitchButton(
            label_on=t("Gui.Button.Stop"),
            label_off=t("Gui.Button.Start"),
            onclick_on=self.simulator.interrupt,
            onclick_off=self._simulator_start,
            get_state=lambda: self.simulator.is_running,
            color_on="off",
            color_off="on",
            scope="scheduler_btn",
        )
        self.task_handler.add(switch_scheduler.g(), 1, True)

        log = RichLog("log")
        log.console.width = log.get_width()
        switch_log_scroll = BinarySwitchButton(
            label_on=t("Gui.Button.ScrollON"),
            label_off=t("Gui.Button.ScrollOFF"),
            onclick_on=lambda: log.set_scroll(False),
            onclick_off=lambda: log.set_scroll(True),
            get_state=lambda: log.keep_bottom,
            color_on="on",
            color_off="off",
            scope="log_scroll_btn",
        )
        self.task_handler.add(switch_log_scroll.g(), 1, True)

        def update_simulator_figure():
            last_figure = getattr(self, "_last_os_simulator_figure", None)
            if self.simulator.figure == last_figure:
                return
            figure_path = self.simulator.figure
            self._last_os_simulator_figure = figure_path
            if figure_path:
                try:
                    with open(figure_path, "rb") as file:
                        image = base64.b64encode(file.read()).decode("utf-8")
                    with use_scope("figure_display", clear=True):
                        put_html(
                            f'<img src="data:image/png;base64,{image}" style="max-width: 100%; height: auto; display: block; margin: 0 auto;">'
                        )
                except FileNotFoundError:
                    with use_scope("figure_display", clear=True):
                        pass
                except Exception as exc:
                    logger.warning(
                        f"[WebUI — инструменты события] Не удалось обновить график симулятора: {exc}"
                    )
            else:
                with use_scope("figure_display", clear=True):
                    pass

        self.task_handler.add(update_simulator_figure, 0.5, True)
        self.task_handler.add(log.put_log(cast(ProcessManager, pm)), 0.25, True)
