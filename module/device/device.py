"""设备交互的综合管理入口。整合截图、控制、输入和应用管理功能，
内置防卡死检测（GameStuckError）和点击频率控制（GameTooManyClickError）。"""

# Этот файл определяет класс Device — единую точку взаимодействия сценария с устройством.
# Отвечает за скриншоты, клики, ввод текста; встроенный контроль частоты кликов и защита от зависаний повышают стабильность автоматизации.
import collections
import os
import sys

import cv2
from lxml import etree

from module.device.env import IS_WINDOWS, IS_MACINTOSH
from module.base.timer import Timer
from module.config.time_source import now as current_time
from module.config.utils import get_server_next_update
from module.device.app_control import AppControl
from module.device.control import Control
from module.device.input import Input
from module.device.platform import Platform
from module.device.screenshot import Screenshot
from module.exception import (EmulatorNotRunningError, GameNotRunningError, GameStuckError, GameTooManyClickError,
                              RequestHumanTakeover)
from module.handler.assets import GET_MISSION
from module.logger import logger


def show_function_call():
    """
    INFO     21:07:31.554 │ Function calls:
                       <string>   L1 <module>
                   spawn.py L116 spawn_main()
                   spawn.py L129 _main()
                 process.py L314 _bootstrap()
                 process.py L108 run()
         process_manager.py L149 run_process()
                    alas.py L285 loop()
                    alas.py  L69 run()
                     src.py  L55 rogue()
                   rogue.py  L36 run()
                   rogue.py  L18 rogue_once()
                   entry.py L335 rogue_world_enter()
                    path.py L193 rogue_path_select()
    """
    import os
    import traceback
    stack = traceback.extract_stack()
    func_list = []
    for row in stack:
        filename, line_number, function_name, _ = row
        filename = os.path.basename(filename)
        # Пример: /tasks/character/switch.py:64 character_update()
        func_list.append([filename, str(line_number), function_name])
    max_filename = max([len(row[0]) for row in func_list])
    max_linenum = max([len(row[1]) for row in func_list]) + 1

    def format_(file, line, func):
        file = file.rjust(max_filename, " ")
        line = f'L{line}'.rjust(max_linenum, " ")
        if not func.startswith('<'):
            func = f'{func}()'
        return f'{file} {line} {func}'

    func_list = [f'\n{format_(*row)}' for row in func_list]
    logger.info('Стек вызовов:' + ''.join(func_list))


class Device(Screenshot, Control, AppControl, Input):
    """
    设备交互管理类，整合截图、控制、应用管理和输入功能。

    通过多重继承组合 Screenshot、Control、AppControl、Input 四个模块，
    并通过 Platform 委托模拟器管理操作。
    """
    _screen_size_checked = False
    detect_record = set()
    click_record = collections.deque(maxlen=15)
    stuck_timer = Timer(60, count=60).start()
    stuck_timer_long = Timer(195, count=195).start()
    stuck_long_wait_list = ['BATTLE_STATUS_S', 'PAUSE', 'LOGIN_CHECK', 'TEMPLATE_MANJUU']
    _prev_fingerprint = None
    _stuck_image_timer = Timer(30, count=0)

    def __init__(self, *args, **kwargs):
        # Инициализация платформы управления эмулятором
        self._platform = None

        for trial in range(4):
            try:
                super().__init__(*args, **kwargs)
                break
            except EmulatorNotRunningError:
                if trial >= 3:
                    logger.critical('[Устройство] Не удалось запустить эмулятор после 3 попыток')
                    raise RequestHumanTakeover
                # Попытка запуска эмулятора
                if self.emulator_instance is not None:
                    self.emulator_start()
                else:
                    logger.critical(
                        f'[Устройство] Эмулятор с serial «{self.config.Emulator_Serial}» не найден. Задайте правильный serial'
                    )
                    raise RequestHumanTakeover

        # Убеждаемся, что атрибут package существует (некоторые режимы подключения могут его не задавать)
        # AppControl.app_is_running() использует этот атрибут
        if not hasattr(self, 'package'):
            # Откат к значению конфигурации; если 'auto', последующее определение обновит его
            self.package = getattr(self.config, 'Emulator_PackageName', 'auto')

        # Автоматическое заполнение информации об эмуляторе
        if IS_WINDOWS and self.config.EmulatorInfo_Emulator == 'auto':
            _ = self.emulator_instance

        # На Mac повышаем приоритет работающих эмуляторов
        if IS_MACINTOSH:
            try:
                self.platform.boost_running_emulator_priority()
            except Exception as e:
                logger.warning(f'[Устройство — эмулятор] Не удалось повысить приоритет эмулятора: {e}')

        self.screenshot_interval_set()
        self.method_check()

        # Автоматический выбор самого быстрого метода скриншотов
        if not self.config.is_template_config and self.config.Emulator_ScreenshotMethod == 'auto':
            self.run_simple_screenshot_benchmark()
        # Автоматический выбор устройства OCR
        if not self.config.is_template_config and self.config.Optimization_OcrDevice == 'auto':
            self.run_simple_ocr_benchmark()

        # Предварительная инициализация метода управления
        if self.config.is_actual_task:
            if self.config.Emulator_ControlMethod == 'MaaTouch':
                self.early_maatouch_init()
            if self.config.Emulator_ControlMethod == 'minitouch':
                self.early_minitouch_init()

    @property
    def platform(self):
        """
        获取模拟器管理平台实例。

        惰性初始化，首次访问时创建 Platform 实例。
        """
        if self._platform is None:
            # Когда эмулятор офлайн (обычно сценарий автозапуска),
            # необходимо избегать полного ADB-подключения, иначе Platform снова выбросит
            # исключение EmulatorNotRunningError, пока Device.__init__ обрабатывает его.
            #
            # Поэтому Platform создается с connect=False для легковесной инициализации
            # (config/adb_client/serial), достаточной для обнаружения emulator_instance и
            # вызова emulator_start(); настоящее ADB-подключение выполняется
            # классом Connection после завершения инициализации Device.
            self._platform = Platform(self.config, connect=False)
        return self._platform

    @property
    def emulator_instance(self):
        """
        获取当前模拟器实例。

        Returns:
            模拟器实例对象，未找到时返回 None。
        """
        return self.platform.emulator_instance

    def emulator_start(self):
        """
        启动模拟器，委托给平台特定实现。
        """
        return self.platform.emulator_start()

    def emulator_stop(self):
        """
        停止模拟器，委托给平台特定实现。
        """
        return self.platform.emulator_stop()

    def run_simple_screenshot_benchmark(self):
        """
        运行截图方式基准测试，每种方式测试 3 次，选择最快的写入配置。
        """
        logger.info('[Устройство — тест] Запуск теста методов снимка экрана')
        # Сначала проверяем разрешение
        self.resolution_check_uiautomator2()
        # Выполнение бенчмарка
        from module.daemon.benchmark import Benchmark
        bench = Benchmark(config=self.config, device=self)
        method = bench.run_simple_screenshot_benchmark()
        # Запись конфигурации
        with self.config.multi_set():
            self.config.Emulator_ScreenshotMethod = method
            # if method == 'nemu_ipc':
            #     self.config.Emulator_ControlMethod = 'nemu_ipc'

    def run_simple_ocr_benchmark(self):
        """
        运行 OCR 设备基准测试，优先测试 GPU。

        准确率 100% 则选择 'gpu'，否则回退到 'cpu'。
        """
        logger.info('[Устройство — OCR benchmark] Проверка доступных OCR-устройств')
        from module.daemon.ocr_benchmark import OcrBenchmark
        bench = OcrBenchmark(config=self.config, device=self)
        device = bench.run_simple_ocr_benchmark()
        # Запись конфигурации
        with self.config.multi_set():
            self.config.Optimization_OcrDevice = device
            # После записи конфигурации необходимо заново выполнить reset_ocr_model().
            # Поскольку run_simple_ocr_benchmark() внутри перезаписывает и сбрасывает состояние,
            # нужно убедиться, что итоговое состояние совпадает со сохраненной конфигурацией.
            from module.ocr.al_ocr import reset_ocr_model
            reset_ocr_model()

    def method_check(self):
        """
        检查截图方式和控制方式的组合是否合法。
        """
        # Скриншоты и управление nemu_ipc должны использоваться совместно
        # if self.config.Emulator_ScreenshotMethod == 'nemu_ipc' and self.config.Emulator_ControlMethod != 'nemu_ipc':
        #     logger.warning('When using nemu_ipc, both screenshot and control should use nemu_ipc')
        #     self.config.Emulator_ControlMethod = 'nemu_ipc'
        # if self.config.Emulator_ScreenshotMethod != 'nemu_ipc' and self.config.Emulator_ControlMethod == 'nemu_ipc':
        #     logger.warning('When not using nemu_ipc, both screenshot and control should not use nemu_ipc')
        #     self.config.Emulator_ControlMethod = 'minitouch'
        # Hermit разрешен к использованию только на VMOS
        if self.config.Emulator_ControlMethod == 'Hermit' and not self.is_vmos:
            logger.warning('[Устройство — методы] Метод управления Hermit разрешён только в VMOS')
            self.config.Emulator_ControlMethod = 'MaaTouch'
        if self.config.Emulator_ScreenshotMethod == 'ldopengl' \
                and self.config.Emulator_ControlMethod == 'minitouch':
            logger.warning('[Устройство — методы] Для LDPlayer следует использовать MaaTouch')
            self.config.Emulator_ControlMethod = 'MaaTouch'

        # nemu_ipc и ldopengl на неподходящих эмуляторах откатываются к auto
        if self.config.Emulator_ScreenshotMethod == 'nemu_ipc':
            if not (self.is_emulator and self.is_mumu_family):
                logger.warning('[Устройство — методы] Метод снимка экрана nemu_ipc поддерживается только MuMu 12; выполняется возврат к auto')
                self.config.Emulator_ScreenshotMethod = 'auto'
        if self.config.Emulator_ScreenshotMethod == 'ldopengl':
            if not (self.is_emulator and self.is_ldplayer_bluestacks_family):
                logger.warning('[Устройство — методы] Метод снимка экрана ldopengl поддерживается только LDPlayer; выполняется возврат к auto')
                self.config.Emulator_ScreenshotMethod = 'auto'
        if not IS_WINDOWS and self.config.Emulator_ScreenshotMethod in ['nemu_ipc', 'ldopengl']:
            logger.warning(f'[Устройство — методы] Метод снимка экрана {self.config.Emulator_ScreenshotMethod} поддерживается только в Windows; выполняется возврат к auto')
            self.config.Emulator_ScreenshotMethod = 'auto'

    def handle_night_commission(self, daily_trigger='21:00', threshold=30):
        """
        检测并处理夜间委托刷新弹窗。

        Args:
            daily_trigger: 委托刷新时间点。
            threshold: 刷新时间前后多少秒内触发检测。

        Returns:
            是否点击了委托弹窗。
        """
        update = get_server_next_update(daily_trigger=daily_trigger)
        now = current_time()
        diff = (update.timestamp() - now.timestamp()) % 86400
        if threshold < diff < 86400 - threshold:
            return False

        if GET_MISSION.match(self.image, offset=True):
            logger.info('[Устройство — комиссии] Появилась ночная комиссия')
            self.click(GET_MISSION)
            return True

        return False

    def screenshot(self):
        """
        Сделать снимок экрана с проверкой зависания и ночной комиссией.

        Returns:
            Изображение экрана в формате массива numpy.
        """
        from module.observability.tracing import trace_operation

        with trace_operation("azurpilot.device.screenshot"):
            self._raise_if_cooperative_stop_requested()
            self.stuck_record_check()

            try:
                super().screenshot()
            except RequestHumanTakeover:
                if not self.ascreencap_available:
                    logger.error('[Устройство — снимок] aScreenCap недоступен на текущем устройстве; выполняется возврат к auto')
                    self.run_simple_screenshot_benchmark()
                    super().screenshot()
                else:
                    raise

            if self.handle_night_commission():
                super().screenshot()

            self._check_image_stuck()
            if os.environ.get("AZURPILOT_DEV_SESSION_ID"):
                from module.dev_runtime.hooks import serve_pending_screenshot

                serve_pending_screenshot(self.image)
            return self.image

    def _raise_if_cooperative_stop_requested(self) -> None:
        """Прервать task на общей interruptible границе после запроса handover."""

        config = getattr(self, "config", None)
        stop_event = getattr(config, "stop_event", None)
        if stop_event is None or not stop_event.is_set():
            return
        config_name = getattr(config, "config_name", "worker")
        logger.info(
            f"[{config_name}] Worker увидел запрос cooperative stop на границе снимка"
        )
        task_stop = getattr(config, "task_stop", None)
        if callable(task_stop):
            task_stop(message="Получен запрос cooperative stop")
        from module.config.config import TaskEnd

        raise TaskEnd("Получен запрос cooperative stop")

    def dump_hierarchy(self) -> etree._Element:
        self.stuck_record_check()
        return super().dump_hierarchy()

    def release_during_wait(self):
        """
        等待期间释放截图资源，避免后台持续占用。
        """
        # Сервер Scrcpy непрерывно передает видеопоток, на время ожидания его нужно останавливать
        if self.config.Emulator_ScreenshotMethod == 'scrcpy':
            self._scrcpy_server_stop()
        if self.config.Emulator_ScreenshotMethod == 'nemu_ipc':
            self.nemu_ipc_release()

    def get_orientation(self):
        """
        获取屏幕方向，方向变化时触发回调。
        """
        o = super().get_orientation()

        self.on_orientation_change_maatouch()

        return o

    def stuck_record_add(self, button):
        self.detect_record.add(str(button))

    def stuck_record_clear(self):
        self.detect_record = set()
        self.stuck_timer.reset()
        self.stuck_timer_long.reset()
        self._stuck_image_timer.clear()

    def _check_image_stuck(self):
        if self.image is None:
            return

        small = cv2.resize(self.image, (16, 16))
        fp = hash(small.tobytes())

        if self._prev_fingerprint is not None and fp == self._prev_fingerprint:
            self._stuck_image_timer.start()
            if self._stuck_image_timer.reached():
                show_function_call()
                logger.warning(f'[Устройство — зависание] Снимок экрана не изменялся более {self._stuck_image_timer.limit} с')
                self.stuck_record_clear()
                if self.app_is_running():
                    raise GameStuckError('[Устройство — зависание] Снимок экрана не изменяется')
                else:
                    raise GameNotRunningError('[Устройство — зависание] Игра завершила работу')
        else:
            self._prev_fingerprint = fp
            self._stuck_image_timer.clear()

    def stuck_record_check(self):
        """
        检查是否卡死（操作超时或长时间无有效截图操作）。

        Raises:
            GameStuckError: 游戏卡死。
            GameNotRunningError: 游戏已退出。
        """
        reached = self.stuck_timer.reached()
        reached_long = self.stuck_timer_long.reached()

        if not reached:
            return False
        if not reached_long:
            for button in self.stuck_long_wait_list:
                if button in self.detect_record:
                    return False

        show_function_call()
        logger.warning('[Устройство — зависание] Превышено время ожидания')
        logger.warning(f'[Устройство — зависание] Ожидаемые элементы: {self.detect_record}')
        self.stuck_record_clear()

        if self.app_is_running():
            raise GameStuckError('[Устройство — зависание] Превышено время ожидания')
        else:
            raise GameNotRunningError('[Устройство — зависание] Игра завершила работу')

    def handle_control_check(self, button):
        self.stuck_record_clear()
        self.click_record_add(button)
        self.click_record_check()

    def click_record_add(self, button):
        self.click_record.append(str(button))

    def click_record_clear(self):
        self.click_record.clear()

    def click_record_remove(self, button):
        """
        从点击记录中移除指定按钮的所有记录。

        Args:
            button: 要移除的按钮对象。

        Returns:
            移除的记录数量。
        """
        removed = 0
        for _ in range(self.click_record.maxlen):
            try:
                self.click_record.remove(str(button))
                removed += 1
            except ValueError:
                # Этого значения уже нет в очереди
                break

        return removed

    def click_record_check(self):
        """
        检查点击频率是否异常（同一按钮被点击过多或两个按钮交替点击过多）。

        Raises:
            GameTooManyClickError: 点击频率异常。
        """
        count = collections.Counter(self.click_record).most_common(2)
        if count[0][1] >= 12:
            show_function_call()
            logger.warning(f'[Устройство — нажатия] Слишком много нажатий на кнопку: {count[0][0]}')
            logger.warning(f'[Устройство — нажатия] История нажатий: {[str(prev) for prev in self.click_record]}')
            self.click_record_clear()
            raise GameTooManyClickError(f'[Устройство — нажатия] Слишком много нажатий на кнопку: {count[0][0]}')
        if len(count) >= 2 and count[0][1] >= 6 and count[1][1] >= 6:
            show_function_call()
            logger.warning(f'[Устройство — нажатия] Слишком много попеременных нажатий на две кнопки: {count[0][0]}, {count[1][0]}')
            logger.warning(f'[Устройство — нажатия] История нажатий: {[str(prev) for prev in self.click_record]}')
            self.click_record_clear()
            raise GameTooManyClickError(f'[Устройство — нажатия] Слишком много попеременных нажатий на две кнопки: {count[0][0]}, {count[1][0]}')

    def disable_stuck_detection(self):
        """
        禁用卡死检测，用于半自动模式和调试场景。
        """
        logger.info('[Устройство — контроль] Обнаружение зависания отключено')

        def empty_function(*arg, **kwargs):
            return False

        self.click_record_check = empty_function
        self.stuck_record_check = empty_function

    def app_start(self):
        if not self.config.Error_HandleError:
            logger.critical('[Устройство] Приложение не запускается и не останавливается, потому что HandleError отключён')
            logger.critical('[Устройство] Включите Alas.Error.HandleError или войдите в Azur Lane вручную')
            raise RequestHumanTakeover
        if getattr(self.config, 'Emulator_GameSettings', False):
            from module.game_setting.player_prefs import apply_recommended_game_settings

            wait_for_stop = getattr(self, '_game_settings_wait_for_stop', False)
            apply_recommended_game_settings(self, wait_for_stop=wait_for_stop)
            self._game_settings_wait_for_stop = False
        super().app_start()
        self.stuck_record_clear()
        self.click_record_clear()

    def app_stop(self):
        if not self.config.Error_HandleError:
            logger.critical('[Устройство] Приложение не запускается и не останавливается, потому что HandleError отключён')
            logger.critical('[Устройство] Включите Alas.Error.HandleError или войдите в Azur Lane вручную')
            raise RequestHumanTakeover
        super().app_stop()
        if getattr(self.config, 'Emulator_GameSettings', False):
            self._game_settings_wait_for_stop = True
        self.stuck_record_clear()
        self.click_record_clear()
