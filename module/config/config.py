"""Основной модуль управления конфигурацией.

Определяет класс AzurLaneConfig, загружающий пользовательские настройки из файла JSON и объединяющий их с шаблоном.
Объединяет ConfigUpdater, ManualConfig, GeneratedConfig, ConfigWatcher,
поддерживает горячую перезагрузку конфигурации, миграцию версий и привязку настроек уровня задач.
"""

import copy
import operator
import os
import platform
import sys
import threading
from datetime import datetime, timedelta

import pywebio

from module.base.filter import Filter
from module.config.config_generated import GeneratedConfig
from module.config.config_manual import ManualConfig, OutputConfig
from module.config.config_updater import ConfigUpdater, ensure_time, get_server_next_update, nearest_future
from module.config.deep import deep_get, deep_set
from module.config.opsi_data_logger import data_logger_is_active_from_data
from module.config.recovery_default_on_migration import apply_recovery_default_on_migration
from module.config.time_source import now as current_time
from module.config.utils import DEFAULT_TIME, dict_to_kv, filepath_config, get_os_reset_remain, path_to_arg, is_good_gpu
from module.config.watcher import ConfigWatcher
from module.exception import RequestHumanTakeover, ScriptError
from module.logger import logger
from module.map.map_grids import SelectedGrids


class TaskEnd(Exception):
    """Исключение досрочного завершения задачи.

    Вызывается при обнаружении необходимости отложить задачу (например, недостаточно настроения),
    перехватывается циклом планировщика для назначения отложенного повтора.
    """
    pass


class Function:
    """Объект-описание функции планировщика задач.

    Описывает базовые свойства планируемой задачи: активность, имя команды и время следующего выполнения.
    Используется планировщиком для сортировки по приоритету и выбора исполнения.

    Attributes:
        enable (bool): Включена ли задача.
        command (str): Имя команды задачи, например 'Research', 'Commission'.
        next_run (datetime): Время следующего запланированного запуска.
    """

    def __init__(self, data):
        self.enable = deep_get(data, keys="Scheduler.Enable", default=False)
        self.command = deep_get(data, keys="Scheduler.Command", default="Unknown")
        self.next_run = deep_get(data, keys="Scheduler.NextRun", default=DEFAULT_TIME)

    def __str__(self):
        enable = "Enable" if self.enable else "Disable"
        return f"{self.command} ({enable}, {str(self.next_run)})"

    __repr__ = __str__

    def __eq__(self, other):
        if not isinstance(other, Function):
            return False

        if self.command == other.command and self.next_run == other.next_run:
            return True
        else:
            return False


def name_to_function(name):
    """
    Создать объект Function по имени задачи.

    Args:
        name (str): Имя задачи.

    Returns:
        Function: Соответствующий экземпляр Function.
    """
    function = Function({})
    function.command = name
    function.enable = True
    return function


class AzurLaneConfig(ConfigUpdater, ManualConfig, GeneratedConfig, ConfigWatcher):
    """Менеджер конфигурации автоматизации Azur Lane.

    Центральный класс конфигурации проекта, объединяющий через множественное наследование:
    - ConfigUpdater: обновление версий конфигурации и слияние значений по умолчанию
    - ManualConfig: доступ к свойствам ручной конфигурации
    - GeneratedConfig: автоматически сгенерированные свойства конфигурации (из template.json)
    - ConfigWatcher: отслеживание изменений файлов конфигурации

    Процесс загрузки конфигурации:
        1. Чтение пользовательской конфигурации из `config/{config_name}.json`
        2. Слияние со значениями по умолчанию из args.json (ConfigUpdater.config_update)
        3. Выполнение перенаправлений миграции версий (ConfigUpdater.config_redirect)
        4. Применение переопределений для облачного телефона (_override)
        5. Привязка к текущей задаче (bind(task))

    Доступ к свойствам:
        Путь конфигурации имеет формат `Task.Group.Argument` и отображается через `__getattr__`
        в `self.Group_Argument` (с разделителем-подчёркиванием), например `self.Research_PresetFilter`.

    Изменение свойств:
        Через `__setattr__` перехватываются привязанные свойства с автоматической записью изменений в файл конфигурации.

    Attributes:
        stop_event (threading.Event | None): Событие остановки для межпоточного уведомления.
        bound (dict): Отображение имён привязанных свойств текущей задачи на пути конфигурации.
        is_hoarding_task (bool): Является ли задача накопительной (влияет на поведение при простое).
    """
    stop_event: threading.Event = None
    bound = {}

    # Свойства класса
    is_hoarding_task = True

    def __setattr__(self, key, value):
        if key in self.bound:
            path = self.bound[key]
            self.modified[path] = value
            if self.auto_update:
                self.update()
        else:
            super().__setattr__(key, value)

    def __init__(self, config_name, task=None):
        logger.attr("Сервер", self.SERVER)
        # Чтение ./config/<config_name>.json
        self.config_name = config_name
        # Исходные данные JSON из файлов YAML
        self.data = {}
        # Измененные параметры. Ключ: путь к параметру в YAML-файле. Значение: измененное значение.
        # Все изменения переменных записываются сюда и сохраняются в методе `save()`.
        self.modified = {}
        # Ключ: имя параметра в GeneratedConfig. Значение: путь в `data`.
        self.bound = {}
        # Выполнять ли немедленную запись после каждого изменения переменной
        self.auto_update = True
        # Принудительно переопределяемые переменные
        # Ключ: имя параметра в GeneratedConfig. Значение: измененное значение.
        self.overridden = {}
        # Очередь планировщика, обновляется в `get_next_task()`, содержит список объектов Function
        # pending_task: время запуска наступило, но задача еще не выполнена из-за планирования
        # waiting_task: время запуска не наступило, требуется ожидание
        self.pending_task = []
        self.waiting_task = []
        # Задачи для выполнения и привязки
        # task обозначает имя функции для запуска в классе AzurLaneAutoScript
        self.task: Function
        # Шаблонная конфигурация для инструментов разработки
        self.is_template_config = config_name.startswith("template")

        if self.is_template_config:
            # Для инструментов разработки
            logger.info("[Конфигурация] Используется шаблон в режиме только для чтения")
            self.auto_update = False
            self.task = name_to_function("template")
        elif not os.path.exists(filepath_config(config_name)):
            from module.config.utils import is_oobe_needed
            if is_oobe_needed():
                logger.warning(
                    "[Конфигурация] Файл конфигурации не найден. "
                    "Запустите 'python gui.py', чтобы завершить первоначальную настройку."
                )
        self._disable_task_switch = False
        self.init_task(task)

    def init_task(self, task=None):
        if self.is_template_config:
            return

        self.load()
        if task is None:
            # По умолчанию привязывается Alas, включая настройки эмулятора
            task = name_to_function("Alas")
        else:
            # Привязка конкретной задачи для отладки
            task = name_to_function(task)
        self.bind(task)
        self.task = task
        self.save()

    def load(self):
        self.data = self.read_file(self.config_name)
        if (
            not self.is_template_config
            and os.path.exists(filepath_config(self.config_name))
            and apply_recovery_default_on_migration(self.data)
        ):
            logger.info(
                "[Конфигурация] Stage 3: unattended emulator recovery включён "
                "для существующего профиля; migration marker сохранён"
            )
            self.write_file(self.config_name, data=self.data)
        self.config_override()

        for path, value in self.modified.items():
            deep_set(self.data, keys=path, value=value)

    def bind(self, func, func_list=None):
        """Привязать задачу и её параметры конфигурации.

        Args:
            func (str, Function): Имя запускаемой задачи или объект Function.
            func_list (list[str]): Список привязываемых задач.
        """
        if isinstance(func, Function):
            func = func.command
        # func_list: ["General", "Alas", <task_general>, <task>, *func_list]
        if func_list is None:
            func_list = []
        if func not in func_list:
            func_list.insert(0, func)
        if func.startswith("Opsi"):
            if "OpsiGeneral" not in func_list:
                func_list.insert(0, "OpsiGeneral")
        if (
            func.startswith("Event")
            or func.startswith("Raid")
            or func.startswith("Coalition")
            or func in ["MaritimeEscort", "GemsFarming", "ThreeOilLowCost"]
        ):
            if "EventGeneral" not in func_list:
                func_list.insert(0, "EventGeneral")
            if "TaskBalancer" not in func_list:
                func_list.insert(0, "TaskBalancer")
        if "Alas" not in func_list:
            func_list.insert(0, "Alas")
        if "General" not in func_list:
            func_list.insert(0, "General")
        logger.info(f"[Конфигурация] Привязка задач: {func_list}")

        # Привязка аргументов
        visited = set()
        self.bound.clear()
        for func in func_list:
            func_data = self.data.get(func, {})
            for group, group_data in func_data.items():
                for arg, value in group_data.items():
                    path = f"{group}.{arg}"
                    if path in visited:
                        continue
                    arg = path_to_arg(path)
                    super().__setattr__(arg, value)
                    self.bound[arg] = f"{func}.{path}"
                    visited.add(path)

        # Переопределение аргументов
        for arg, value in self.overridden.items():
            super().__setattr__(arg, value)


    @property
    def ocr_backend(self) -> str:
        val = self.Optimization_OcrBackend
        if val == 'auto':
            return 'onnxruntime'
        return val

    def ocr_model_version(self, name: str) -> str:
        if name != "azur_lane":
            raise ValueError(f"Неподдерживаемая OCR-модель: {name}")
        if self.ocr_backend == "ncnn":
            return "ncnn"
        return self.Optimization_OcrModelVersionEnglish

    @property
    def ocr_device(self) -> str:
        val = self.Optimization_OcrDevice
        if val == 'auto':
            if self.ocr_backend == 'onnxruntime':
                if sys.platform == 'darwin' and platform.machine() == 'arm64':
                    return 'ane'
                if sys.platform == 'win32':
                    # Windows ML самостоятельно фильтрует NPU, дискретный GPU и CPU; не следует полагаться только на видеопамять.
                    return 'auto'
                return 'gpu' if is_good_gpu() else 'cpu'
            else:
                # Бэкенд ncnn: проверка доступности Vulkan GPU
                from module.ocr.ncnn_ocr import has_ncnn_vulkan_gpu
                return 'gpu' if has_ncnn_vulkan_gpu() else 'cpu'

        if self.ocr_backend == 'ncnn' and val in {
            'qnn_npu',
            'openvino_npu',
            'openvino_gpu',
            'openvino_cpu',
        }:
            return 'cpu'
        return val

    @property
    def hoarding(self):
        minutes = int(
            deep_get(
                self.data, keys="Alas.Optimization.TaskHoardingDuration", default=0
            )
        )
        return timedelta(minutes=max(minutes, 0))

    @property
    def close_game(self):
        return deep_get(
            self.data, keys="Alas.Optimization.CloseGameDuringWait", default=False
        )

    @property
    def is_actual_task(self):
        return self.task.command.lower() not in ['alas', 'template']

    def get_next_task(self):
        """Вычислить очередь задач, заполнив pending_task и waiting_task."""
        pending = []
        waiting = []
        error = []
        now = current_time()
        if AzurLaneConfig.is_hoarding_task:
            now -= self.hoarding
        from module.dev_runtime.task_sandbox import task_policy_context

        policy_context = task_policy_context(self.config_name)
        policy = policy_context.policy
        sandbox_enforced = policy_context.enforced
        if sandbox_enforced and (policy is None or policy.state != "active"):
            # Fail-closed: без подтверждённой active policy задачи не планируются.
            self.pending_task = []
            self.waiting_task = []
            return
        allowed_tasks = set(policy.allowed_tasks) if sandbox_enforced and policy is not None else None
        for section, raw_task in self.data.items():
            if sandbox_enforced and not isinstance(section, str):
                continue
            func = Function(raw_task)
            if sandbox_enforced and (
                func.command != section
                or func.command not in allowed_tasks
                or func.enable is not True
            ):
                continue
            if sandbox_enforced and not isinstance(func.next_run, datetime):
                continue
            if not sandbox_enforced and not func.enable:
                continue
            if not isinstance(func.next_run, datetime):
                error.append(func)
            elif func.next_run < now:
                pending.append(func)
            else:
                waiting.append(func)

        f = Filter(regex=r"(.*)", attr=["command"])
        f.load(self.SCHEDULER_PRIORITY)
        if pending:
            pending = f.apply(pending)
        if waiting:
            waiting = f.apply(waiting)
            waiting = sorted(waiting, key=operator.attrgetter("next_run"))
        if error:
            pending = error + pending

        self.pending_task = pending
        self.waiting_task = waiting

    def get_next(self):
        """Получить следующую задачу для выполнения.

        Returns:
            Function: Задача для выполнения.
        """
        self.get_next_task()

        if self.pending_task:
            AzurLaneConfig.is_hoarding_task = False
            logger.info(f"[Конфигурация] Задачи в очереди: {[f.command for f in self.pending_task]}")
            task = self.pending_task[0]
            logger.attr("Задача", task)
            return task
        else:
            AzurLaneConfig.is_hoarding_task = True

        if self.waiting_task:
            logger.info("[Конфигурация] Нет задач в очереди")
            task = copy.deepcopy(self.waiting_task[0])
            task.next_run = (task.next_run + self.hoarding).replace(microsecond=0)
            logger.attr("Задача", task)
            return task
        else:
            logger.critical("[Конфигурация] Нет задач в очереди или ожидающих запуска")
            logger.critical("[Конфигурация] Включите хотя бы одну задачу")
            raise RequestHumanTakeover

    def save(self, mod_name='alas'):
        if not self.modified:
            return False

        for path, value in self.modified.items():
            deep_set(self.data, keys=path, value=value)

        logger.info(
            f"[Конфигурация] Сохранение {filepath_config(self.config_name, mod_name)}, {dict_to_kv(self.modified)}"
        )
        # Не используйте self.modified = {}, это создаст новый объект.
        self.modified.clear()
        self.write_file(self.config_name, data=self.data)

    def update(self):
        self.load()
        self.config_override()
        self.bind(getattr(self, '_bind_task_override', self.task))
        self.save()

    def override(self, **kwargs):
        now = current_time().replace(microsecond=0)
        limited = set()

        def limit_next_run(tasks, limit):
            for task in tasks:
                if task in limited:
                    continue
                limited.add(task)
                next_run = deep_get(
                    self.data, keys=f"{task}.Scheduler.NextRun", default=None
                )
                if isinstance(next_run, datetime) and next_run > limit:
                    deep_set(self.data, keys=f"{task}.Scheduler.NextRun", value=now)

        limit_next_run(["Commission", "Reward"], limit=now + timedelta(hours=12, seconds=-1))
        limit_next_run(["Research"], limit=now + timedelta(hours=24, seconds=-1))
        limit_next_run(["OpsiExplore", "OpsiCrossMonth", "OpsiVoucher", "OpsiMonthBoss", "OpsiShop"],
                       limit=now + timedelta(days=31, seconds=-1))
        limit_next_run(["OpsiArchive"], limit=now + timedelta(days=7, seconds=-1))
        # Задача защиты от перелива откладывается до восстановления 200 AP, максимум свыше 24 часов.
        limit_next_run(["OpsiPreventActionPointOverflow"], limit=now + timedelta(hours=48, seconds=-1))
        # IslandPearlSell планируется еженедельно, корректный NextRun может превышать 24 часа.
        limit_next_run(["IslandPearlSell"], limit=now + timedelta(days=8, seconds=-1))
        # Универсальный резерв сохраняет небольшой допуск для 24-часового расписания, чтобы не сбрасывать отложенные на день задачи.
        limit_next_run(
            [task for task in self.args.keys() if task != "OpsiPreventActionPointOverflow"],
            limit=now + timedelta(hours=25, seconds=-1),
        )

        """
        Принудительно переопределить произвольные параметры конфигурации.

        Переопределённые переменные сохраняют своё состояние даже при повторной загрузке конфигурации из YAML-файла.
        Обратите внимание: данный метод необратим.
        """
        for arg, value in kwargs.items():
            self.overridden[arg] = value
            super().__setattr__(arg, value)

    config_override = override

    def set_record(self, **kwargs):
        """Установить значение и автоматически записать текущее время.

        Args:
            **kwargs: Например, `Emotion1_Value=150` одновременно установит
                `Emotion1_Value=150` и `Emotion1_Record=now()`.
        """
        with self.multi_set():
            for arg, value in kwargs.items():
                record = arg.replace("Value", "Record")
                self.__setattr__(arg, value)
                self.__setattr__(record, current_time().replace(microsecond=0))

    def multi_set(self):
        """Пакетно установить несколько параметров с однократным сохранением.

        Examples:
            with self.config.multi_set():
                self.config.foo1 = 1
                self.config.foo2 = 2
        """
        return MultiSetWrapper(main=self)

    def cross_get(self, keys, default=None):
        """Получить параметр конфигурации из другой задачи.

        Args:
            keys (str, list[str]): Путь конфигурации, например `{task}.Scheduler.Enable`.
            default: Значение по умолчанию.

        Returns:
            Any: Значение параметра конфигурации.
        """
        return deep_get(self.data, keys=keys, default=default)

    def cross_set(self, keys, value):
        """Установить параметр конфигурации для другой задачи.

        Args:
            keys (str, list[str]): Путь конфигурации, например `{task}.Scheduler.Enable`.
            value (Any): Устанавливаемое значение.
        """
        self.modified[keys] = value
        if self.auto_update:
            self.update()

    def task_delay(self, success=None, server_update=None, target=None, minute=None, task=None):
        """Установить Scheduler.NextRun, отложив время следующего запуска задачи.

        Требуется задать хотя бы один параметр. Если указано несколько параметров, выбирается ближайшее время.

        Args:
            success (bool):
                True — отложить на Scheduler.SuccessInterval,
                False — отложить на Scheduler.FailureInterval.
            server_update (bool, list, str):
                True — отложить до ближайшего времени Scheduler.ServerUpdate.
                Тип list или str — отложить до указанного времени обновления сервера.
            target (datetime.datetime, str, list):
                Отложить до указанного момента времени.
            minute (int, float, tuple):
                Отложить на указанное количество минут.
            task (str):
                Установка для другой задачи. Если None — для текущей задачи.
        """

        def ensure_delta(delay):
            return timedelta(seconds=int(ensure_time(delay, precision=3) * 60))

        run = []
        if success is not None:
            interval = (
                self.Scheduler_SuccessInterval
                if success
                else self.Scheduler_FailureInterval
            )
            run.append(current_time() + ensure_delta(interval))
        if server_update is not None:
            if server_update is True:
                server_update = self.Scheduler_ServerUpdate
            run.append(get_server_next_update(server_update))
        if target is not None:
            target = [target] if not isinstance(target, list) else target
            target = nearest_future(target)
            run.append(target)
        if minute is not None:
            run.append(current_time() + ensure_delta(minute))

        if len(run):
            run = min(run).replace(microsecond=0)
            kv = dict_to_kv(
                {
                    "success": success,
                    "server_update": server_update,
                    "target": target,
                    "minute": minute,
                },
                allow_none=False,
            )
            if task is None:
                task = self.task.command
            logger.info(f"[Конфигурация] Задача `{task}` отложена до {run} ({kv})")
            self.modified[f'{task}.Scheduler.NextRun'] = run
            self.update()
        else:
            raise ScriptError(
                "[Конфигурация] Для delay_next_run требуется хотя бы один аргумент"
            )

    def opsi_task_delay(
            self,
            recon_scan=False,
            submarine_call=False,
            ap_limit=False,
            cl1_preserve=False,
            ap_limit_minutes=None,
    ):
        """Отложить время NextRun всех задач Operation Siren.

        Args:
            recon_scan (bool): True — отложить на 27 минут все задачи, требующие сканирования разведки.
            submarine_call (bool): True — отложить на 60 минут все задачи, требующие вызова подлодок.
            ap_limit (bool): True — отложить на 360 минут все задачи, требующие очков действия (AP).
            cl1_preserve (bool): True — отложить на 360 минут все задачи с большим расходом AP.
            ap_limit_minutes (int): Использовать это значение при известном времени восстановления AP.
        """
        if not recon_scan and not submarine_call and not ap_limit and not cl1_preserve:
            return None
        kv = dict_to_kv(
            {
                "recon_scan": recon_scan,
                "submarine_call": submarine_call,
                "ap_limit": ap_limit,
                "cl1_preserve": cl1_preserve,
                "ap_limit_minutes": ap_limit_minutes,
            },
            allow_none=False,
        )

        def delay_tasks(task_list, minutes):
            next_run = current_time().replace(microsecond=0) + timedelta(
                minutes=minutes
            )
            for task in task_list:
                keys = f"{task}.Scheduler.NextRun"
                current = deep_get(self.data, keys=keys, default=DEFAULT_TIME)
                if current < next_run:
                    logger.info(f"[Конфигурация — Operation Siren] Задача `{task}` отложена до {next_run} ({kv})")
                    self.modified[keys] = next_run

        def is_submarine_call(task):
            return (
                deep_get(self.data, keys=f"{task}.OpsiFleet.Submarine", default=False)
                or "submarine"
                in deep_get(
                    self.data, keys=f"{task}.OpsiFleetFilter.Filter", default=""
                ).lower()
            )

        def is_force_run(task):
            return (
                deep_get(self.data, keys=f"{task}.OpsiExplore.ForceRun", default=False)
                or deep_get(
                    self.data, keys=f"{task}.OpsiObscure.ForceRun", default=False
                )
                or deep_get(
                    self.data, keys=f"{task}.OpsiAbyssal.ForceRun", default=False
                )
                or deep_get(
                    self.data, keys=f"{task}.OpsiStronghold.ForceRun", default=False
                )
            )

        def is_special_radar(task):
            if task == "OpsiExplore":
                return data_logger_is_active_from_data(self.data)
            return deep_get(
                self.data, keys=f"{task}.OpsiExplore.SpecialRadar", default=False
            )

        if recon_scan:
            tasks = SelectedGrids(["OpsiExplore", "OpsiObscure", "OpsiStronghold"])
            tasks = tasks.delete(tasks.filter(is_force_run)).delete(
                tasks.filter(is_special_radar)
            )
            delay_tasks(tasks, minutes=27)
        if submarine_call:
            tasks = SelectedGrids(
                [
                    "OpsiExplore",
                    "OpsiDaily",
                    "OpsiObscure",
                    "OpsiAbyssal",
                    "OpsiArchive",
                    "OpsiStronghold",
                    "OpsiMeowfficerFarming",
                    "OpsiMonthBoss",
                ]
            )
            tasks = tasks.filter(is_submarine_call).delete(tasks.filter(is_force_run))
            delay_tasks(tasks, minutes=60)
        if ap_limit:
            tasks = SelectedGrids(
                [
                    "OpsiExplore",
                    "OpsiDaily",
                    "OpsiObscure",
                    "OpsiAbyssal",
                    "OpsiStronghold",
                    # Откладываем OpsiArchive, так как OpsiArchive и OpsiDaily делят один список задач,
                    # хотя для входа очки действия не требуются.
                    "OpsiArchive",
                    "OpsiMeowfficerFarming",
                ]
            )
            if ap_limit_minutes is not None:
                delay_tasks(tasks, minutes=ap_limit_minutes)
            elif get_os_reset_remain() > 0:
                delay_tasks(tasks, minutes=360)
            else:
                logger.info("[Конфигурация — Operation Siren] До сброса менее суток: задачи отложены на 2,5 часа")
                delay_tasks(tasks, minutes=150)
        if cl1_preserve:
            tasks = SelectedGrids(
                [
                    "OpsiObscure",
                    "OpsiAbyssal",
                    "OpsiStronghold",
                    "OpsiMeowfficerFarming",
                ]
            )
            delay_tasks(tasks, minutes=360)

        self.update()

    def task_call(self, task, force_call=True):
        """Запланировать запуск другой задачи.

        Задача будет запущена после завершения текущей, но фактический запуск
        может не произойти, если:
        - другая задача имеет более высокий приоритет в SCHEDULER_PRIORITY;
        - задача отключена пользователем.

        Args:
            task (str): имя вызываемой задачи, например `Restart`.
            force_call (bool): принудительно запланировать вызов.

        Returns:
            bool: удалось ли запланировать вызов.
        """
        if deep_get(self.data, keys=f"{task}.Scheduler.NextRun", default=None) is None:
            raise ScriptError(f"[Конфигурация] Вызываемая задача `{task}` отсутствует в пользовательской конфигурации")

        from module.dev_runtime.task_sandbox import (
            authorize_task_call,
            register_task_dependency,
            rollback_task_dependency,
        )

        caller = getattr(getattr(self, "task", None), "command", None)
        authorization = authorize_task_call(self.config_name, caller, task)
        if authorization is not None and not authorization.allowed:
            logger.warning(
                f"[Dev Runtime] Вызов задачи `{task}` заблокирован task sandbox: {authorization.code}"
            )
            return False

        if force_call or self.is_task_enabled(task):
            dependency = None
            dependency_timestamp = None
            if authorization is not None and authorization.new_dependency:
                dependency_timestamp = current_time().isoformat()
                dependency = register_task_dependency(
                    self.config_name,
                    caller=caller,
                    target=task,
                    timestamp=dependency_timestamp,
                )
                if dependency is None or not dependency.allowed:
                    logger.warning(
                        f"[Dev Runtime] Provenance вызова `{task}` не зафиксирована: "
                        f"{dependency.code if dependency is not None else 'DEV_TASK_POLICY_INACTIVE'}"
                    )
                    return False
            logger.info(f"[Конфигурация] Вызов задачи: {task}")
            try:
                self.modified[f"{task}.Scheduler.NextRun"] = current_time().replace(
                    microsecond=0
                )
                self.modified[f"{task}.Scheduler.Enable"] = True
                if self.auto_update:
                    self.update()
            except Exception:
                if dependency_timestamp is not None:
                    try:
                        rolled_back = rollback_task_dependency(
                            self.config_name,
                            caller=caller,
                            target=task,
                            timestamp=dependency_timestamp,
                        )
                    except Exception as rollback_error:
                        logger.error(
                            f"[Dev Runtime] Откат provenance вызова `{task}` завершился ошибкой: "
                            f"{type(rollback_error).__name__}"
                        )
                    else:
                        if rolled_back is None or not rolled_back.rolled_back:
                            logger.error(
                                f"[Dev Runtime] Не удалось откатить provenance вызова `{task}` "
                                f"после ошибки записи профиля: "
                                f"{rolled_back.code if rolled_back is not None else 'DEV_TASK_POLICY_INACTIVE'}; "
                                f"cleanup_pending="
                                f"{rolled_back.policy_marked_cleanup_pending if rolled_back is not None else False}"
                            )
                raise
            if dependency_timestamp is not None:
                try:
                    from module.dev_runtime.hooks import record_dependency_registered

                    record_dependency_registered(
                        self.config_name,
                        caller=caller,
                        target=task,
                        timestamp=dependency_timestamp,
                    )
                except Exception:
                    # Диагностика не должна блокировать штатный вызов задачи.
                    pass
            if dependency is not None and dependency.reason == "dependency_override":
                logger.info(
                    f"[Dev Runtime] Задача `{task}` исключена root-политикой, "
                    f"но временно разрешена как зависимость `{caller}`"
                )
            return True
        else:
            logger.info(f"[Конфигурация] Вызов задачи: {task} (пропущен: задача отключена пользователем)")
            return False

    @staticmethod
    def task_stop(message=""):
        """Остановить текущую задачу.

        Raises:
            TaskEnd: Всегда выбрасывает это исключение для прерывания задачи.
        """
        try:
            from module.base.async_executor import async_executor
            async_executor.flush(timeout=2.0)
        except Exception:
            pass

        if message:
            raise TaskEnd(message)
        else:
            raise TaskEnd

    def task_switched(self):
        """Проверить, требуется ли переключение задачи.

        Returns:
            bool: Требуется ли смена задачи.
        """
        # Обновление события
        if self.stop_event is not None:
            if self.stop_event.is_set():
                return True
        prev = getattr(self, '_task_switch_owner', self.task)
        self.load()
        new = self.get_next()
        if prev == new:
            logger.info(f"[Конфигурация] Продолжение задачи `{new}`")
            return False
        else:
            logger.info(f"[Конфигурация] Переключение с задачи `{prev}` на `{new}`")
            return True

    def check_task_switch(self, message=""):
        """Остановить текущую задачу при переключении задач.

        Raises:
            TaskEnd: Вызывается при переключении задачи.
        """
        # Если установлен флаг отключения переключения задач, проверка пропускается
        if getattr(self, '_disable_task_switch', False):
            logger.info('[Конфигурация] Проверка переключения задач временно отключена')
            return

        if self.task_switched():
            self.task_stop(message=message)

    def is_task_enabled(self, task):
        return bool(self.cross_get(keys=[task, 'Scheduler', 'Enable'], default=False))

    @property
    def campaign_name(self):
        """Имя подкаталога, используемое при сохранении статистики выпадений."""
        name = self.Campaign_Name.lower().replace("-", "_")
        if name[0].isdigit():
            name = "campaign_" + str(name)
        if self.Campaign_Mode == "hard":
            name += "_hard"
        return name

    """
    Следующие настройки и методы сохранены для совместимости со старыми версиями.
    """

    def merge(self, other):
        """Объединить другую конфигурацию с текущей.

        Args:
            other (AzurLaneConfig, Config): Объект конфигурации для слияния.

        Returns:
            AzurLaneConfig: Объединённая конфигурация.
        """
        # Поскольку все задачи выполняются независимо, разделение конфигурации не требуется
        # config = copy.copy(self)
        config = self

        for attr in dir(config):
            if attr.endswith("__"):
                continue
            if hasattr(other, attr):
                value = other.__getattribute__(attr)
                if value is not None:
                    config.__setattr__(attr, value)

        return config

    @property
    def DEVICE_SCREENSHOT_METHOD(self):
        return self.Emulator_ScreenshotMethod

    @property
    def DEVICE_CONTROL_METHOD(self):
        return self.Emulator_ControlMethod

    @property
    def FLEET_1(self):
        return self.Fleet_Fleet1

    @property
    def FLEET_2(self):
        return self.Fleet_Fleet2

    @FLEET_2.setter
    def FLEET_2(self, value):
        self.override(Fleet_Fleet2=value)

    @property
    def SUBMARINE(self):
        return self.Submarine_Fleet

    @SUBMARINE.setter
    def SUBMARINE(self, value):
        self.override(Submarine_Fleet=value)

    _fleet_boss = 0

    @property
    def FLEET_BOSS(self):
        if self._fleet_boss:
            return self._fleet_boss
        if self.Fleet_Fleet2:
            if self.Fleet_FleetOrder in [
                "fleet1_mob_fleet2_boss",
                "fleet1_boss_fleet2_mob",
            ]:
                return 2
            else:
                return 1
        else:
            return 1

    @FLEET_BOSS.setter
    def FLEET_BOSS(self, value):
        self._fleet_boss = value

    def temporary(self, **kwargs):
        """Временно переопределить часть настроек с последующим восстановлением.

        Использование:
            backup = self.config.cover(ENABLE_DAILY_REWARD=False)
            # do_something()
            backup.recover()

        Args:
            **kwargs: Временно переопределяемые параметры конфигурации.

        Returns:
            ConfigBackup: Объект резервной копии для восстановления исходной конфигурации.
        """
        backup = ConfigBackup(config=self)
        backup.cover(**kwargs)
        return backup


pywebio.output.Output = OutputConfig
pywebio.pin.Output = OutputConfig


class ConfigBackup:
    def __init__(self, config):
        """
        Args:
            config (AzurLaneConfig): Резервируемый объект конфигурации.
        """
        self.config = config
        self.backup = {}
        self.kwargs = {}

    def cover(self, **kwargs):
        self.kwargs = kwargs
        for key, value in kwargs.items():
            self.backup[key] = self.config.__getattribute__(key)
            self.config.__setattr__(key, value)

    def recover(self):
        for key, value in self.backup.items():
            self.config.__setattr__(key, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.recover()


class MultiSetWrapper:
    def __init__(self, main):
        """
        Args:
            main (AzurLaneConfig): Экземпляр конфигурации.
        """
        self.main = main
        self.in_wrapper = False

    def __enter__(self):
        if self.main.auto_update:
            self.main.auto_update = False
        else:
            self.in_wrapper = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.in_wrapper:
            self.main.update()
            self.main.auto_update = True
