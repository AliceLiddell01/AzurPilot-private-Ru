"""WebUI会话外壳"""

from module.webui.app_dependencies import (
    AzurLaneConfig,
    Icon,
    ProcessManager,
    State,
    alas_instance,
    clear,
    current_time,
    filepath_args,
    put_html,
    put_icon_buttons,
    put_loading_text,
    put_scope,
    queue,
    read_file,
    t,
    time_source_status,
    timedelta,
    timezone,
    use_scope,
    webconfig,
)


from module.webui.app_types import WebUIMixinBase


class AppShellMixin(WebUIMixinBase):
    """WebUI会话外壳"""

    def initial(self) -> None:
        self.ALAS_MENU = read_file(filepath_args("menu", self.alas_mod))
        self.ALAS_ARGS = read_file(filepath_args("args", self.alas_mod))

    def __init__(self) -> None:
        super().__init__()
        # 已修改的配置键，来自 pin_wait_change() 的返回值
        self.modified_config_queue = queue.Queue()
        # 当前 Alas 配置名称
        self.alas_name = ""
        self.alas_mod = "alas"
        self.alas_config = AzurLaneConfig("template")
        self.initial()
        # 已渲染的状态缓存
        self.rendered_cache = []
        self.inst_cache = []
        self._shell_mounted = False
        self._active_aside = None
        self._stored_aside = None
        self._overview_snapshot = None
        self.af_flag = False
        self._last_announcement_id = None
        self._announcement_result = None
        self._announcement_fetching = False
        self._announcement_force = False
        self._simulator = None
        self._simulator_logger_pm = None
        self._overview_log = None
        self._overview_log_config_name = None

    @property
    def simulator(self):
        """在首次进入大世界模拟器时再加载其运行时依赖。"""
        if self._simulator is None:
            import sys

            from module.webui.fake_pil_module import remove_fake_pil_module

            # matplotlib 需要真实 PIL；仅移除 WebUI 启动阶段安装的替身，
            # 避免其他会话已加载真实 PIL 时再次从模块缓存中删除它。
            if not hasattr(sys.modules.get("PIL"), "__path__"):
                remove_fake_pil_module()
            from module.os_simulator.simulator import OSSimulator

            self._simulator = OSSimulator()
        return self._simulator

    @use_scope("aside", clear=True)
    def set_aside(self) -> None:
        # TODO: 更新 put_icon_buttons()

        current_date = current_time().date()
        if current_date.month == 4 and current_date.day == 1:
            self.af_flag = True

        put_scope("aside_home")
        put_scope("aside_instance")
        put_scope("aside_manage")
        self.refresh_aside_labels()
        self.refresh_aside_instances(force=True)

    def refresh_aside_labels(self) -> None:
        """语言变化时只更新主边栏中的静态按钮。"""
        with use_scope("aside_home", clear=True):
            put_icon_buttons(
                Icon.DEVELOP,
                "false",
                buttons=[
                    {
                        "label": t("Gui.Aside.Home"),
                        "value": "Home",
                        "color": "aside",
                    }
                ],
                onclick=[self.ui_develop],
            )
        with use_scope("aside_manage", clear=True):
            put_icon_buttons(
                Icon.SETTING,
                "false",
                buttons=[
                    {
                        "label": t("Gui.AddAlas.Manage"),
                        "value": "Manage",
                        "color": "aside",
                    }
                ],
                onclick=[self.ui_manage],
            )
        aside_name = self._active_aside or self._stored_aside or "Home"
        self.active_button("aside", aside_name)

    @use_scope("aside_instance")
    def refresh_aside_instances(self, force=False) -> None:
        """仅在实例集合或运行状态变化时更新实例侧栏。"""
        instances = alas_instance()
        rebuild = (
            force
            or instances != self.inst_cache
            or len(self.rendered_cache) != len(instances)
        )

        def update(name, seq):
            with use_scope(f"alas-instance-{seq}", clear=True):
                rendered_state = ProcessManager.get_manager(name).state
                if rendered_state == 1:
                    icon_html = Icon.RUNNING
                elif rendered_state == 3:
                    icon_html = Icon.ERROR
                else:
                    icon_html = Icon.RUN
                status_signal = "false" if rendered_state in (1, 3) else "true"
                if rendered_state == 1 and getattr(self, "af_flag", False):
                    icon_html = icon_html[:31] + " anim-rotate" + icon_html[31:]
                put_icon_buttons(
                    icon_html,
                    status_signal,
                    buttons=[{"label": name, "value": name, "color": "aside"}],
                    onclick=self.ui_alas,
                )
            return rendered_state

        changed = rebuild
        if rebuild:
            self.inst_cache = instances
            self.rendered_cache.clear()
            clear()
            for index, _ in enumerate(instances):
                put_scope(f"alas-instance-{index}")
            for index, inst in enumerate(instances):
                self.rendered_cache.append(update(inst, index))
        else:
            for index, inst in enumerate(instances):
                state = ProcessManager.get_manager(inst).state
                if state != self.rendered_cache[index]:
                    self.rendered_cache[index] = update(inst, index)
                    changed = True

        if changed:
            aside_name = self._active_aside or self._stored_aside or "Home"
            self.active_button("aside", aside_name)

    def set_aside_status(self) -> None:
        self.refresh_aside_instances()

    @use_scope("header_status")
    def set_status(self, state: int) -> None:
        """
        Args:
            state (int):
                1 (running)
                2 (not running)
                3 (warning, stop unexpectedly)
                0 (hide)
                -1 (*state not changed)
        """
        if state == -1:
            return
        clear()

        if state == 1:
            put_loading_text(t("Gui.Status.Running"), color="success")
        elif state == 2:
            put_loading_text(t("Gui.Status.Inactive"), color="secondary", fill=True)
        elif state == 3:
            put_loading_text(t("Gui.Status.Warning"), shape="grow", color="warning")

    @staticmethod
    def _format_tz_offset(offset: timedelta) -> str:
        seconds = int(offset.total_seconds())
        sign = "+" if seconds >= 0 else "-"
        seconds = abs(seconds)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    def _time_status_text(self) -> str:
        data = time_source_status()
        local_offset = current_time(timezone.utc).astimezone().utcoffset()
        local_tz = self._format_tz_offset(local_offset or timedelta(0))
        sync_text = "синхронизировано" if data["synced"] else "локальное время"
        enabled_text = "NTP" if data["enabled"] else "NTP отключён"
        return (
            f"{enabled_text}: {sync_text} · смещение {data['offset']:+.3f} с · "
            f"локальный пояс {local_tz}"
        )

    @classmethod
    def set_theme(cls, theme="default") -> None:
        if theme == "apple":
            theme = "advanced_material"
        if theme not in (
            "default",
            "dark",
            "light",
            "advanced_material",
            "dark_advanced_material",
        ):
            theme = "default"
        cls.theme = theme
        State.deploy_config.Theme = theme
        State.theme = theme
        pywebio_theme = theme if theme in ("default", "dark", "light") else "dark"
        if theme in ("advanced_material", "dark_advanced_material"):
            pywebio_theme = "default"

        webconfig(theme=pywebio_theme)
