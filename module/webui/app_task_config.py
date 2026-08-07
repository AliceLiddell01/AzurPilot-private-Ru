"""WebUI任务菜单和配置表单"""

import json
import re

from typing import cast

from module.webui.app_dependencies import (
    Any,
    DEFAULT_CONFIG_NAME,
    Dict,
    List,
    Optional,
    Output,
    State,
    T_Output_Kwargs,
    current_time,
    datetime,
    deep_get,
    deep_iter,
    deep_set,
    dict_to_kv,
    filepath_config,
    get_device_id,
    logger,
    os,
    parse_pin_value,
    pin,
    pin_on_change,
    popup,
    put_button,
    put_buttons,
    put_collapse,
    put_html,
    put_none,
    put_output,
    put_scope,
    put_text,
    queue,
    re_fullmatch,
    run_js,
    t,
    to_pin_value,
    to_server,
    toast,
    use_scope,
)

from module.webui.app_helpers import (
    DEMO_DEVICE_ID_TEXT,
    build_copyable_device_id,
    is_demo_mode,
)


from module.webui.app_types import WebUIMixinBase


class TaskConfigMixin(WebUIMixinBase):
    """WebUI任务菜单和配置表单"""

    @use_scope("menu", clear=True)
    def alas_set_menu(self) -> None:
        """
        Set menu
        """
        put_buttons(
            [
                {
                    "label": t("Gui.MenuAlas.Overview"),
                    "value": "Overview",
                    "color": "menu",
                }
            ],
            onclick=[self.alas_overview],
        ).style(f"--menu-Overview--")

        for menu, task_data in self.ALAS_MENU.items():
            if task_data.get("page") == "tool":
                _onclick = self.alas_daemon_overview
            else:
                _onclick = self.alas_set_group

            if task_data.get("menu") == "collapse":
                task_btn_list = [
                    put_buttons(
                        [
                            {
                                "label": t(f"Task.{task}.name"),
                                "value": task,
                                "color": "menu",
                            }
                        ],
                        onclick=_onclick,
                    ).style(f"--menu-{task}--")
                    for task in task_data.get("tasks", [])
                ]
                put_collapse(title=t(f"Menu.{menu}.name"), content=task_btn_list)
            else:
                title = t(f"Menu.{menu}.name")
                put_html(
                    '<div class="hr-task-group-box">'
                    '<span class="hr-task-group-line"></span>'
                    f'<span class="hr-task-group-text">{title}</span>'
                    '<span class="hr-task-group-line"></span>'
                    "</div>"
                )
                for task in task_data.get("tasks", []):
                    put_buttons(
                        [
                            {
                                "label": t(f"Task.{task}.name"),
                                "value": task,
                                "color": "menu",
                            }
                        ],
                        onclick=_onclick,
                    ).style(f"--menu-{task}--").style(f"padding-left: 0.75rem")

        self.alas_overview()

    @use_scope("content", clear=True)
    def alas_set_group(self, task: str) -> None:
        """
        Set arg groups from dict
        """
        config = self.alas_config.read_file(self.alas_name)
        self.init_menu(name=task)
        self.set_title(t(f"Task.{task}.name"))

        put_scope("_groups", [put_none(), put_scope("groups"), put_scope("navigator")])

        task_help: str = t(f"Task.{task}.help")
        if task_help:
            put_scope(
                "group__info",
                scope="groups",
                content=[put_text(task_help).style("font-size: 1rem")],
            )

        if task == "Alas":
            with use_scope("groups"):
                self._render_startup_run_setting()

        if task == "OpsiSimulator":
            with use_scope("groups"):
                self._os_simulator()

        for group, arg_dict in deep_iter(self.ALAS_ARGS[task], depth=1):
            if self.set_group(group, arg_dict, config, task):
                self.set_navigator(group)
                if task == "EventGeneral" and group[0] == "EventGeneral":
                    with use_scope("groups"):
                        put_scope("group_EventCalculator")
                    self._render_event_calculator(config)

    @use_scope("groups")
    def set_group(self, group, arg_dict, config: Dict[str, Any], task: str) -> int:
        group_name = group[0]

        output_list: List[Output] = []
        watcher_paths: List[List[str]] = []
        for arg, arg_dict in deep_iter(arg_dict, depth=1):
            output_kwargs: T_Output_Kwargs = arg_dict.copy()

            # Skip hide
            display: Optional[str] = output_kwargs.pop("display", None)
            if display == "hide":
                continue
            # Disable
            elif display == "disabled":
                output_kwargs["disabled"] = True
            # Output type
            output_kwargs["widget_type"] = output_kwargs.pop("type")
            widget_type = output_kwargs["widget_type"]

            arg_name = arg[0]  # [arg_name,]
            # Internal pin widget name
            output_kwargs["name"] = f"{task}_{group_name}_{arg_name}"
            # Display title
            output_kwargs["title"] = t(f"{group_name}.{arg_name}.name")

            # Get value from config
            value = deep_get(
                config, [task, group_name, arg_name], output_kwargs["value"]
            )
            # datetime 控件只能接收文本，避免 Pin 在重绘时丢失原始时间值。
            value = str(value) if isinstance(value, datetime) else value
            # Default value
            output_kwargs["value"] = value
            # Options
            options = output_kwargs.pop("option", [])
            package_name = deep_get(config, "Alas.Emulator.PackageName", "cn")
            server = to_server(package_name if isinstance(package_name, str) else "cn")
            available_events = deep_get(
                self.ALAS_ARGS, keys=f"{task}.{group_name}.{arg_name}.option_{server}"
            )
            if available_events is not None:
                options = [opt for opt in options if opt in available_events]

            server_options = output_kwargs.get(f"option_{server}")
            if (
                output_kwargs["widget_type"] == "select"
                and isinstance(server_options, list)
                and server_options
            ):
                options = server_options
            output_kwargs["options"] = options
            if (
                task == "GemsFarming"
                and group_name == "Campaign"
                and arg_name == "Event"
                and output_kwargs["widget_type"] == "select"
                and len(options) == 1
            ):
                continue
            if output_kwargs["widget_type"] == "select" and len(options) == 1:
                only_option = options[0]
                if only_option in output_kwargs.get("option_bold", []):
                    output_kwargs["widget_type"] = "state"
            # Options label
            options_label = []
            for opt in options:
                options_label.append(t(f"{group_name}.{arg_name}.{opt}"))
            output_kwargs["options_label"] = options_label
            # Help
            arg_help = t(f"{group_name}.{arg_name}.help")
            if arg_help == "" or not arg_help:
                arg_help = None
            output_kwargs["help"] = arg_help
            if group_name == "Scheduler" and arg_name == "NextRun":
                output_kwargs["after"] = put_text(self._time_status_text()).style(
                    "font-size: .75rem; opacity: .68; margin: .2rem .25rem 0;"
                )
            # Invalid feedback
            output_kwargs["invalid_feedback"] = t("Gui.Text.InvalidFeedBack", value)

            o = put_output(output_kwargs)
            if o is not None:
                # output will inherit current scope when created, override here
                o.spec["scope"] = f"#pywebio-scope-group_{group_name}"
                output_list.append(o)
                if display != "readonly" and widget_type != "stored":
                    watcher_paths.append([task, group_name, arg_name])

        if not output_list:
            return 0

        with use_scope(f"group_{group_name}"):
            put_text(t(f"{group_name}._info.name"))
            group_help = t(f"{group_name}._info.help")
            if group_help != "":
                put_text(group_help)
            put_html('<hr class="hr-group">')
            for output in output_list:
                output.show()

            for path in watcher_paths:
                self._bind_config_watcher(path)

            # 在掉落记录组中显示可复制的设备ID
            if group_name == "DropRecord":
                device_id = DEMO_DEVICE_ID_TEXT if is_demo_mode() else get_device_id()
                put_html(build_copyable_device_id(device_id))

        return len(output_list)

    @use_scope("navigator")
    def set_navigator(self, group):
        js = f"""
            $("#pywebio-scope-groups").scrollTop(
                $("#pywebio-scope-group_{group[0]}").position().top
                + $("#pywebio-scope-groups").scrollTop() - 59
            )
        """
        put_button(
            label=t(f"{group[0]}._info.name"),
            onclick=lambda: run_js(js),
            color="navigator",
        )

    def _render_startup_run_setting(self) -> None:
        instance = self.alas_name or DEFAULT_CONFIG_NAME
        scope_id = re.sub(r"[^0-9A-Za-z_]", "_", instance)
        switch_id = f"startup-run-switch-{scope_id}"
        status_id = f"startup-run-status-{scope_id}"
        put_html(
            f"""
            <div class="startup-run-panel">
              <div class="startup-run-row">
                <div>
                  <div class="startup-run-title">{t("Gui.StartupRun.Title")}</div>
                  <div class="startup-run-desc">{t("Gui.StartupRun.Description")}</div>
                </div>
                <label class="launcher-switch" title="{t("Gui.StartupRun.Title")}">
                  <input id="{switch_id}" type="checkbox" disabled>
                </label>
              </div>
              <div id="{status_id}" class="startup-run-status">{t("Gui.StartupRun.Loading")}</div>
            </div>
            """
        )
        run_js(
            f"""
            (function(){{
              const instance = {json.dumps(instance)};
              const switchEl = document.getElementById({json.dumps(switch_id)});
              const statusEl = document.getElementById({json.dumps(status_id)});
              const text = {{
                loading: {json.dumps(t("Gui.StartupRun.Loading"))},
                enabled: {json.dumps(t("Gui.StartupRun.Enabled"))},
                disabled: {json.dumps(t("Gui.StartupRun.Disabled"))},
                setting: {json.dumps(t("Gui.StartupRun.Setting"))},
                failed: {json.dumps(t("Gui.StartupRun.Failed"))},
                unavailable: {json.dumps(t("Gui.StartupRun.Unavailable"))}
              }};

              async function refresh() {{
                switchEl.disabled = true;
                statusEl.textContent = text.loading;
                try {{
                  const resp = await fetch('/api/deploy/startup-run?instance=' + encodeURIComponent(instance), {{cache: 'no-store'}});
                  const result = await resp.json();
                  if (!result.success) {{
                    throw new Error(result.error || 'Неизвестная ошибка');
                  }}
                  switchEl.checked = result.data.enabled === true;
                  switchEl.disabled = false;
                  statusEl.textContent = result.data.enabled ? text.enabled : text.disabled;
                }} catch (err) {{
                  statusEl.textContent = text.unavailable + ': ' + (err.message || err);
                }}
              }}

              switchEl.addEventListener('change', async function() {{
                const target = switchEl.checked;
                switchEl.disabled = true;
                statusEl.textContent = text.setting;
                try {{
                  const resp = await fetch('/api/deploy/startup-run', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{instance, enabled: target}})
                  }});
                  const result = await resp.json();
                  if (!result.success) {{
                    throw new Error(result.error || 'Неизвестная ошибка');
                  }}
                  switchEl.checked = result.data.enabled === true;
                  statusEl.textContent = result.data.enabled ? text.enabled : text.disabled;
                }} catch (err) {{
                  switchEl.checked = !target;
                  statusEl.textContent = text.failed + ': ' + (err.message || err);
                  setTimeout(refresh, 1600);
                  return;
                }}
                switchEl.disabled = false;
              }});

              refresh();
            }})();
            """
        )

    def _alas_start(self):
        self.alas.start(None)

    def _simulator_start(self):
        if is_demo_mode():
            logger.info("[WebUI] DEMO=1: запуск симулятора Operation Siren пропущен.")
            return
        self.simulator.start()

    def _bind_config_watcher(self, path: List[str]) -> None:
        """为已渲染的配置控件注册一次变更监听。"""
        pin_name = "_".join(path)
        watcher_pins = getattr(self, "_config_watcher_pins", None)
        if watcher_pins is None:
            watcher_pins = set()
            self._config_watcher_pins = watcher_pins
        if pin_name in watcher_pins:
            return

        path_text = ".".join(path)

        def put_queue(value: Any) -> None:
            self.modified_config_queue.put({"name": path_text, "value": value})

        pin_on_change(name=pin_name, onchange=put_queue)
        watcher_pins.add(pin_name)

    def _alas_thread_update_config(self) -> None:
        modified = {}
        while self.alive:
            try:
                d = self.modified_config_queue.get(timeout=10)
                config_name = self.alas_name
                config_updater = self.alas_config
            except queue.Empty:
                continue
            modified[d["name"]] = d["value"]
            while True:
                try:
                    d = self.modified_config_queue.get(timeout=1)
                    modified[d["name"]] = d["value"]
                except queue.Empty:
                    self._save_config(modified, config_name, config_updater)
                    modified.clear()
                    break

    def _save_config(
        self,
        modified: Dict[str, Any],
        config_name: str,
        config_updater: Any = State.config_updater,
    ) -> None:
        if os.environ.get("DEMO") == "1":
            return

        try:
            skip_time_record = False
            valid = []
            invalid = []
            config = config_updater.read_file(config_name)
            n = current_time()
            for p, v in deep_iter(config, depth=3):
                if p[-1].endswith("un") and not isinstance(v, bool):
                    if (v - n).days >= 31:
                        deep_set(config, p, "")
            for k, v in modified.copy().items():
                arg_def = deep_get(self.ALAS_ARGS, k, {})
                valuetype = (
                    arg_def.get("valuetype") if isinstance(arg_def, dict) else None
                )
                widget_type = arg_def.get("type") if isinstance(arg_def, dict) else None
                options = arg_def.get("option") if isinstance(arg_def, dict) else None
                # YAML 参数定义允许省略类型；运行时解析器会处理 None，
                # 这里保留原行为并向类型检查器声明该动态边界。
                v = parse_pin_value(
                    v, cast(str, valuetype), cast(str, widget_type), options
                )
                validate = deep_get(self.ALAS_ARGS, k + ".validate")
                if not len(str(v)):
                    default = deep_get(self.ALAS_ARGS, k + ".value")
                    modified[k] = default
                    deep_set(config, k, default)
                    valid.append(k)
                    pin["_".join(k.split("."))] = default

                elif not validate or re_fullmatch(validate, v):
                    deep_set(config, k, v)
                    modified[k] = v
                    valid.append(k)
                    for set_key, set_value in config_updater.save_callback(k, v):
                        modified[set_key] = set_value
                        deep_set(config, set_key, set_value)
                        valid.append(set_key)
                        pin["_".join(set_key.split("."))] = to_pin_value(set_value)
                    # ==================== 自定义弹窗逻辑 ====================
                    # 当保存侵蚀1兑换凭证保留值为 0 时弹出提示
                    try:
                        is_zero_preserve = int(cast(Any, v)) == 0
                    except (TypeError, ValueError):
                        is_zero_preserve = False
                    if (
                        k
                        in [
                            "OpsiHazard1Leveling.OpsiHazard1Leveling.OperationCoinsPreserve",
                            "OpsiScheduling.OpsiScheduling.OperationCoinsPreserve",
                        ]
                        and is_zero_preserve
                    ):
                        from pywebio.output import popup, put_html, PopupSize

                        popup(
                            "Сохранение нулевого резерва монет",
                            [
                                put_html(
                                    '<div style="line-height:1.8;font-size:14px;">'
                                    "Нулевой резерв монет OpSi может остановить фарм зоны "
                                    "коррозии 1: без монет нельзя покупать контейнеры с "
                                    "очками действия в магазине мяуфицера.<br><br>"
                                    "Рекомендуется сохранить резерв из настроек умного "
                                    "планирования. Перед продолжением убедитесь, что понимаете "
                                    "влияние этой настройки на баланс монет и очков действия."
                                    "</div>"
                                )
                            ],
                            size=PopupSize.LARGE,
                        )
                    # ========================================================
                else:
                    modified.pop(k)
                    invalid.append(k)
                    logger.warning(f"[WebUI — Конфигурация задач] Недопустимое значение {v} для ключа {k}; сохранение пропущено")
            self.pin_remove_invalid_mark(valid)
            self.pin_set_invalid_mark(invalid)
            if modified:
                toast(
                    t("Gui.Toast.ConfigSaved"),
                    duration=1,
                    position="right",
                    color="success",
                )
                logger.info(
                    f"[WebUI — Конфигурация задач] Сохранение конфигурации {filepath_config(config_name)}, {dict_to_kv(modified)}"
                )
                config_updater.write_file(config_name, config)
        except Exception as e:
            logger.exception(e)
