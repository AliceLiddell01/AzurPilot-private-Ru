"""Модуль управления вводом устройства.

Централизованно управляет всеми сенсорными операциями (нажатия, долгие нажатия, свайпы,
перетаскивания), автоматически направляя их в соответствующую реализацию согласно
настроенному методу управления (ADB, uiautomator2, minitouch, Hermit, MaaTouch, scrcpy, nemu_ipc).
"""
from module.base.button import Button
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.base.utils import *
from module.device.method.hermit import Hermit
from module.device.method.maatouch import MaaTouch
from module.device.method.minitouch import Minitouch
from module.device.method.nemu_ipc import NemuIpc
from module.device.method.scrcpy import Scrcpy
from module.logger import logger


class Control(Hermit, Minitouch, Scrcpy, MaaTouch, NemuIpc):
    """Диспетчер сенсорного управления устройства.

    Объединяет все бэкенды управления (Hermit, Minitouch, Scrcpy, MaaTouch, NemuIpc) через
    множественное наследование, автоматически направляя вызовы в реализацию согласно
    настроенному Emulator_ControlMethod. Предоставляет единый интерфейс клика, долгого
    нажатия, свайпа и перетаскивания.
    """
    def handle_control_check(self, button):
        # Будет переопределено в Device
        pass

    @cached_property
    def click_methods(self):
        """Возвращает словарь соответствия имени метода управления реализации клика.

        Returns:
            dict[str, Callable]: Ключ — имя метода управления (например, 'ADB', 'minitouch'),
                значение — соответствующий метод нажатия.
        """
        return {
            'ADB': self.click_adb,
            'uiautomator2': self.click_uiautomator2,
            'minitouch': self.click_minitouch,
            'Hermit': self.click_hermit,
            'MaaTouch': self.click_maatouch,
            'nemu_ipc': self.click_nemu_ipc,
        }

    def click(self, button, control_check=True):
        """Нажимает на кнопку.

        Args:
            button (button.Button): Экземпляр кнопки Azur Lane.
            control_check (bool): Выполнять ли проверку управления.
        """
        if control_check:
            self.handle_control_check(button)
        x, y = random_rectangle_point(button.button)
        x, y = ensure_int(x, y)
        logger.debug(
            '[Устройство — управление] Нажатие %s в %s' % (point2str(x, y), button)
        )
        method = self.click_methods.get(
            self.config.Emulator_ControlMethod,
            self.click_adb
        )
        method(x, y)

    def multi_click(self, button, n, interval=(0.1, 0.2)):
        """Выполняет несколько последовательных нажатий на кнопку.

        Args:
            button (button.Button): Экземпляр кнопки Azur Lane.
            n (int): Количество нажатий.
            interval (tuple): Диапазон интервала между нажатиями (в секундах), формат (min, max).
        """
        self.handle_control_check(button)
        click_timer = Timer(0.1)
        for _ in range(n):
            remain = ensure_time(interval) - click_timer.current_time()
            if remain > 0:
                self.sleep(remain)
            click_timer.reset()

            self.click(button, control_check=False)

    def long_click(self, button, duration=(1, 1.2)):
        """Выполняет долгое нажатие на кнопку.

        Args:
            button (button.Button): Экземпляр кнопки Azur Lane.
            duration (int, float, tuple): Длительность долгого нажатия.
        """
        self.handle_control_check(button)
        x, y = random_rectangle_point(button.button)
        x, y = ensure_int(x, y)
        duration = ensure_time(duration)
        logger.debug(
            '[Устройство — управление] Долгое нажатие %s в %s, длительность %s' % (point2str(x, y), button, duration)
        )
        method = self.config.Emulator_ControlMethod
        if method == 'minitouch':
            self.long_click_minitouch(x, y, duration)
        elif method == 'uiautomator2':
            self.long_click_uiautomator2(x, y, duration)
        elif method == 'scrcpy':
            self.long_click_scrcpy(x, y, duration)
        elif method == 'MaaTouch':
            self.long_click_maatouch(x, y, duration)
        elif method == 'nemu_ipc':
            self.long_click_nemu_ipc(x, y, duration)
        else:
            self.swipe_adb((x, y), (x, y), duration)

    def swipe(self, p1, p2, duration=(0.1, 0.2), name='SWIPE', distance_check=True):
        """Выполняет операцию свайпа между двумя точками.

        Длительность свайпа для ADB автоматически умножается на 2.5 для обеспечения надёжности.
        Проверка расстояния отбрасывает свайпы короче 10 пикселей (Azur Lane воспринимает их как клик).

        Args:
            p1 (tuple): Начальные координаты (x, y).
            p2 (tuple): Конечные координаты (x, y).
            duration (int, float, tuple): Длительность свайпа (в секундах).
            name (str): Имя операции свайпа для вывода в журнал.
            distance_check (bool): Проверять ли дистанцию, пропуская операцию при слишком малом расстоянии.
        """
        self.handle_control_check(name)
        p1, p2 = ensure_int(p1, p2)
        duration = ensure_time(duration)
        method = self.config.Emulator_ControlMethod
        if method == 'uiautomator2':
            logger.debug('[Устройство — управление] Свайп %s → %s, длительность %s' % (point2str(*p1), point2str(*p2), duration))
        elif method in ['minitouch', 'MaaTouch', 'scrcpy', 'nemu_ipc']:
            logger.debug('[Устройство — управление] Свайп %s → %s' % (point2str(*p1), point2str(*p2)))
        else:
            # Для ADB нужна меньшая скорость, иначе свайп может не сработать
            duration *= 2.5
            logger.debug('[Устройство — управление] Свайп %s → %s, длительность %s' % (point2str(*p1), point2str(*p2), duration))

        if distance_check:
            if np.linalg.norm(np.subtract(p1, p2)) < 10:
                # Нужна минимальная длина свайпа, иначе Azur Lane распознает его как нажатие
                # Для uiautomator2 требуется >= 6 px, для minitouch — >= 5 px
                logger.debug('[Устройство — управление] Длина свайпа меньше 10 px; команда отброшена')
                return

        if method == 'minitouch':
            self.swipe_minitouch(p1, p2)
        elif method == 'uiautomator2':
            self.swipe_uiautomator2(p1, p2, duration=duration)
        elif method == 'scrcpy':
            self.swipe_scrcpy(p1, p2)
        elif method == 'MaaTouch':
            self.swipe_maatouch(p1, p2)
        elif method == 'nemu_ipc':
            self.swipe_nemu_ipc(p1, p2)
        else:
            self.swipe_adb(p1, p2, duration=duration)

    def swipe_vector(self, vector, box=(123, 159, 1175, 628), random_range=(0, 0, 0, 0), padding=15,
                     duration=(0.1, 0.2), whitelist_area=None, blacklist_area=None, name='SWIPE', distance_check=True):
        """Выполняет векторный свайп в заданной области.

        Args:
            box (tuple): Область свайпа, формат (x_min, y_min, x_max, y_max).
            vector (tuple): Вектор свайпа, формат (x, y).
            random_range (tuple): Диапазон случайного смещения (x_min, y_min, x_max, y_max).
            padding (int): Внутренний отступ.
            duration (int, float, tuple): Длительность свайпа.
            whitelist_area (list[tuple[int]]): Список безопасных зон клика, где путь свайпа завершится.
            blacklist_area (list[tuple[int]]): Запретные зоны, используемые если белый список недоступен.
                Исключает случайные траектории с концом в чёрном списке.
            name (str): Имя свайпа.
            distance_check (bool): Выполнять ли проверку дистанции.
        """
        p1, p2 = random_rectangle_vector_opted(
            vector,
            box=box,
            random_range=random_range,
            padding=padding,
            whitelist_area=whitelist_area,
            blacklist_area=blacklist_area
        )
        self.swipe(p1, p2, duration=duration, name=name, distance_check=distance_check)

    def drag(self, p1, p2, segments=1, shake=(0, 15), point_random=(-10, -10, 10, 10), shake_random=(-5, -5, 5, 5),
             swipe_duration=0.25, shake_duration=0.1, name='DRAG'):
        """Выполняет операцию перетаскивания (drag) с поддержкой сегментированного свайпа и имитации покачивания при отпускании.

        Используется в сценариях Azur Lane, требующих точного перетаскивания (экипировка, настройка флота).
        Бэкенды, не поддерживающие drag, откатываются на ADB swipe + click.

        Args:
            p1 (tuple): Начальные координаты (x, y).
            p2 (tuple): Конечные координаты (x, y).
            segments (int): Число сегментов свайпа.
            shake (tuple): Смещение покачивания после отпускания (x, y).
            point_random (tuple): Случайное смещение начальной точки (x_min, y_min, x_max, y_max).
            shake_random (tuple): Случайное смещение покачивания (x_min, y_min, x_max, y_max).
            swipe_duration (float): Длительность свайпа (в секундах).
            shake_duration (float): Длительность покачивания (в секундах).
            name (str): Имя операции перетаскивания для журнала.
        """
        self.handle_control_check(name)
        p1, p2 = ensure_int(p1, p2)
        logger.debug(
            '[Устройство — управление] Перетаскивание %s → %s' % (point2str(*p1), point2str(*p2))
        )
        method = self.config.Emulator_ControlMethod
        if method == 'minitouch':
            self.drag_minitouch(p1, p2, point_random=point_random)
        elif method == 'uiautomator2':
            self.drag_uiautomator2(
                p1, p2, segments=segments, shake=shake, point_random=point_random, shake_random=shake_random,
                swipe_duration=swipe_duration, shake_duration=shake_duration)
        elif method == 'scrcpy':
            self.drag_scrcpy(p1, p2, point_random=point_random)
        elif method == 'MaaTouch':
            self.drag_maatouch(p1, p2, point_random=point_random)
        elif method == 'nemu_ipc':
            self.drag_nemu_ipc(p1, p2, point_random=point_random)
        else:
            logger.warning(f'[Устройство — управление] Метод {method} не поддерживает перетаскивание. Использование свайпа ADB может привести к неожиданному поведению')
            self.swipe_adb(p1, p2, duration=ensure_time(swipe_duration * 2))
            self.click(Button(area=(), color=(), button=area_offset(point_random, p2), name=name), False)

    def island_swipe_hold(self, p1, p2, hold_time):
        """Операция свайпа с удержанием, предназначенная для островной системы.

        Выполняет свайп между двумя точками и удерживает палец в конечной точке, используется для взаимодействия на острове.

        Args:
            p1 (tuple): Начальные координаты (x, y).
            p2 (tuple): Конечные координаты (x, y).
            hold_time (int, float, tuple): Время удержания в конечной точке (в секундах).
        """
        p1, p2 = ensure_int(p1, p2)
        hold_time = ensure_time(hold_time)
        method = self.config.Emulator_ControlMethod
        if method == 'minitouch':
            self.island_swipe_hold_minitouch(p1, p2, hold_time)