"""WebUI首页和会话运行"""

from module.webui.app_dependencies import (
    Switch,
    _t,
    alas_instance,
    get_localstorage_values,
    get_window_visibility_state,
    go_app,
    is_oobe_needed,
    lang,
    load_webui_styles,
    put_buttons,
    put_html,
    put_markdown,
    put_text,
    register_thread,
    run_js,
    set_env,
    set_localstorage,
    t,
    threading,
    toast,
    use_scope,
)


from module.webui.app_types import WebUIMixinBase


class HomeMixin(WebUIMixinBase):
    """WebUI首页和会话运行"""

    def show(self) -> None:
        self.mount_shell()
        self.show_home()

    def show_home(self) -> None:
        self.mount_shell()
        self._set_manage_mode(False)
        self._active_aside = "Home"
        self.init_aside(name="Home")
        self.dev_set_menu()
        self.init_menu(name="HomePage")
        self.set_title(t("Gui.MenuDevelop.HomePage"))
        self.alas_name = ""
        if hasattr(self, "alas"):
            del self.alas
        self.set_status(0)

        def set_theme(t):
            self.set_theme(t)
            set_localstorage("aside", "Home")
            go_app("index", new_window=False)

        with use_scope("content"):
            put_text("Тема интерфейса").style("text-align: center")
            put_buttons(
                [
                    {"label": "Светлая", "value": "default", "color": "light"},
                    {"label": "Тёмная", "value": "dark", "color": "dark"},
                    {
                        "label": "Современная",
                        "value": "advanced_material",
                        "color": "primary",
                    },
                    {
                        "label": "Современная тёмная",
                        "value": "dark_advanced_material",
                        "color": "dark",
                    },
                ],
                onclick=lambda t: set_theme(t),
            ).style("text-align: center")
            put_html('<div class="alas-home-marker" aria-hidden="true"></div>')
            # show something
            put_markdown(
                """
            AzurPilot — бесплатная модификация проекта Alas (AzurLaneAutoScript),
            распространяемая по лицензии GPL-3.0. Если вы заплатили за программу,
            запросите возврат средств.

            Исходный проект: `https://github.com/LmeSzinc/AzurLaneAutoScript`

            AzurPilot: `https://github.com/wess09/AzurPilot`

            Персональная русская версия: `https://github.com/AliceLiddell01/AzurPilot-private-Ru`
            """
            ).style("text-align: center")

        if lang.TRANSLATE_MODE:
            lang.reload()

            def _disable():
                lang.TRANSLATE_MODE = False
                self.show_home()

            toast(
                _t("Gui.Toast.DisableTranslateMode"),
                duration=0,
                position="right",
                onclick=_disable,
            )

    def _load_deferred_client_assets(self) -> None:
        """在首次绘制后加载本地交互资源。"""
        run_js(
            "(function() {"
            "function load() {"
            "if (!document.querySelector('link[rel=\"manifest\"]')) {"
            "var manifest=document.createElement('link');"
            "manifest.rel='manifest';manifest.href='static/assets/spa/manifest.json';"
            "document.head.appendChild(manifest);"
            "}"
            "if (!document.getElementById('alas-utils-script')) {"
            "var script=document.createElement('script');"
            "script.id='alas-utils-script';script.async=true;"
            "script.src='static/assets/gui/js/alas-utils.js';"
            "document.head.appendChild(script);"
            "}"
            "}"
            "if (window.requestIdleCallback) {"
            "window.requestIdleCallback(load, {timeout: 3000});"
            "} else { window.setTimeout(load, 0); }"
            "})();"
        )

    def run(self, initial_page="home", localstorage=None) -> None:
        # setup gui
        set_env(title="AzurPilot", output_animation=False)
        load_webui_styles(theme=self.theme, is_mobile=self.is_mobile)
        if localstorage is None:
            localstorage = get_localstorage_values(("aside",))
        aside = localstorage.get("aside")
        self._stored_aside = aside

        # OOBE 初次设置向导：无用户配置时引导完成基本设置
        if is_oobe_needed():
            from module.webui.oobe import OOBEWizard

            OOBEWizard(self).start()
            self._load_deferred_client_assets()
            return

        self.mount_shell()
        restore_instance = initial_page == "home" and aside in alas_instance()
        if initial_page == "manage":
            self.ui_manage()
        elif not restore_instance:
            self.show_home()

        # save config
        _thread_save_config = threading.Thread(target=self._alas_thread_update_config)
        register_thread(_thread_save_config)
        _thread_save_config.start()

        visibility_state_switch = Switch(
            status={
                True: [
                    lambda: self.__setattr__("visible", True),
                    lambda: (
                        self.alas_update_overview_task()
                        if self.page == "Overview"
                        else 0
                    ),
                    lambda: self.task_handler._task.__setattr__("delay", 15),
                ],
                False: [
                    lambda: self.__setattr__("visible", False),
                    lambda: self.task_handler._task.__setattr__("delay", 1),
                ],
            },
            get_state=get_window_visibility_state,
            name="visibility_state",
        )

        self.state_switch = Switch(
            status=self.set_status,
            get_state=lambda: getattr(getattr(self, "alas", -1), "state", 0),
            name="state",
        )

        self.task_handler.add(self.state_switch.g(), 2)
        self.task_handler.add(self.set_aside_status, 2)
        self.task_handler.add(visibility_state_switch.g(), 15)

        if restore_instance:
            self.ui_alas(aside)

        self._load_deferred_client_assets()

        # 启动任务处理器
        self.task_handler.start()
