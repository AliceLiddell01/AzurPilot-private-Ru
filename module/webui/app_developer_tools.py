"""WebUI调试工具和远程访问"""

from deploy.atomic import atomic_write
from module.logger import logger

from module.webui.app_dependencies import (
    DEFAULT_CONFIG_NAME,
    Optional,
    ProcessManager,
    RemoteAccess,
    State,
    Switch,
    alas_instance,
    clear,
    load_config,
    os,
    put_button,
    put_buttons,
    put_html,
    put_link,
    put_loading,
    put_row,
    put_scope,
    put_text,
    put_warning,
    t,
    toast,
    use_scope,
)
from module.webui.app_lifecycle import clearup


from module.webui.app_types import WebUIMixinBase


def prepare_webui_restart() -> bool:
    """保存当前运行实例，供新 WebUI 在重启后恢复。"""
    try:
        names = [
            f"{alas.config_name}\n" for alas in ProcessManager.running_instances()
        ]
        atomic_write("./config/reloadalas", "".join(names))
    except Exception as exc:
        logger.exception_context(
            title='Не удалось подготовить ручной перезапуск WebUI',
            exc=exc,
            impact='При продолжении запущенные профили AzurPilot не будут восстановлены автоматически.',
            action='Проверьте права записи в каталог config и повторите попытку.',
            level=50,
        )
        return False
    return True


def request_webui_restart() -> bool:
    """请求由父监督器执行手动 WebUI 重启。"""
    if State.restart_event is None:
        return False
    if not State.restart_lock.acquire(blocking=False):
        logger.info("Перезапуск WebUI уже выполняется; повторный запрос пропущен")
        return False

    try:
        if State._restart_requested:
            return True
        if not prepare_webui_restart():
            return False

        State._restart_requested = True
        try:
            if not clearup():
                logger.warning(
                    "Очистка WebUI не завершена; родительский процесс завершит всё дерево процессов"
                )
        except Exception as exc:
            logger.exception_context(
                title='Ошибка очистки при ручном перезапуске WebUI',
                exc=exc,
                impact='Родительский процесс всё равно завершит дерево процессов старой WebUI.',
                action='Проверьте журнал очистки WebUI на наличие оставшихся ресурсов.',
                level=50,
            )

        try:
            State.restart_event.set()
        except Exception as exc:
            State._restart_requested = False
            logger.exception_context(
                title='Не удалось запросить у родительского процесса перезапуск WebUI',
                exc=exc,
                impact='Текущая WebUI не завершится, а сохранённая отметка восстановления профилей останется.',
                action='Проверьте связь с родительским процессом и повторите перезапуск.',
                level=50,
            )
            return False
        return True
    finally:
        State.restart_lock.release()


class DeveloperToolsMixin(WebUIMixinBase):
    """WebUI调试工具和远程访问"""

    @use_scope("content", clear=True)
    def dev_utils(self) -> None:
        self.init_menu(name="Utils")
        self.set_title(t("Gui.MenuDevelop.Utils"))
        put_scope("develop_detail")
        def _get_debug_target_instance() -> Optional[str]:
            if getattr(self, "alas_name", ""):
                return self.alas_name
            all_instances = alas_instance()
            if all_instances:
                return all_instances[0]
            return None

        def _refresh_debug_status():
            self.set_aside_status()
            if hasattr(self, "state_switch"):
                try:
                    self.state_switch.switch()
                except Exception:
                    pass

        def _mock_icon_state(state: int, seconds: int = 10):
            target = _get_debug_target_instance()
            if not target:
                toast("Нет доступного профиля для имитации состояния значка", color="warning")
                return
            ProcessManager.get_manager(target).set_state_override(
                state, duration=seconds
            )
            _refresh_debug_status()
            toast(f"Для {target} установлено тестовое состояние {state} на {seconds} с", color="info")

        def _clear_mock_icon_state():
            target = _get_debug_target_instance()
            if not target:
                toast("Нет доступного профиля для сброса тестового состояния", color="warning")
                return
            ProcessManager.get_manager(target).clear_state_override()
            _refresh_debug_status()
            toast(f"Тестовое состояние значка {target} сброшено", color="success")

        put_buttons(
            buttons=[
                {"label": "Значок работы (10 с)", "value": 1, "color": "success"},
                {"label": "Значок ошибки (10 с)", "value": 3, "color": "danger"},
            ],
            onclick=lambda state: _mock_icon_state(state, 10),
            scope="develop_detail",
        )
        put_button(
            label="Сбросить тестовый значок",
            onclick=_clear_mock_icon_state,
            color="secondary",
            scope="develop_detail",
        )

        def _force_restart():
            if State.restart_event is None:
                toast(t("Gui.Toast.ReloadEnabled"), color="error")
                return
            if request_webui_restart():
                toast(t("Gui.Toast.AlasRestart"), duration=0, color="error")
            else:
                toast("Перезапуск WebUI отменён: операция занята или не удалось сохранить активный профиль", color="error")

        put_button(label="Перезапустить AzurPilot", onclick=_force_restart, scope="develop_detail")

        def _test_notify_announcement():
            from module.notify.notify import notify_webui

            instance = getattr(self, "alas_name", DEFAULT_CONFIG_NAME)
            notify_webui(
                instance=instance,
                title="Новое объявление!",
                content="Тест уведомления об объявлении: лаунчер должен показать отдельный заголовок.",
                updata=False,
            )
            toast("Тестовое уведомление об объявлении отправлено", color="info")

        def _test_notify_error():
            from module.notify import handle_notify

            instance = _get_debug_target_instance()
            if not instance:
                toast("Нет доступного профиля для теста уведомления об ошибке", color="warning")
                return
            config = load_config(instance)
            success = handle_notify(
                config.Error_OnePushConfig,
                title=f"Сбой AzurPilot <{instance}>",
                content=f"<{instance}>: тест уведомления разработчика об ошибке",
            )
            if success:
                toast("Тестовое уведомление об ошибке отправлено", color="success")
            else:
                toast("Не удалось отправить тестовое уведомление. Проверьте настройки уведомлений об ошибках", color="error")

        put_buttons(
            buttons=[
                {
                    "label": "Тест уведомления об объявлении",
                    "value": "announcement",
                    "color": "info",
                },
                {
                    "label": "Тест уведомления об ошибке",
                    "value": "error",
                    "color": "danger",
                },
            ],
            onclick=[
                _test_notify_announcement,
                _test_notify_error,
            ],
            scope="develop_detail",
        )

    @use_scope("content", clear=True)
    def dev_remote(self) -> None:
        self.init_menu(name="Remote")
        self.set_title(t("Gui.MenuDevelop.Remote"))
        put_scope("develop_detail")
        with use_scope("develop_detail"):
            put_row(
                content=[put_scope("remote_loading"), None, put_scope("remote_state")],
                size="auto .25rem 1fr",
            )
            put_scope("remote_info")

        def u(state):
            if state == -1:
                return
            status_map = {
                "direct_p2p": t("Gui.Remote.StatusDirect"),
                "turn_relay": t("Gui.Remote.StatusTurn"),
                "ssh_forward": t("Gui.Remote.StatusSsh"),
                "waiting_peer": t("Gui.Remote.StatusSignaling"),
                "signaling": t("Gui.Remote.StatusSignaling"),
                "starting": t("Gui.Remote.StatusStarting"),
                "dependency_missing": t("Gui.Remote.StatusSsh"),
                "failed": t("Gui.Remote.StatusFailed"),
            }
            clear("remote_loading")
            clear("remote_state")
            clear("remote_info")
            if state in (1, 2):
                put_loading("grow", "success", "remote_loading").style(
                    "--loading-grow--"
                )
                remote_status = RemoteAccess.get_connection_state()
                put_text(
                    f"{t('Gui.Remote.Running')} · {status_map.get(remote_status, remote_status)}",
                    scope="remote_state",
                )
                put_text(t("Gui.Remote.EntryPoint"), scope="remote_info")
                entrypoint = RemoteAccess.get_entry_point()
                if entrypoint:
                    if State.electron:  # Prevent click into url in electron client
                        put_text(entrypoint, scope="remote_info").style(
                            "text-decoration-line: underline"
                        )
                    else:
                        put_link(name=entrypoint, url=entrypoint, scope="remote_info")
                else:
                    put_text("Загрузка...", scope="remote_info")
                remote_error = RemoteAccess.get_error()
                if remote_error and remote_status in ("dependency_missing", "failed"):
                    put_warning(remote_error, closable=False, scope="remote_info")
            elif state in (0, 3, 4):
                put_loading("border", "secondary", "remote_loading").style(
                    "--loading-border-fill--"
                )
                if State.deploy_config.EnableRemoteAccess and (
                    State.deploy_config.Password or os.environ.get("DEMO") == "1"
                ):
                    put_text(t("Gui.Remote.NotRunning"), scope="remote_state")
                else:
                    put_text(t("Gui.Remote.NotEnable"), scope="remote_state")
                put_text(t("Gui.Remote.ConfigureHint"), scope="remote_info")
                url = "http://app.azurlane.cloud/en.html"
                put_html(
                    f'<a href="{url}" target="_blank">{url}</a>', scope="remote_info"
                )
                if state == 3:
                    put_warning(
                        t("Gui.Remote.SSHNotInstall"),
                        closable=False,
                        scope="remote_info",
                    )

        remote_switch = Switch(
            status=u, get_state=RemoteAccess.get_state, name="remote"
        )

        self.task_handler.add(remote_switch.g(), delay=1, pending_delete=True)
