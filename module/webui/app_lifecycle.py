"""Управление жизненным циклом ASGI-приложения WebUI."""

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


def build_fleet_page_runtime_context(*, clock=None, require_ready: bool = True):
    """Собрать контекст выполнения страницы флотов в разрешённой корневой точке WebUI."""
    from module.persistence.runtime import build_runtime_fleet_page_context

    return build_runtime_fleet_page_context(
        clock=clock,
        require_ready=require_ready,
    )


def build_morale_runtime_context(*, clock=None, require_ready: bool = True):
    """Собрать общий Morale/Dorm boundary в разрешённой корневой точке WebUI."""
    from module.persistence.runtime import build_runtime_morale_context

    return build_runtime_morale_context(
        clock=clock,
        require_ready=require_ready,
    )


def _clearup_step(name, handler) -> bool:
    """Выполнить один шаг очистки, не блокируя освобождение остальных ресурсов."""
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
    from module.persistence.runtime import bootstrap_runtime_storage

    bootstrap_runtime_storage(require_ready=True)
    logger.info("[WebUI] PostgreSQL готов к работе")
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
    """Остановить ресурсы процесса WebUI без утечек при горячей перезагрузке."""
    with State.cleanup_lock:
        if State._clearup:
            return True

        logger.info("[WebUI-жизненный цикл] Начата очистка")
        success = _clearup_step("обработчик фоновых задач", task_handler.stop)

        for name, handler in (
            ("удалённый доступ", RemoteAccess.kill_ssh_process),
            ("Discord RPC", close_discord_rpc),
            ("служба OCR", stop_ocr_server_process),
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
            success = _clearup_step(f"профиль AzurPilot {alas.config_name}", alas.stop) and success

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
            logger.error(
                "Очистка WebUI не завершена; служба Manager сохранена до завершения дерева процессов родительским процессом"
            )
        logger.info("[WebUI-жизненный цикл] AzurPilot остановлен")
        return success
