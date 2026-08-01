"""WebUIASGI生命周期管理"""

from module.webui.app_dependencies import (
    ProcessManager,
    RemoteAccess,
    State,
    close_discord_rpc,
    init_discord_rpc,
    lang,
    logger,
    os,
    start_ocr_server_process,
    stop_ocr_server_process,
    task_handler,
)

from module.webui.app_helpers import (
    is_demo_mode,
)


def _clearup_step(name, handler) -> bool:
    """执行单项清理；一项失败不应阻断其余资源回收。"""
    try:
        return handler() is not False
    except Exception as exc:
        logger.exception_context(
            title=f'Ошибка очистки WebUI: {name}',
            exc=exc,
            impact='Очистка остальных ресурсов WebUI будет продолжена.',
            action='Проверьте журнал завершения ресурса и наличие оставшихся дочерних процессов.',
            level=40,
        )
        return False


def startup() -> None:
    """Инициализировать WebUI после явной миграции UI locale."""
    from deploy.language_migration import migrate_deploy_language

    result = migrate_deploy_language()
    if result.changed:
        logger.info("[WebUI] Старое значение Language безопасно изменено на ru-RU")
    State.init()
    lang.reload()
    task_handler.start()
    if State.deploy_config.DiscordRichPresence:
        init_discord_rpc()
    if State.deploy_config.StartOcrServer and not is_demo_mode():
        start_ocr_server_process(State.deploy_config.OcrServerPort)
    if State.deploy_config.EnableRemoteAccess and (
        State.deploy_config.Password is not None or os.environ.get("DEMO") == "1"
    ):
        task_handler.add(RemoteAccess.keep_ssh_alive(), 60)


def clearup() -> bool:
    """停止 WebUI 进程级资源，避免热重载遗留子进程。"""
    with State.cleanup_lock:
        if State._clearup:
            return True

        logger.info("[WebUI-生命周期] 开始清理")
        success = _clearup_step("任务处理器", task_handler.stop)

        for name, handler in (
            ("远程访问", RemoteAccess.kill_ssh_process),
            ("Discord RPC", close_discord_rpc),
            ("OCR 服务", stop_ocr_server_process),
        ):
            success = _clearup_step(name, handler) and success

        try:
            instances = ProcessManager.running_instances()
        except Exception as exc:
            logger.exception_context(
                title='Ошибка очистки WebUI: не удалось получить запущенные профили',
                exc=exc,
                impact='Нельзя подтвердить остановку всех рабочих процессов AzurPilot.',
                action='Проверьте реестр процессов WebUI и состояние службы Manager.',
                level=40,
            )
            instances = []
            success = False

        for alas in instances:
            success = _clearup_step(f"AzurPilot 实例 {alas.config_name}", alas.stop) and success

        if success:
            try:
                State.clearup()
            except Exception as exc:
                logger.exception_context(
                    title='Ошибка очистки WebUI: общее состояние',
                    exc=exc,
                    impact='Manager завершён не полностью; родительский процесс принудительно закроет дерево процессов.',
                    action='Проверьте службу Manager и системные права управления процессами.',
                    level=40,
                )
                success = False
        else:
            logger.error("WebUI 清理未完成，保留 Manager 直到父进程终止进程树")
        logger.info("[WebUI-生命周期] Alas 已关闭")
        return success
