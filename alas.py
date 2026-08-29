import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta

import inflection
from cached_property import cached_property

from module.base.decorator import del_cached_property
from module.base.ssh import clear_ssh_host_key
from module.config.config import AzurLaneConfig, TaskEnd
from module.config.deep import deep_get, deep_set
from module.config.time_source import now as current_time
from module.config.utils import (
    DEFAULT_CONFIG_NAME,
    ensure_time,
    filepath_i18n,
    get_server_last_update,
    get_server_next_update,
    read_file,
)
from module.config.locale import UI_LOCALE
from module.exception import *
from module.logger import logger
from module.logging_context import task_logging_context
from module.notify import handle_notify, notify_webui
from module.persistence.runtime import bootstrap_runtime_storage

# 缓存 i18n 任务名查找
_i18n_task_names = None
def _get_task_display_name(task_command):
    """从 i18n 获取任务的本地化显示名，找不到则返回英文名"""
    global _i18n_task_names
    if _i18n_task_names is None:
        _i18n_task_names = {}
        try:
            i18n_file = filepath_i18n(UI_LOCALE)
            if os.path.exists(i18n_file):
                with open(i18n_file, encoding='utf-8') as f:
                    data = json.load(f)
                _i18n_task_names = {
                    k: v.get('name', k)
                    for k, v in data.get('Task', {}).items()
                }
        except Exception:
            pass
    return _i18n_task_names.get(task_command, task_command)




class AzurLaneAutoScript:
    stop_event: threading.Event = None

    def __init__(self, config_name=DEFAULT_CONFIG_NAME):
        logger.hr('Запуск', level=0)
        bootstrap_runtime_storage(require_ready=True)
        logger.info('[Хранилище] PostgreSQL готов к работе')
        self.config_name = config_name
        # 跳过启动后的第一次 Restart 任务
        self.is_first_task = True
        # 任务失败计数器，key 为任务名，value 为连续失败次数
        self.failure_record = {}
        # Счётчики последовательных зависаний игры и сбоев ADB для ограничения циклов восстановления.
        self.consecutive_game_stuck = 0
        self.consecutive_adb_offline = 0
        self._last_emulator_recovery_mode = ''
        self._emulator_recovery_transport_lost = False
        # 上次计划重启模拟器的时间戳
        self.last_emulator_restart_time = time.monotonic()
        self._manual_scan_wakeup = False

    def _try_restart_emulator(self, *, reason='adb_offline', verify_game=False):
        """Выполнить одну проверяемую цепочку восстановления эмулятора.

        Причины восстановления имеют независимую политику:
        - adb_offline использует Error_AdbOfflineRestart и отдельный ADB budget;
        - game_stuck разрешён только Error_GameStuckRestart после провала Stage 1;
        - scheduled не использует error budget и никогда не включает hard kill.

        При verify_game=True успех признаётся только после fresh Device и
        повторной Stage 1 login/UI health validation Azur Lane.
        """
        self._emulator_recovery_transport_lost = False

        if reason == 'adb_offline':
            if not self.config.Error_AdbOfflineRestart:
                logger.error_context(
                    title='Автоматический перезапуск эмулятора отключён',
                    reason='Параметр Error.AdbOfflineRestart отключён.',
                    impact='После отключения эмулятора автоматическое восстановление невозможно; текущая задача может завершиться.',
                    action='Проверьте стабильность эмулятора и при необходимости включите AdbOfflineRestart с подходящим пределом попыток.',
                    level=30,
                )
                return False

            self.consecutive_adb_offline += 1
            limit = int(self.config.Error_AdbOfflineThreshold)
            logger.warning(
                f'[Alas] EmulatorNotRunningError: последовательных сбоев '
                f'{self.consecutive_adb_offline}/{limit}'
            )
            if self.consecutive_adb_offline > limit:
                logger.error_context(
                    title='Достигнут предел автоматических перезапусков эмулятора',
                    reason=f'Число последовательных отключений эмулятора превысило установленный предел: {limit}.',
                    impact='Автоматическое восстановление остановлено; задача перейдёт в обработку ошибки.',
                    action='Проверьте, что эмулятор запущен и ADB доступен, затем перезапустите AzurPilot.',
                    level=50,
                )
                return False
        elif reason == 'game_stuck':
            if not self.config.Error_GameStuckRestart:
                logger.warning(
                    '[Alas] Эскалация к восстановлению эмулятора после game restart failed отключена '
                    'параметром Error.GameStuckRestart'
                )
                return False
        elif reason == 'scheduled':
            pass
        else:
            logger.error(f'[Alas] Неизвестная причина восстановления эмулятора: {reason}')
            return False

        allow_hard_kill = reason in {'adb_offline', 'game_stuck'}
        logger.hr('[Alas] Проверяемое восстановление эмулятора', level=1)

        recovery_started = False
        try:
            from module.recovery.emulator_recovery import recover_emulator_transport

            current_device = self.__dict__.get('device', None)
            recovery_started = True
            outcome = recover_emulator_transport(
                self.config,
                current_device=current_device,
                allow_hard_kill=allow_hard_kill,
            )
            if not outcome.success:
                if outcome.stage in {'hard-kill', 'cold-start', 'fresh-device'}:
                    if 'device' in self.__dict__:
                        del_cached_property(self, 'device')
                    self._emulator_recovery_transport_lost = True
                logger.error_context(
                    title='Не удалось восстановить эмулятор',
                    reason=f'Цепочка восстановления завершилась на этапе: {outcome.stage}.',
                    impact='Эмулятор или Azur Lane могут оставаться недоступны; ложный success не возвращается.',
                    action='Проверьте журнал этапов recovery, identity выбранного instance и состояние ADB.',
                    level=40,
                )
                return False

            if 'device' in self.__dict__:
                del_cached_property(self, 'device')
            self.__dict__['device'] = outcome.device
            self._last_emulator_recovery_mode = outcome.mode

            if verify_game:
                logger.info('[Alas] Новый Device создан; запускается финальная проверка Azur Lane')
                if not self._try_restart_game():
                    logger.error_context(
                        title='Эмулятор восстановлен, но Azur Lane не прошла финальную проверку',
                        reason='game restart failed: после cold start новый Device не подтвердил login/UI health.',
                        impact='Вся Stage 2 recovery-chain считается неуспешной; повторная эскалация в этом вызове запрещена.',
                        action='Проверьте снимки после загрузки эмулятора и состояние клиента Azur Lane.',
                        level=40,
                    )
                    return False
                logger.info('[Alas] Финальная проверка Azur Lane после восстановления эмулятора пройдена')

            logger.info(
                f'[Alas] Восстановление эмулятора завершено успешно; режим={outcome.mode}, '
                f'instance={outcome.instance_name}'
            )
            return True
        except Exception as e:
            if recovery_started:
                if 'device' in self.__dict__:
                    del_cached_property(self, 'device')
                self._emulator_recovery_transport_lost = True
            logger.exception_context(
                title='Не удалось перезапустить эмулятор',
                exc=e,
                impact='Эмулятор может оставаться недоступным; текущую задачу восстановить невозможно.',
                action='Проверьте права процесса эмулятора, службу ADB и параметры управления эмулятором.',
            )
            return False

    def _start_emulator_after_long_wait(self):
        """
        长时间等待关闭模拟器后，显式启动模拟器。

        这是省资源功能的正常恢复路径，不受 ADB 离线重启开关和次数限制。

        Returns:
            bool: 启动成功返回 True，失败返回 False。
        """
        logger.hr('[Alas] Запуск эмулятора после длительного ожидания', level=1)
        try:
            from module.device.platform import Platform

            platform = Platform(self.config, connect=False)
            if platform.emulator_instance is None:
                logger.warning('[Alas] Экземпляр эмулятора не найден; запуск после длительного ожидания невозможен')
                return False

            if platform.emulator_start():
                logger.info('[Alas] Эмулятор запущен после длительного ожидания')
                if 'device' in self.__dict__:
                    del_cached_property(self, 'device')
                return True

            logger.warning('[Alas] Не удалось запустить эмулятор после длительного ожидания; продолжается восстановление планировщика')
            return False
        except Exception as e:
            logger.warning(f'[Alas] Не удалось запустить эмулятор после длительного ожидания; продолжается восстановление планировщика: {e}')
            return False

    @cached_property
    def config(self):
        try:
            config = AzurLaneConfig(config_name=self.config_name)
            return config
        except RequestHumanTakeover:
            logger.error_context(
                title='Для инициализации конфигурации требуется вмешательство пользователя',
                reason='Загрузка или проверка конфигурации завершилась ошибкой; автоматическое исправление невозможно.',
                impact='Планировщик не может быть запущен.',
                action='Проверьте файл конфигурации и последний стек ошибки, исправьте параметры и перезапустите приложение.',
                level=50,
            )
            exit(1)
        except Exception as e:
            logger.exception_context(
                title='Не удалось инициализировать конфигурацию', exc=e,
                impact='Планировщик не может быть запущен.',
                action='Проверьте формат конфигурации, имена параметров и права доступа к файлам в каталоге config.',
                level=50,
            )
            exit(1)

    @cached_property
    def device(self):
        try:
            from module.device.device import Device
            device = Device(config=self.config)
            return device
        except RequestHumanTakeover:
            logger.error_context(
                title='Для инициализации устройства требуется вмешательство пользователя',
                reason='Подключение к устройству или проверка его параметров завершилась ошибкой; автоматическое исправление невозможно.',
                impact='Планировщик не может управлять эмулятором.',
                action='Убедитесь, что эмулятор запущен, ADB доступен и разрешение равно 1280x720, затем перезапустите приложение.',
                level=50,
            )
            exit(1)
        except Exception as e:
            logger.exception_context(
                title='Не удалось инициализировать устройство', exc=e,
                impact='Планировщик не может управлять эмулятором.',
                action='Проверьте эмулятор, подключение ADB и выбранные способы получения снимков и управления.',
                level=50,
            )
            exit(1)

    @cached_property
    def checker(self):
        try:
            from module.server_checker import ServerChecker
            checker = ServerChecker(server=self.config.Emulator_ServerName)
            return checker
        except Exception as e:
            logger.exception_context(
                title='Не удалось инициализировать проверку состояния сервера', exc=e,
                impact='Невозможно определить состояние технического обслуживания сервера; планировщик не может продолжить работу.',
                action='Проверьте сетевое подключение, параметры сервера и связанные зависимости, затем перезапустите приложение.',
                level=50,
            )
            exit(1)

    def _build_fleet_autoscan_controller(self):
        """Создать Formation controller поверх текущего scheduler-owned Device."""

        from module.formation.navigation import FormationFleetController

        device = self.device
        device.stuck_record_clear()
        device.click_record_clear()
        return FormationFleetController(config=self.config, device=device)

    @cached_property
    def fleet_autoscan(self):
        """Scheduler coordinator с ленивым Device/controller boundary."""

        from module.application.fleet_autoscan import FleetAutoScanCoordinator
        from module.persistence.runtime import build_runtime_fleet_state_context

        context = build_runtime_fleet_state_context(
            self._build_fleet_autoscan_controller,
            require_ready=False,
        )
        return FleetAutoScanCoordinator(context.state_service)

    @cached_property
    def fleet_manual_scan(self):
        """Координатор устойчивых ручных команд на текущем Device worker-процесса."""

        from module.persistence.runtime import build_runtime_fleet_manual_scan_context

        context = build_runtime_fleet_manual_scan_context(
            self._build_fleet_autoscan_controller,
            require_ready=False,
        )
        return context.coordinator

    def _run_fleet_manual_scan_if_pending(self):
        execution = self.fleet_manual_scan.process_next(self.config_name)
        if execution is None:
            return None
        command = execution.command
        selected = ", ".join(map(str, command.selection.fleet_indices))
        logger.hr('[Alas] Ручное сканирование флотов', level=1)
        logger.info(
            f'[Alas] Ручное сканирование завершено: флоты={selected}, '
            f'статус={command.status.value}'
        )
        if execution.batch_result.failed_fleet_index is not None:
            logger.warning(
                '[Alas] Ручное сканирование остановлено на флоте '
                f'{execution.batch_result.failed_fleet_index}'
            )
        return execution

    def _check_sensitive_exit(self, command, error):
        """
        检查当前任务是否为敏感任务，如果是则直接退出。

        敏感任务出错时不做任何重启或恢复，完全停止 Alas 运行。

        Args:
            command (str): 任务方法名（下划线形式，如 opsi_cross_month）。
            error (Exception): 触发的异常对象。

        Returns:
            bool: True 表示已退出（不会返回），False 表示非敏感任务，继续原有逻辑。
        """
        task_name = inflection.camelize(command)
        sensitive = self.config.cross_get(
            keys=f'{task_name}.Scheduler.Sensitive', default=False
        )
        if not sensitive:
            return False

        logger.error_context(
            title=f'Ошибка чувствительной задачи; автоматический перезапуск запрещён ({task_name})',
            reason=f'Задача вызвала {type(error).__name__} и помечена как чувствительная к перезапуску.',
            impact='Чтобы избежать повреждения состояния или данных, AzurPilot завершит работу.',
            action='Изучите сохранённое состояние и вручную проверьте игру; устраните причину или исправьте конфигурацию перед следующим запуском.',
            exc=error,
            level=50,
        )
        handle_notify(
            self.config.Error_OnePushConfig,
            title=f"AzurPilot <{self.config_name}>: ошибка чувствительной задачи",
            content=f"<{self.config_name}> Чувствительная задача `{task_name}` завершилась с ошибкой; AzurPilot остановлен\n{error}",
        )
        notify_webui(
            self.config_name,
            title=f"Ошибка чувствительной задачи {task_name}; AzurPilot остановлен",
            content=f"Задача {task_name} является чувствительной, поэтому после ошибки автоматический перезапуск не выполняется.\n{error}",
        )
        exit(1)

    def _record_dev_runtime_error(self, exception, *, phase, task=None):
        """Передать ошибку во внутренний перехватчик диагностики без изменения пути восстановления."""
        if not os.environ.get("AZURPILOT_DEV_SESSION_ID"):
            return
        try:
            from module.dev_runtime.hooks import record_runtime_error

            record_runtime_error(
                self.config_name,
                exception,
                phase=phase,
                task=task,
            )
        except Exception:
            return

    def _record_dev_runtime_task_started(self, task):
        """Зафиксировать начало задачи только на канонической границе планировщика."""
        if not os.environ.get("AZURPILOT_DEV_SESSION_ID"):
            return
        try:
            from module.dev_runtime.hooks import record_task_started

            record_task_started(self.config_name, task)
        except Exception:
            return

    def _record_dev_runtime_task_finished(self, task):
        """Зафиксировать возврат штатного исполнителя задачи без заключения PASS/FAIL."""
        if not os.environ.get("AZURPILOT_DEV_SESSION_ID"):
            return
        try:
            from module.dev_runtime.hooks import record_task_finished

            record_task_finished(self.config_name, task)
        except Exception:
            return

    def _try_restart_game(self):
        """Перезапустить только Azur Lane и подтвердить восстановление через login/UI flow."""
        from module.handler.login import LoginHandler

        logger.hr('[Alas] Проверяемый перезапуск Azur Lane', level=1)
        try:
            LoginHandler(self.config, device=self.device).app_restart()
        except Exception as e:
            logger.exception_context(
                title='Не удалось восстановить Azur Lane после перезапуска',
                reason='game restart failed: существующий login/UI health check не завершился успешно.',
                impact='Текущая ступень восстановления игры завершилась ошибкой.',
                action='Проверьте сохранённые снимки и журнал; scheduler применит разрешённую политику следующей ступени.',
                exc=e,
                level=40,
            )
            return False

        logger.info('[Alas] Azur Lane восстановлена: post-restart login/UI health check успешно завершён')
        return True

    @task_logging_context
    def run(self, command, skip_first_screenshot=False):
        """
        执行指定任务命令，捕获异常并决定后续行为。

        根据异常类型自动判断：重启游戏、重启模拟器、请求人工介入或直接终止。
        敏感任务出错时直接停止，不做任何重启。

        任务执行前会进行一次截图（除非 skip_first_screenshot=True）。

        Args:
            command (str): 任务方法名（驼峰转下划线后的形式）。
            skip_first_screenshot (bool): 是否跳过执行前的首次截图。

        Returns:
            bool | str:
                True — 任务成功完成。
                False — 不可恢复的失败，计入连续失败限制。
                'recoverable' — 可恢复的失败，不计入连续失败限制。
        """
        record_dev_runtime_error = getattr(
            self,
            "_record_dev_runtime_error",
            lambda *_args, **_kwargs: None,
        )
        try:
            if not skip_first_screenshot:
                self.device.screenshot()
            self.__getattribute__(command)()
            return True
        except TaskEnd:
            return True
        except GameNotRunningError as e:
            record_dev_runtime_error(e, phase="task", task=command)
            # 游戏未运行，调度 Restart 任务自动恢复
            logger.error_context(
                title='Игровой процесс не запущен',
                reason='Перед выполнением задачи процесс Azur Lane не обнаружен.',
                impact='Текущая задача пропущена; планировщик автоматически назначит задачу Restart.',
                action='Обычно действие не требуется. При повторении проверьте имя пакета игры, состояние эмулятора и процедуру входа.',
                exc=e,
                level=30,
                # 预期恢复路径仅保留异常摘要，避免堆栈淹没后续重启日志。
                with_traceback=False,
            )
            self._check_sensitive_exit(command, e)
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config_name}>: предупреждение",
                content=f"<{self.config_name}> Игра не запущена; будет выполнен автоматический перезапуск",
            )
            notify_webui(
                self.config_name,
                title=f"<{self.config_name}>: предупреждение",
                content=f"<{self.config_name}> Игра не запущена; будет выполнен автоматический перезапуск",
            )
            self.config.task_call('Restart')
            return 'recoverable'
        except (GameStuckError, GameTooManyClickError) as e:
            record_dev_runtime_error(e, phase="task", task=command)
            logger.error_context(
                title='Игра не отвечает на действия',
                reason='Изображение не меняется в течение допустимого времени либо одна кнопка нажата слишком много раз.',
                impact='Текущая задача прервана; AzurPilot сначала перезапустит только Azur Lane и проверит фактическое восстановление.',
                action='Убедитесь, что эмулятор не управляется вручную; проверьте способ получения снимков, разрешение игры и версию ресурсов.',
                exc=e,
            )
            self.save_error_log()
            self._check_sensitive_exit(command, e)

            self.consecutive_game_stuck += 1
            limit = max(1, int(self.config.Error_GameStuckThreshold))
            logger.warning(f'[Alas] Последовательные recovery после зависания: {self.consecutive_game_stuck}/{limit}')

            if self.consecutive_game_stuck > limit:
                logger.error_context(
                    title='Достигнут предел повторных восстановлений Azur Lane',
                    reason=f'После {limit} последовательных recovery игра снова зависла; дальнейший автоматический цикл остановлен.',
                    impact='Текущая задача считается ошибочной; новая destructive recovery без свежего Stage 1 failure не запускается.',
                    action='Проверьте журнал и состояние игры; после успешной обычной задачи счётчик будет сброшен.',
                    level=40,
                )
                handle_notify(
                    self.config.Error_OnePushConfig,
                    title=f"AzurPilot <{self.config_name}>: восстановление остановлено",
                    content=f"<{self.config_name}> game restart failed: достигнут предел повторных восстановлений Azur Lane",
                )
                notify_webui(
                    self.config_name,
                    title=f"<{self.config_name}>: game restart failed",
                    content='Достигнут предел повторных восстановлений; новая эскалация MuMu не запускалась.',
                )
                return False

            logger.warning(f'[Alas] Игра зависла; выполняется проверяемый перезапуск пакета {self.device.package}')
            logger.warning('[Alas] Если вы управляете игрой вручную, остановите AzurPilot')
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config_name}>: предупреждение",
                content=f"<{self.config_name}> Игра зависла; выполняется проверяемый перезапуск Azur Lane",
            )

            if self._try_restart_game():
                logger.info('[Alas] Восстановление Azur Lane завершено успешно; MuMu не затрагивался')
                notify_webui(
                    self.config_name,
                    title=f"<{self.config_name}>: Azur Lane восстановлена",
                    content='Перезапуск игры и post-restart health validation завершились успешно; MuMu не перезапускался.',
                )
                return 'recoverable'

            logger.error_context(
                title='Перезапуск Azur Lane не восстановил игру',
                reason='game restart failed: post-restart health validation завершилась ошибкой.',
                impact='Stage 1 завершился ошибкой; при разрешённой политике начинается Stage 2 recovery выбранного эмулятора.',
                action='AzurPilot сначала запросит штатную остановку конкретного instance и только при доказанно живом target сможет применить instance-scoped hard kill.',
                level=40,
            )

            if self._try_restart_emulator(reason='game_stuck', verify_game=True):
                mode = getattr(self, '_last_emulator_recovery_mode', 'graceful')
                if mode == 'hard-kill':
                    title = f"<{self.config_name}>: MuMu восстановлен после hard kill"
                    content = 'Обычный перезапуск Azur Lane не помог; выбранный MuMu instance восстановлен через instance-scoped hard kill и финальную UI-проверку.'
                else:
                    title = f"<{self.config_name}>: MuMu восстановлен штатно"
                    content = 'Обычный перезапуск Azur Lane не помог; выбранный MuMu instance штатно перезапущен и Azur Lane прошла финальную UI-проверку.'
                handle_notify(
                    self.config.Error_OnePushConfig,
                    title=f"AzurPilot <{self.config_name}>: восстановление завершено",
                    content=f"<{self.config_name}> {content}",
                )
                notify_webui(self.config_name, title=title, content=content)
                return 'recoverable'

            logger.error_context(
                title='Полная цепочка восстановления не завершилась успешно',
                reason='game restart failed: Stage 2 emulator recovery отключена или завершилась ошибкой.',
                impact='Текущая задача считается ошибочной; повторная Stage 2 эскалация внутри этого инцидента не выполняется.',
                action='Проверьте журнал этапов recovery, права управления MuMu, ADB и финальное состояние Azur Lane.',
                level=40,
            )
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config_name}>: game restart failed",
                content=f"<{self.config_name}> Azur Lane не восстановилась после обычного перезапуска; Stage 2 recovery не завершилась успешно",
            )
            notify_webui(
                self.config_name,
                title=f"<{self.config_name}>: game restart failed",
                content='Полная цепочка восстановления Azur Lane и эмулятора завершилась ошибкой.',
            )
            return False
        except GameBugError as e:
            record_dev_runtime_error(e, phase="task", task=command)
            # 游戏客户端 bug，重启游戏修复
            logger.error_context(
                title='Ошибка игрового клиента',
                reason='Обнаружено некорректное состояние клиента Azur Lane.',
                impact='Текущая задача прервана; выполняется перезапуск игры для восстановления.',
                action='Дождитесь автоматического перезапуска. При повторении обновите игру и AzurPilot и сохраните данные об ошибке.',
                exc=e,
            )
            self.save_error_log()
            self._check_sensitive_exit(command, e)
            logger.warning('[Alas] Клиент Azur Lane завершился с ошибкой, которую AzurPilot не может обработать')
            logger.warning(f'[Alas] Перезапуск {self.device.package} для восстановления')
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config_name}>: предупреждение",
                content=f"<{self.config_name}> Ошибка игрового клиента; будет выполнен автоматический перезапуск",
            )
            notify_webui(
                self.config_name,
                title=f"<{self.config_name}>: предупреждение",
                content=f"<{self.config_name}> Ошибка игрового клиента; будет выполнен автоматический перезапуск",
            )
            self.config.task_call('Restart')
            self.device.sleep(10)
            return 'recoverable'
        except GamePageUnknownError as e:
            record_dev_runtime_error(e, phase="task", task=command)
            logger.info('[Alas] Возможны техническое обслуживание сервера или разрыв сети; проверяется состояние сервера')
            self.checker.check_now()
            if self.checker.is_available():
                logger.error_context(
                    title='Игровая страница не распознана',
                    reason='Сервер доступен, но текущий снимок не соответствует ни одной известной странице игры.',
                    impact='Безопасное продолжение задачи невозможно; планировщик завершит работу.',
                    action='Проверьте версию игры, сервер и разрешение. Если проблема появилась после обновления, обновите ресурсы AzurPilot.',
                    exc=e,
                    level=50,
                )
                self.save_error_log()
                handle_notify(
                    self.config.Error_OnePushConfig,
                    title=f"AzurPilot <{self.config_name}>: аварийное завершение",
                    content=f"<{self.config_name}> GamePageUnknownError",
                )
                notify_webui(
                    self.config_name,
                    title=f"Аварийное завершение {self.config_name}",
                    content=f"Причина: GamePageUnknownError",
                )
                exit(1)
            else:
                self.checker.wait_until_available()
                return False
        except ScriptError as e:
            record_dev_runtime_error(e, phase="task", task=command)
            logger.exception_context(
                title='Ошибка выполнения сценария задачи', exc=e,
                impact='Текущую задачу продолжить невозможно; планировщик завершит работу и сохранит данные об ошибке.',
                action='Определите причину по стеку. Если это регрессия новой версии, приложите журнал ошибки и снимки экрана.',
                level=50,
            )
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config_name}>: аварийное завершение",
                content=f"<{self.config_name}> ScriptError",
            )
            notify_webui(
                self.config_name,
                title=f"Аварийное завершение {self.config_name}",
                content=f"Причина: ScriptError",
            )
            raise
        except EmulatorNotRunningError as e:
            record_dev_runtime_error(e, phase="task", task=command)
            # 模拟器离线或死机，尝试自动重启
            logger.error_context(
                title='Соединение с эмулятором потеряно',
                reason='Во время выполнения задачи эмулятор или устройство ADB стали недоступны.',
                impact='Текущая задача прервана; система попробует перезапустить эмулятор согласно конфигурации.',
                action='Убедитесь, что эмулятор и служба ADB работают. При повторении проверьте порты, прокси и настройки поддержания работы эмулятора.',
                exc=e,
            )
            self.save_error_log()
            self._check_sensitive_exit(command, e)
            if self._try_restart_emulator(reason='adb_offline'):
                # 重启成功，调度 Restart 任务恢复游戏
                self.config.task_call('Restart')
                handle_notify(
                    self.config.Error_OnePushConfig,
                    title=f"AzurPilot <{self.config_name}>: предупреждение",
                    content=f"<{self.config_name}> Эмулятор был недоступен и автоматически восстановлен",
                )
                notify_webui(
                    self.config_name,
                    title=f"{self.config_name}: эмулятор восстановлен",
                    content=f"Эмулятор был недоступен и прошёл проверяемую цепочку восстановления",
                )
                return 'recoverable'
            else:
                # 重启失败或未启用自动重启，终止程序
                logger.error_context(
                    title='Автоматическое восстановление эмулятора невозможно',
                    reason='Перезапуск отключён, завершился ошибкой либо достиг предела попыток.',
                    impact='Планировщик завершит работу; выполнение задач остановлено.',
                    action='Запустите эмулятор вручную, убедитесь, что он виден через ADB, и перезапустите AzurPilot.',
                    level=50,
                )
                handle_notify(
                    self.config.Error_OnePushConfig,
                    title=f"AzurPilot <{self.config_name}>: аварийное завершение",
                    content=f"<{self.config_name}> EmulatorNotRunningError",
                )
                notify_webui(
                    self.config_name,
                    title=f"Аварийное завершение {self.config_name}",
                    content=f"Причина: EmulatorNotRunningError",
                )
                exit(1)
        except RequestHumanTakeover as e:
            record_dev_runtime_error(e, phase="task", task=command)
            logger.error_context(
                title='Требуется вмешательство пользователя',
                reason='Автоматизация не может безопасно определить или исправить текущее состояние.',
                impact='Планировщик завершит работу, чтобы избежать ошибочных действий.',
                action='Изучите сохранённое состояние и стек и выполните рекомендации журнала перед повторным запуском.',
                level=50,
            )
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config_name}>: аварийное завершение",
                content=f"<{self.config_name}> RequestHumanTakeover",
            )
            notify_webui(
                self.config_name,
                title=f"Аварийное завершение {self.config_name}",
                content=f"Причина: требуется вмешательство пользователя",
            )
            exit(1)
        except AutoSearchSetError as e:
            record_dev_runtime_error(e, phase="task", task=command)
            logger.error_context(
                title='Не удалось настроить автоматический поиск',
                reason='Игру не удалось переключить в требуемый режим автоматического поиска.',
                impact='Безопасное продолжение задачи невозможно; планировщик завершит работу.',
                action='Проверьте состав флота, ограничения этапа и текущую страницу; настройте автоматический поиск вручную и перезапустите приложение.',
                exc=e,
                level=50,
            )
            exit(1)
        except Exception as e:
            record_dev_runtime_error(e, phase="task", task=command)
            logger.exception_context(
                title=f'Необработанная ошибка при выполнении задачи ({command})', exc=e,
                impact='Результат текущей задачи невозможно определить; планировщик сохранит данные об ошибке и завершит работу.',
                action='Изучите log.txt, снимки и полный стек в сохранённых данных и определите, требуется ли обновление ресурсов или регистрация проблемы.',
                level=50,
            )
            self.save_error_log()
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config_name}>: аварийное завершение",
                content=f"<{self.config_name}> Произошла необработанная ошибка",
            )
            notify_webui(
                self.config_name,
                title=f"Аварийное завершение {self.config_name}",
                content=f"Причина: необработанная ошибка",
            )
            raise

    def keep_last_errlog(self, folder_path, n: int = 30):
        """
        清理旧的错误日志文件夹，只保留最近的 n 个。

        Args:
            folder_path (str): 错误日志根目录路径。
            n (int): 保留的文件夹数量，<=0 时不清理。
        """
        if n <= 0:
            return
        folders = [
            os.path.join(folder_path, f)
            for f in os.listdir(folder_path)
            if os.path.isdir(os.path.join(folder_path, f))
        ]
        for folder in folders[:-n]:
            shutil.rmtree(folder)

    def save_error_log(self):
        """
        保存错误现场：最近截图和日志文件到 ./log/error/<config-name>/<timestamp>/。

        同时触发 LLM 错误分析（如果启用）。
        """
        import pathlib
        from module.base.utils import save_image
        from module.handler.sensitive_info import (handle_sensitive_image,
                                                   handle_sensitive_logs)

        # LLM 错误分析放在最前面，避免后续截图保存时二次崩溃导致分析未执行
        try:
            if hasattr(self, 'config') and getattr(self.config, 'Error_LlmAnalysis', False):
                from module.llm import analyze_exception
                import sys
                _, exc_value, _ = sys.exc_info()
                if exc_value is not None:
                    analyze_exception(self.config, exc_value)
        except Exception as e:
            logger.exception_context(
                title='Не удалось выполнить LLM-анализ ошибки',
                exc=e,
                impact='Восстановление задачи не затронуто, но для этой ошибки результат LLM-анализа не создан.',
                action='Проверьте конфигурацию LLM API, сеть и квоту; используйте сохранённые данные ошибки для диагностики.',
                level=30,
            )

        if getattr(self.config, 'Error_SaveError', False):
            config_folder = pathlib.Path(f"./log/error/{self.config_name}")
            folder = config_folder.joinpath(str(int(time.time() * 1000)))
            folder.mkdir(parents=True, exist_ok=True)
            logger.warning(f'[Alas] Сохранение журнала ошибки: {folder}')

            try:
                # 只在已经初始化了设备时才尝试保存截图，避免按需初始化时二次崩溃
                if 'device' in self.__dict__:
                    for data in self.device.screenshot_deque:
                        image_time = datetime.strftime(data['time'], '%Y-%m-%d_%H-%M-%S-%f')
                        image = handle_sensitive_image(data['image'])
                        save_image(image, f'{folder}/{image_time}.png')
            except Exception as e:
                logger.error(f"[Alas] Не удалось сохранить снимок ошибки: {e}")

            try:
                with open(logger.log_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    start = 0
                    for index, line in enumerate(lines):
                        line = line.strip(' \r\t\n')
                        if re.match('^═{15,}$', line):
                            start = index
                    lines = lines[start - 2:]
                    lines = handle_sensitive_logs(lines)
                with open(f'{folder}/log.txt', 'w', encoding='utf-8') as f:
                    f.writelines(lines)
            except Exception as e:
                logger.error(f"[Alas] Не удалось сохранить журнал ошибки: {e}")

            self.keep_last_errlog(config_folder, getattr(self.config, 'Error_SaveErrorCount', 0))

    def restart(self):
        from module.handler.login import LoginHandler
        if self.delay_due_restart():
            return
        LoginHandler(self.config, device=self.device).app_restart()
        self.delay_next_restart()

    def restart_random_delay_minutes(self):
        """获取每日重启的随机延后分钟数。"""
        random_delay = getattr(self.config, 'Restart_RandomDelay', 0)
        if isinstance(random_delay, list) and len(random_delay) == 2:
            random_delay = tuple(random_delay)
        try:
            delay = int(ensure_time(random_delay, n=1, precision=0))
        except (TypeError, ValueError):
            logger.warning(f'[Alas] Некорректная случайная задержка перезапуска: {random_delay}; используется 0 минут')
            delay = 0

        return max(delay, 0)

    def delay_due_restart(self):
        """把已排在服务器刷新整点的每日重启改排到随机延后时间。"""
        current = self.config.Scheduler_NextRun
        if not isinstance(current, datetime):
            return False

        last_update = get_server_last_update(self.config.Scheduler_ServerUpdate).replace(microsecond=0)
        if current.replace(microsecond=0) != last_update:
            return False

        delay = self.restart_random_delay_minutes()
        if delay <= 0:
            return False

        next_run = last_update + timedelta(minutes=delay)
        if next_run <= current_time().replace(microsecond=0):
            logger.info(f'[Alas] Случайная задержка ежедневного перезапуска на {delay} минут истекла; перезапуск продолжается')
            return False

        logger.info(f'[Alas] Ежедневный перезапуск совпал со временем обновления сервера и отложен на {delay} минут до {next_run}')
        self.config.task_delay(target=next_run)
        return True

    def delay_next_restart(self):
        """将下一次每日重启延后到服务器刷新后的随机时间。"""
        delay = self.restart_random_delay_minutes()
        next_run = get_server_next_update(self.config.Scheduler_ServerUpdate) + timedelta(minutes=delay)
        if delay:
            logger.info(f'[Alas] Ежедневный перезапуск отложен на {delay} минут')
        self.config.task_delay(target=next_run)

    def start(self):
        from module.handler.login import LoginHandler
        LoginHandler(self.config, device=self.device).app_start()

    def goto_main(self):
        from module.handler.login import LoginHandler
        from module.ui.ui import UI
        if self.device.app_is_running():
            logger.info('[Alas] Приложение уже запущено; переход на главную страницу')
            UI(self.config, device=self.device).ui_goto_main()
        else:
            logger.info('[Alas] Приложение не запущено; запуск и переход на главную страницу')
            LoginHandler(self.config, device=self.device).app_start()
            UI(self.config, device=self.device).ui_goto_main()

    def research(self):
        from module.research.research import RewardResearch
        RewardResearch(config=self.config, device=self.device).run()

    def commission(self):
        from module.commission.commission import RewardCommission
        RewardCommission(config=self.config, device=self.device).run()

    def tactical(self):
        from module.tactical.tactical_class import RewardTacticalClass
        RewardTacticalClass(config=self.config, device=self.device).run()

    def dorm(self):
        from module.dorm.dorm import RewardDorm
        RewardDorm(config=self.config, device=self.device).run()

    def meowfficer(self):
        from module.meowfficer.meowfficer import RewardMeowfficer
        RewardMeowfficer(config=self.config, device=self.device).run()

    def guild(self):
        from module.guild.guild_reward import RewardGuild
        RewardGuild(config=self.config, device=self.device).run()

    def reward(self):
        from module.reward.reward import Reward
        Reward(config=self.config, device=self.device).run()

    def awaken(self):
        from module.awaken.awaken import Awaken
        Awaken(config=self.config, device=self.device).run()

    def shop_frequent(self):
        from module.shop.shop_reward import RewardShop
        RewardShop(config=self.config, device=self.device).run_frequent()

    def shop_once(self):
        from module.shop.shop_reward import RewardShop
        RewardShop(config=self.config, device=self.device).run_once()

    def event_shop(self):
        from module.shop_event.shop_event import EventShop
        EventShop(config=self.config, device=self.device).run()

    def fleet_auto_scan(self):
        from module.application.fleet_autoscan import FleetAutoScanConfig

        config = FleetAutoScanConfig.from_raw(self.config.FleetAutoScan_Fleets)
        try:
            execution = self.fleet_autoscan.run(self.config_name, config)
        except Exception:
            self.config.task_delay(success=False)
            raise

        selected = ", ".join(map(str, execution.selection.fleet_indices))
        complete = ", ".join(map(str, execution.complete_fleet_indices)) or "нет"
        incomplete = ", ".join(map(str, execution.incomplete_fleet_indices)) or "нет"
        logger.hr('[Alas] Автосканирование флотов', level=1)
        logger.info(
            f'[Alas] Автосканирование завершено: флоты={selected}, '
            f'полные={complete}, неполные={incomplete}'
        )
        if execution.batch_result.failed_fleet_index is not None:
            logger.warning(
                '[Alas] Автосканирование остановлено на флоте '
                f'{execution.batch_result.failed_fleet_index}; задача отложена '
                'по Scheduler.FailureInterval'
            )
        if execution.incomplete_fleet_indices:
            self.config.task_delay(success=False)
        else:
            self.config.task_delay(server_update=True)
        return execution

    def shipyard(self):
        from module.shipyard.shipyard_reward import RewardShipyard
        RewardShipyard(config=self.config, device=self.device).run()

    def gacha(self):
        from module.gacha.gacha_reward import RewardGacha
        RewardGacha(config=self.config, device=self.device).run()

    def freebies(self):
        from module.freebies.freebies import Freebies
        Freebies(config=self.config, device=self.device).run()

    def minigame(self):
        from module.minigame.minigame import Minigame
        Minigame(config=self.config, device=self.device).run()

    def private_quarters(self):
        from module.private_quarters.private_quarters import PrivateQuarters
        PrivateQuarters(config=self.config, device=self.device).run()

    def island(self):
        from module.island.island import Island
        Island(config=self.config, device=self.device).run()

    def island_mine_forest(self):
        from module.island.island_mine_forest import IslandMineForest
        IslandMineForest(config=self.config, device=self.device).run()

    def island_farm(self):
        from module.island.island_farm import IslandFarm
        IslandFarm(config=self.config, device=self.device).run()

    def island_rancher(self):
        from module.island.island_rancher import IslandRancher
        IslandRancher(config=self.config, device=self.device).run()

    def island_fishery(self):
        from module.island.island_fishery import IslandFishery
        IslandFishery(config=self.config, device=self.device).run()

    def island_grill(self):
        from module.island.island_grill import IslandGrill
        IslandGrill(config=self.config, device=self.device).run()

    def island_teahouse(self):
        from module.island.island_teahouse import IslandTeahouse
        IslandTeahouse(config=self.config, device=self.device).run()

    def island_restaurant(self):
        from module.island.island_restaurant import IslandRestaurant
        IslandRestaurant(config=self.config, device=self.device).run()

    def island_juu_coffee(self):
        from module.island.island_juu_coffee import IslandJuuCoffee
        IslandJuuCoffee(config=self.config, device=self.device).run()

    def island_juu_eatery(self):
        from module.island.island_juu_eatery import IslandJuuEatery
        IslandJuuEatery(config=self.config, device=self.device).run()

    def island_daily_gather(self):
        from module.island.island_daily_gather import IslandDailyGather
        IslandDailyGather(config=self.config, device=self.device).run()

    def island_manufacture(self):
        from module.island.island_manufacture import IslandManufacture
        IslandManufacture(config=self.config, device=self.device).run()

    def island_air_drop(self):
        from module.island.island_air_drop import IslandAirDrop
        IslandAirDrop(config=self.config, device=self.device).run()

    def island_cargo_preparation(self):
        from module.island.island_cargo_preparation import IslandCargoPreparation
        IslandCargoPreparation(config=self.config, device=self.device).run()

    def island_business(self):
        from module.island.island_business import IslandBusiness
        IslandBusiness(config=self.config, device=self.device).run()

    def island_daily_order(self):
        from module.island.island_daily_order import IslandDailyOrder
        IslandDailyOrder(config=self.config, device=self.device).run()

    def island_daily_interact(self):
        from module.island.island_daily_interact import IslandDailyInteract
        IslandDailyInteract(config=self.config, device=self.device).run()

    def island_pearl_sell(self):
        from module.island.island_pearl_sell import IslandPearlSell
        IslandPearlSell(config=self.config, device=self.device).run()

    def daily(self):
        from module.daily.daily import Daily
        Daily(config=self.config, device=self.device).run()

    def hard(self):
        from module.hard.hard import CampaignHard
        CampaignHard(config=self.config, device=self.device).run()

    def exercise(self):
        from module.exercise.exercise import Exercise
        Exercise(config=self.config, device=self.device).run()

    def sos(self):
        from module.sos.sos import CampaignSos
        CampaignSos(config=self.config, device=self.device).run()

    def war_archives(self):
        from module.war_archives.war_archives import CampaignWarArchives
        CampaignWarArchives(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def raid_daily(self):
        from module.raid.daily import RaidDaily
        RaidDaily(config=self.config, device=self.device).run()

    def event_a(self):
        from module.event.campaign_abcd import CampaignABCD
        CampaignABCD(config=self.config, device=self.device).run()

    def event_b(self):
        from module.event.campaign_abcd import CampaignABCD
        CampaignABCD(config=self.config, device=self.device).run()

    def event_c(self):
        from module.event.campaign_abcd import CampaignABCD
        CampaignABCD(config=self.config, device=self.device).run()

    def event_d(self):
        from module.event.campaign_abcd import CampaignABCD
        CampaignABCD(config=self.config, device=self.device).run()

    def event_sp(self):
        from module.event.campaign_sp import CampaignSP
        CampaignSP(config=self.config, device=self.device).run()

    def maritime_escort(self):
        from module.event.maritime_escort import MaritimeEscort
        MaritimeEscort(config=self.config, device=self.device).run()

    def opsi_ash_assist(self):
        from module.os_ash.meta import AshBeaconAssist
        AshBeaconAssist(config=self.config, device=self.device).run()

    def opsi_ash_beacon(self):
        from module.os_ash.meta import OpsiAshBeacon
        OpsiAshBeacon(config=self.config, device=self.device).run()

    def opsi_explore(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_explore()

    def opsi_shop(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_shop()

    def opsi_voucher(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_voucher()

    def opsi_daily(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_daily()

    def opsi_obscure(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_obscure()

    def opsi_month_boss(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_month_boss()

    def opsi_abyssal(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_abyssal()

    def opsi_archive(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_archive()

    def opsi_stronghold(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_stronghold()

    def opsi_meowfficer_farming(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_meowfficer_farming()

    def opsi_hazard1_leveling(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_hazard1_leveling()

    def opsi_scheduling(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_scheduling()

    def opsi_prevent_action_point_overflow(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_prevent_action_point_overflow()

    def opsi_cross_month(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_cross_month()

    def opsi_daily_delay(self):
        from module.campaign.os_run import OSCampaignRun
        OSCampaignRun(config=self.config, device=self.device).opsi_daily_delay()

    def main(self):
        from module.campaign.run import CampaignRun
        CampaignRun(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def main2(self):
        from module.campaign.run import CampaignRun
        CampaignRun(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def main3(self):
        from module.campaign.run import CampaignRun
        CampaignRun(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def event(self):
        from module.campaign.run import CampaignRun
        CampaignRun(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def event2(self):
        from module.campaign.run import CampaignRun
        CampaignRun(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def event3(self):
        from module.campaign.run import CampaignRun
        CampaignRun(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def raid(self):
        from module.raid.run import RaidRun
        RaidRun(config=self.config, device=self.device).run()

    def raid_scuttle(self):
        from module.raid.scuttle import RaidScuttleRun
        RaidScuttleRun(config=self.config, device=self.device).run()

    def hospital(self):
        from module.event_hospital.hospital import Hospital
        Hospital(config=self.config, device=self.device).run()

    def hospital_event(self):
        from module.event_hospital.hospital_event import HospitalEvent
        HospitalEvent(config=self.config, device=self.device).run()

    def coalition(self):
        from module.coalition.coalition import Coalition
        Coalition(config=self.config, device=self.device).run()

    def coalition_sp(self):
        from module.coalition.coalition_sp import CoalitionSP
        CoalitionSP(config=self.config, device=self.device).run()

    def coalition_scuttle(self):
        from module.coalition.coalition_scuttle import CoalitionScuttleRun
        CoalitionScuttleRun(config=self.config, device=self.device).run()

    def c72_mystery_farming(self):
        from module.campaign.run import CampaignRun
        CampaignRun(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def c122_medium_leveling(self):
        from module.campaign.run import CampaignRun
        CampaignRun(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def c124_large_leveling(self):
        from module.campaign.run import CampaignRun
        CampaignRun(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def gems_farming(self):
        from module.campaign.gems_farming import GemsFarming
        GemsFarming(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def three_oil_low_cost(self):
        from module.campaign.gems_farming import GemsFarming
        GemsFarming(config=self.config, device=self.device).run(
            name=self.config.Campaign_Name, folder=self.config.Campaign_Event, mode=self.config.Campaign_Mode)

    def ambush11(self):
        from module.campaign.ambush_1_1 import Ambush11
        Ambush11(config=self.config, device=self.device).run()

    def daemon(self):
        from module.daemon.daemon import AzurLaneDaemon
        AzurLaneDaemon(config=self.config, device=self.device, task="Daemon").run()

    def opsi_daemon(self):
        from module.daemon.os_daemon import AzurLaneDaemon
        AzurLaneDaemon(config=self.config, device=self.device, task="OpsiDaemon").run()

    def event_story(self):
        from module.eventstory.eventstory import EventStory
        EventStory(config=self.config, device=self.device, task="EventStory").run()

    def box_disassemble(self):
        from module.storage.box_disassemble import StorageBox
        StorageBox(config=self.config, device=self.device, task="BoxDisassemble").run()

    def auto_equip(self):
        from module.auto_equip.auto_equip import AutoEquip
        AutoEquip(config=self.config, device=self.device, task="AutoEquip").run()

    def benchmark(self):
        from module.daemon.benchmark import run_benchmark
        run_benchmark(config=self.config)

    def ocr_benchmark(self):
        from module.daemon.ocr_benchmark import run_ocr_benchmark
        run_ocr_benchmark(config=self.config)

    def screenshot_interval_benchmark(self):
        from module.daemon.screenshot_interval_benchmark import (
            run_screenshot_interval_benchmark,
        )
        run_screenshot_interval_benchmark(
            config=self.config,
            device=self.device,
        )

    def game_manager(self):
        from module.daemon.game_manager import GameManager
        GameManager(config=self.config, device=self.device, task="GameManager").run()

    def emulator_manager(self):
        import subprocess
        # 优先使用 EmulatorInfo 中的 SSH 配置
        if getattr(self.config, 'EmulatorInfo_EnableRemoteSSH', False):
            host = getattr(self.config, 'EmulatorInfo_RemoteSSHHost', '')
            port = getattr(self.config, 'EmulatorInfo_RemoteSSHPort', 22)
            user = getattr(self.config, 'EmulatorInfo_RemoteSSHUser', '')
            command = getattr(self.config, 'EmulatorInfo_RemoteStartCommand', '')
            key = getattr(self.config, 'EmulatorInfo_RemoteSSHPublicKey', '')
        else:
            # 回退到 EmulatorManager 配置
            enable = deep_get(self.config.data, 'EmulatorManager.EmulatorManager.EnableRemoteSSH', False)
            if not enable:
                logger.warning('[Alas-SSH] Удалённый SSH не включён в настройках управления эмулятором')
                return

            host = deep_get(self.config.data, 'EmulatorManager.EmulatorManager.RemoteSSHHost', '')
            port = deep_get(self.config.data, 'EmulatorManager.EmulatorManager.RemoteSSHPort', 22)
            user = deep_get(self.config.data, 'EmulatorManager.EmulatorManager.RemoteSSHUser', '')
            command = deep_get(self.config.data, 'EmulatorManager.EmulatorManager.RemoteStartCommand', '')
            if not command:
                command = deep_get(self.config.data, 'EmulatorManager.EmulatorManager.RemoteCommand', '')
            key = deep_get(self.config.data, 'EmulatorManager.EmulatorManager.RemoteSSHPublicKey', '')

        if not host or not command:
            logger.warning(f'[Alas-SSH] Хост удалённого SSH ({host}) или команда запуска ({command}) не заданы; команда пропущена')
            return

        logger.hr('Команда удалённого SSH', level=1)
        target = f'{user}@{host}' if user else host
        clear_ssh_host_key(host, port)
        # -n: 禁用标准输入  -T: 禁用伪终端分配  BatchMode: 避免密码提示导致挂起
        cmd = [
            'ssh', '-n', '-T', '-p', str(port),
            '-o', 'StrictHostKeyChecking=no',
            '-o', f'UserKnownHostsFile={os.devnull}',
            '-o', f'GlobalKnownHostsFile={os.devnull}',
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        ]

        key_file = None
        if key and len(key) > 50:
            import tempfile
            try:
                fd, key_file = tempfile.mkstemp()
                with os.fdopen(fd, 'w') as f:
                    f.write(key.strip() + '\n')

                if os.name == 'nt':
                    import subprocess
                    user_env = os.environ.get('USERNAME')
                    subprocess.run(['icacls', key_file, '/reset'], capture_output=True)
                    subprocess.run(['icacls', key_file, '/inheritance:r'], capture_output=True)
                    subprocess.run(['icacls', key_file, '/grant:r', f'{user_env}:F'], capture_output=True)
                else:
                    os.chmod(key_file, 0o600)

                cmd += ['-i', key_file]
                logger.info(f'[Alas-SSH] Для аутентификации используется предоставленный закрытый ключ')
            except Exception as e:
                logger.error(f'[Alas-SSH] Не удалось создать или защитить временный файл ключа: {e}')

        cmd += [target, command]
        logger.info(f'[Alas-SSH] Выполнение удалённой команды: {" ".join(cmd)}')

        try:
            process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True, 
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            # 缓存 stderr 输出，仅在失败时打印
            stderr_content = []
            import threading

            def collect_stderr():
                for line in process.stderr:
                    stderr_content.append(line.strip())

            def collect_stdout():
                for line in process.stdout:
                    logger.info(f'[Alas-SSH] Удалённый вывод: {line.strip()}')

            stderr_thread = threading.Thread(target=collect_stderr)
            stdout_thread = threading.Thread(target=collect_stdout)
            stderr_thread.start()
            stdout_thread.start()

            try:
                # 主线程等待进程退出
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                logger.error('[Alas-SSH] Команда удалённого SSH превысила тайм-аут 30 секунд')
                return
            finally:
                stderr_thread.join(timeout=5)
                stdout_thread.join(timeout=5)

            if process.returncode == 0:
                logger.info('[Alas-SSH] Удалённая команда выполнена успешно')
            else:
                logger.error(f'[Alas-SSH] Удалённая команда завершилась с кодом {process.returncode}')
                for line in stderr_content:
                    logger.error(f'[Alas-SSH] Ошибка удалённой команды: {line}')
        except Exception as e:
            logger.error(f'[Alas-SSH] Не удалось выполнить команду удалённого SSH: {e}')
        finally:
            if key_file and os.path.exists(key_file):
                try:
                    os.remove(key_file)
                except:
                    pass

    def wait_until(self, future):
        """
        阻塞等待直到指定时间到达。

        等待期间每 5 秒检查一次配置文件变更和停止事件。

        Args:
            future (datetime): 目标等待时间。

        Returns:
            bool: 正常等到返回 True，检测到配置变更返回 False。
        """
        future = future + timedelta(seconds=1)
        self.config.start_watching()
        while 1:
            if current_time() > future:
                return True
            if self.fleet_manual_scan.has_pending(self.config_name):
                self._manual_scan_wakeup = True
                logger.info(
                    '[Alas] Ожидание прервано ожидающей командой ручного '
                    'сканирования флотов'
                )
                return True
            if self.stop_event is not None:
                if self.stop_event.is_set():
                    logger.info('[Alas] Получен запрос на остановку')
                    logger.info(
                        f'[{self.config_name}] Работа завершена. Причина: запрос на остановку'
                    )
                    exit(0)

            time.sleep(5)

            if self.config.should_reload():
                return False

    def get_next_task(self):
        """
        获取下一个待执行的任务。

        如果任务尚未到执行时间，根据 Optimization_WhenTaskQueueEmpty 设置
        选择等待策略（关闭游戏 / 前往主页 / 停留原地），然后阻塞等待。

        Returns:
            str: 下一个任务的方法名（如 'Restart'、'Commission'）。
        """
        while 1:
            task = self.config.get_next()
            self.config.task = task
            self.config.bind(task)

            from module.base.resource import release_resources
            if self.config.task.command != 'Alas':
                release_resources(next_task=task.command)

            if task.next_run > current_time():
                logger.info(f'[Alas] Ожидание до {task.next_run} перед запуском задачи `{task.command}`')
                self.is_first_task = False
                method = self.config.Optimization_WhenTaskQueueEmpty
                wait_duration = task.next_run - current_time()
                if (
                    self.config.Optimization_CloseEmulatorDuringLongWait
                    and wait_duration > timedelta(hours=3)
                    and 'device' in self.__dict__ and self.device.emulator_instance is not None  # 远程设备（无线 ADB / SSH）没有本地模拟器实例可管理，跳过关闭流程，走常规等待逻辑
                ):
                    logger.info(
                        f'Следующая задача `{task.command}` запустится через {wait_duration}; '
                        'на время ожидания эмулятор будет остановлен'
                    )
                    release_resources()
                    self.device.release_during_wait()
                    try:
                        if self.device.emulator_stop():
                            logger.info('[Alas] Эмулятор остановлен на время ожидания')
                        else:
                            logger.warning('[Alas] Не удалось остановить эмулятор на время ожидания; ожидание продолжается')
                    except Exception as e:
                        logger.warning(f'[Alas] Не удалось остановить эмулятор на время ожидания; ожидание продолжается: {e}')
                    if 'device' in self.__dict__:
                        del_cached_property(self, 'device')
                    if not self.wait_until(task.next_run):
                        del_cached_property(self, 'config')
                        continue
                    self._start_emulator_after_long_wait()
                    if task.command != 'Restart':
                        self.config.task_call('Restart')
                        del_cached_property(self, 'config')
                        continue
                elif method == 'close_game':
                    logger.info('[Alas] Игра закрывается на время ожидания')
                    self.device.app_stop()
                    release_resources()
                    self.device.release_during_wait()
                    if not self.wait_until(task.next_run):
                        del_cached_property(self, 'config')
                        continue
                    if task.command != 'Restart':
                        self.config.task_call('Restart')
                        del_cached_property(self, 'config')
                        continue
                elif method == 'goto_main':
                    logger.info('[Alas] Переход на главную страницу на время ожидания')
                    self.run('goto_main')
                    release_resources()
                    self.device.release_during_wait()
                    if not self.wait_until(task.next_run):
                        del_cached_property(self, 'config')
                        continue
                elif method == 'stay_there':
                    release_resources()
                    self.device.release_during_wait()
                    if not self.wait_until(task.next_run):
                        del_cached_property(self, 'config')
                        continue
                else:
                    logger.warning(f'[Alas] Некорректное значение Optimization_WhenTaskQueueEmpty: {method}; используется stay_there')
                    release_resources()
                    self.device.release_during_wait()
                    if not self.wait_until(task.next_run):
                        del_cached_property(self, 'config')
                        continue
            break

        AzurLaneConfig.is_hoarding_task = False
        return task.command

    def _prepare_task_boundary(self, task):
        """Обработать durable manual scan только между обычными задачами."""

        _ = self.device
        self.device.config = self.config
        woke_for_manual = bool(getattr(self, '_manual_scan_wakeup', False))
        self._manual_scan_wakeup = False
        if self.is_first_task and task == 'Restart':
            logger.info('[Alas] При запуске планировщика задача `Restart` пропущена')
            self.delay_next_restart()
            del_cached_property(self, 'config')
            return False
        if task == 'Restart':
            return True
        manual_execution = self._run_fleet_manual_scan_if_pending()
        if manual_execution is not None:
            if self.stop_event is not None and self.stop_event.is_set():
                logger.info('[Alas] Запрос на остановку получен во время ручного сканирования')
                return False
            # Задача после досрочного пробуждения не должна стартовать раньше next_run.
            return not woke_for_manual
        if woke_for_manual:
            # Другой worker мог забрать команду, поэтому возвращаемся к штатному ожиданию.
            return False
        return True

    def loop(self):
        logger.set_file_logger(self.config_name)
        logger.info(f'[Alas] Запуск цикла планировщика: {self.config_name}')
        record_dev_runtime_error = getattr(
            self,
            "_record_dev_runtime_error",
            lambda *_args, **_kwargs: None,
        )
        record_dev_runtime_task_started = getattr(
            self,
            "_record_dev_runtime_task_started",
            lambda *_args, **_kwargs: None,
        )
        record_dev_runtime_task_finished = getattr(
            self,
            "_record_dev_runtime_task_finished",
            lambda *_args, **_kwargs: None,
        )

        from module.config.utils import is_oobe_needed

        if is_oobe_needed():
            logger.error_context(
                title='Файл конфигурации не найден',
                reason='Первичная настройка проекта не завершена либо файл конфигурации отсутствует в каталоге config.',
                impact='Планировщик не может быть запущен.',
                action='Запустите `uv run python gui.py`, откройте WebUI, завершите первоначальную настройку и снова запустите планировщик.',
                level=50,
            )
            exit(1)

        # 全局异常连续失败计数与阈值
        consecutive_global_failures = 0
        MAX_GLOBAL_FAILURES = 3
        RESTART_DELAY = 20
        LONG_WAIT = 300

        while 1:
            try:
                # 检查来自 GUI 的通用停止请求
                if self.stop_event is not None:
                    if self.stop_event.is_set():
                        logger.info('[Alas] Получен запрос на остановку')
                        logger.info(
                            f"[Alas] [{self.config_name}] Работа завершена. Причина: запрос на остановку"
                        )
                        break
                # 检查游戏服务器维护
                self.checker.wait_until_available()
                if self.checker.is_recovered():
                    # 服务器恢复后强制刷新配置，修复阻塞期间配置未更新的问题
                    del_cached_property(self, 'config')
                    logger.info('[Alas] Сервер или сеть восстановлены; выполняется перезапуск игрового клиента')
                    self.config.task_call('Restart')
                # 检查计划的模拟器重启（在任务之间，不会中断正在运行的任务）
                if self.config.EmulatorManagement_ScheduledEmulatorRestart:
                    elapsed_hours = (time.monotonic() - self.last_emulator_restart_time) / 3600
                    interval = self.config.EmulatorManagement_RestartIntervalHours
                    if elapsed_hours >= interval:
                        logger.hr('[Alas] Плановый перезапуск эмулятора', level=1)
                        logger.info(f'[Alas] Эмулятор работает {elapsed_hours:.1f} ч; '
                                    f'интервал планового перезапуска — {interval} ч')
                        if self._try_restart_emulator(reason='scheduled'):
                            self.last_emulator_restart_time = time.monotonic()
                            self.config.task_call('Restart')
                            del_cached_property(self, 'config')
                            continue
                        else:
                            # Сдвигаем окно, чтобы неудачная плановая попытка не повторялась перед каждой задачей.
                            self.last_emulator_restart_time = time.monotonic()
                            if self._emulator_recovery_transport_lost:
                                logger.error_context(
                                    title='Плановое восстановление потеряло рабочий transport',
                                    reason='Эмулятор был остановлен или его состояние стало неопределённым, а новый Device не создан.',
                                    impact='Продолжение задач могло бы запустить скрытый autostart или использовать недействительный transport.',
                                    action='AzurPilot останавливает scheduler. Проверьте MuMu и ADB перед следующим запуском.',
                                    level=50,
                                )
                                break
                            logger.warning('[Alas] Плановый перезапуск эмулятора не выполнен; обычная работа продолжается')

                # 获取任务
                task = self.get_next_task()
                # Autoscan проверяется на безопасной границе после возможного ожидания.
                if not self._prepare_task_boundary(task):
                    continue

                # 运行
                logger.info(f'[Alas] Планировщик: запуск задачи `{task}`')
                record_dev_runtime_task_started(task)
                self.device.stuck_record_clear()
                self.device.click_record_clear()
                logger.hr(task, level=0)
                success = self.run(inflection.underscore(task))
                record_dev_runtime_task_finished(task)
                logger.info(f'[Alas] Планировщик: завершение задачи `{task}`')
                self.is_first_task = False

                # 每任务推送通知（须在 config_generated 刷新前读取）
                if success is not None:
                    try:
                        if getattr(self.config, 'Scheduler_PushNotification', False):
                            if success == True:
                                status = 'Успешно'
                            elif success == 'recoverable':
                                status = 'Успешно (обнаружена восстановимая ошибка)'
                            else:
                                status = 'Ошибка'
                            task_display = _get_task_display_name(task)
                            handle_notify(
                                self.config.Error_OnePushConfig,
                                title=f"[AzurPilot] <{self.config_name}> {task_display}: {status}",
                                content=f"<{self.config_name}> Задача {task_display}: {status}",
                            )
                    except Exception:
                        logger.warning('[Alas] Не удалось отправить уведомление о задаче; уведомление пропущено')

                # 检查失败
                # 单个任务连续失败三次终止程序
                # 注意：可恢复错误 (success == 'recoverable') 不计入失败次数
                failed = deep_get(self.failure_record, keys=task, default=0)
                if success == True:
                    failed = 0  # 成功，重置计数
                elif success == 'recoverable':
                    # 可恢复错误（如 GameStuckError），不增加失败计数
                    # 但也不重置，保持之前的计数
                    logger.info(f'[Alas] В задаче `{task}` произошла восстановимая ошибка; предел ошибок не увеличен')
                else:
                    failed = failed + 1  # 不可恢复错误，增加计数
                deep_set(self.failure_record, keys=task, value=failed)

                if self._emulator_recovery_transport_lost:
                    logger.error_context(
                        title='Восстановление эмулятора не вернуло рабочий transport',
                        reason='Stage 2 завершилась после destructive transport-stage без нового Device.',
                        impact='Повторное использование старого Device или скрытый autostart запрещены; scheduler остановлен.',
                        action='Проверьте состояние MuMu и ADB и запустите AzurPilot повторно после восстановления среды.',
                        level=50,
                    )
                    break

                strict_restart = self.config.Error_StrictRestart and failed >= 1 and self.config.cross_get(
                    keys=f'{task}.Scheduler.Sensitive', default=False
                )
                if failed >= 3 or strict_restart:
                    reason = 'Конфигурация или способ запуска задачи некорректны либо в самой задаче произошла ошибка.'
                    action = 'Проверьте справку по параметрам задачи и сохранённые данные ошибки; исправьте конфигурацию перед повторным запуском.'
                    if strict_restart:
                        reason += 'Задача чувствительна к перезапуску, поэтому автоматический перезапуск после ошибки запрещён.'
                        action += 'Для автоматического восстановления отключите StrictRestart этой задачи; иначе перейдите к ручному управлению игрой.'
                    logger.error_context(
                        title=f'Задача многократно завершилась с ошибкой; требуется вмешательство пользователя ({task})',
                        reason=f'Задача завершилась с ошибкой {failed} раз подряд. {reason}',
                        impact='Планировщик остановится, чтобы избежать повторных действий или повреждения данных.',
                        action=action,
                        level=50,
                    )
                    handle_notify(
                        self.config.Error_OnePushConfig,
                        title=f"AzurPilot <{self.config_name}>: аварийное завершение",
                        content=f"<{self.config_name}> RequestHumanTakeover\nЗадача `{task}` завершилась с ошибкой не менее {failed} раз.",
                    )
                    notify_webui(
                        self.config_name,
                        title=f"Ошибка в {self.config_name}",
                        content=f"Задача {task} превысила предел последовательных ошибок.",
                    )
                    logger.warning("[Alas] Превышен предел последовательных ошибок задачи; подробности сохранены только в локальном журнале.")
                    exit(1)

                if success == True:
                    del_cached_property(self, 'config')
                    consecutive_global_failures = 0 # 任务成功时重置全局失败计数器
                    self.consecutive_game_stuck = 0
                    self.consecutive_adb_offline = 0
                    continue
                elif success == 'recoverable' or self.config.Error_HandleError:
                    # 可恢复错误或启用了错误处理，刷新配置后继续循环
                    del_cached_property(self, 'config')
                    self.checker.check_now()
                    continue
                else:
                    break

            # 捕获全局异常并执行重启
            except Exception as e:
                scheduler_task = getattr(
                    getattr(self.__dict__.get("config"), "task", None),
                    "command",
                    None,
                )
                if not isinstance(scheduler_task, str):
                    scheduler_task = None
                record_dev_runtime_error(
                    e,
                    phase="scheduler",
                    task=scheduler_task,
                )
                consecutive_global_failures += 1
                self.is_first_task = False
                import traceback
                logger.exception_context(
                    title='Необработанная ошибка в цикле планировщика',
                    exc=e,
                    impact='Текущая итерация прервана; планировщик попробует назначить Restart и продолжить работу.',
                    action='Изучите стек ниже. При повторении проверьте подключение устройства, конфигурацию и недавно обновлённые ресурсы.',
                )

                # 即使没有达到重启或失败上限，也第一时间自动请求分析崩溃原因
                try:
                    if hasattr(self, 'config') and getattr(self.config, 'Error_LlmAnalysis', False):
                        from module.llm import analyze_exception
                        analyze_exception(self.config, e)
                except Exception as ex:
                    logger.error(f'[Alas] Не удалось выполнить LLM-анализ ошибки: {ex}')

                logger.warning(
                    f">>> Последовательная глобальная ошибка {consecutive_global_failures} из {MAX_GLOBAL_FAILURES}."
                )

                # 检查是否达到重试上限
                if consecutive_global_failures >= MAX_GLOBAL_FAILURES:
                    logger.error_context(
                        title='Достигнут предел последовательных ошибок планировщика',
                        reason=f'Глобальная ошибка произошла {MAX_GLOBAL_FAILURES} раз подряд.',
                        impact='Автоматическое восстановление остановлено; AzurPilot завершит работу.',
                        action='Изучите log.txt и снимки в сохранённых данных, устраните причину и перезапустите приложение; приложите эти данные при регистрации проблемы.',
                        exc=e,
                        level=50,
                    )
                    self.save_error_log()
                    logger.warning("[Alas] Обнаружена невосстановимая ошибка; подробности сохранены только в локальном журнале.")
                    exit(1)

                # 尝试重启
                logger.warning("[Alas] Попытка восстановления через принудительное назначение задачи `Restart`...")
                try:
                    # 注入 Restart 任务
                    self.config.task_call('Restart')
                    # 重新加载配置
                    del_cached_property(self, 'config')
                    logger.info("[Alas] Задача `Restart` назначена для следующего цикла.")
                except Exception as restart_e:
                    logger.exception_context(
                        title='Не удалось назначить задачу Restart для восстановления',
                        exc=restart_e,
                        impact='Автоматическое восстановление невозможно; следующая итерация также может завершиться ошибкой.',
                        action='Проверьте доступность конфигурации, включена ли задача Restart и остаётся ли устройство подключённым.',
                    )

                # 等待一段时间后开始下一次循环
                wait_seconds = RESTART_DELAY if consecutive_global_failures < 4 else LONG_WAIT
                logger.info(
                    f"Планировщик повторит цикл с начала через {wait_seconds} с."
                )
                time.sleep(wait_seconds)

if __name__ == '__main__':
    alas = AzurLaneAutoScript()
    alas.loop()
