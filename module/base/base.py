"""Определение базового модуля.

Определяет высший базовый класс ModuleBase для всех модулей игровой логики, объединяя
управление конфигурацией, контроль устройства, навигацию по UI, управление циклами задач
и базовую логику обработки исключений, являясь общим предком всех функциональных модулей.
"""

from typing import Tuple, Union

from module.base.button import Button
from module.base.decorator import cached_property
# Этот файл определяет высший базовый класс логических модулей Alas — ModuleBase.
# Как общий предок всех конкретных функциональных модулей, он объединяет UI-навигацию, управление циклами задач и базовую обработку исключений.
from module.base.timer import Timer
from module.base.utils import *
from module.combat.emotion import Emotion
from module.config.config import AzurLaneConfig
from module.config.server import set_server, to_package
from module.device.device import Device
from module.device.method.utils import HierarchyButton
from module.logger import logger
from module.map_detection.utils import fit_points
from module.statistics.azurstats import AzurStats
from module.webui.setting import cached_class_property


class ModuleBase:
    config: AzurLaneConfig
    device: Device

    EARLY_OCR_IMPORT = False

    def __init__(self, config, device=None, task=None):
        """
        Инициализация базового класса модуля, привязка конфигурации и устройства.

        Args:
            config: Объект конфигурации или имя конфигурации.
                При передаче экземпляра AzurLaneConfig используется напрямую,
                при передаче str загружается из ./config/.
            device: Объект устройства, серийный номер устройства или None.
                При передаче экземпляра Device повторно используется существующее устройство,
                при передаче str задаётся серийный номер эмулятора,
                при None автоматически создаётся новое устройство.
            task: Имя привязанной задачи, используется только для разработки и отладки.
                При автоматическом диспетчеризировании обычно None с конфигурацией по умолчанию.
        """
        if isinstance(config, AzurLaneConfig):
            self.config = config
            if task is not None:
                self.config.init_task(task)
        elif isinstance(config, str):
            self.config = AzurLaneConfig(config, task=task)
        else:
            logger.warning('[Базовый класс] Получен неизвестный объект конфигурации; предполагается AzurLaneConfig')
            self.config = config

        if isinstance(device, Device):
            self.device = device
        elif device is None:
            self.device = Device(config=self.config)
        elif isinstance(device, str):
            self.config.override(Emulator_Serial=device)
            self.device = Device(config=self.config)
        else:
            logger.warning('[Базовый класс] Получен неизвестный объект устройства; предполагается Device')
            self.device = device

        self.interval_timer = {}
        self.early_ocr_import()

    @cached_property
    def stat(self) -> AzurStats:
        return AzurStats(config=self.config)

    @cached_property
    def emotion(self) -> Emotion:
        return Emotion(config=self.config)

    def early_ocr_import(self):
        """
        Асинхронный предварительный импорт моделей OCR.

        При запуске создания снимка экрана фоновый поток заранее загружает зависимости
        OCR, такие как cnocr. Снимок экрана — I/O-интенсивная операция, импорт — CPU-интенсивная,
        их параллельное выполнение ускоряет запуск на 0.5~5 секунд.
        """
        return

    @cached_class_property
    def worker(self):
        """
        Пул фоновых потоков для выполнения неблокирующих фоновых задач.

        Examples:
            >>> def func(image):
            ...     with self.config.multi_set():
            ...         self.dungeon_get_simuni_point(image)
            ...         self.dungeon_update_stamina(image)
            >>> ModuleBase.worker.submit(func, self.device.image)
        """
        logger.hr('Создание пула фоновых потоков')
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(1)
        return pool

    def ensure_button(self, button):
        if isinstance(button, str):
            button = HierarchyButton(self.device.hierarchy, button)

        return button

    def loop(self, skip_first=True, timeout=None):
        """
        Синтаксический сахар для цикла состояний с автоматическим созданием снимка экрана на каждой итерации.

        Args:
            skip_first: Если True, повторно использует предыдущий снимок во избежание лишнего захвата.
            timeout: Таймаут в секундах или объект Timer; по истечении таймаута автоматически завершает цикл.

        Yields:
            np.ndarray: Текущий снимок экрана.

        Examples:
            Базовый цикл состояний:
            >>> for _ in self.loop():
            ...     if self.appear(END_CONDITION):
            ...         break
            ...     if self.appear_then_click(BUTTON_A):
            ...         continue

            Цикл состояний с таймаутом:
            >>> for _ in self.loop(timeout=2):
            ...     if self.appear(END_CONDITION):
            ...         break
            >>> else:
            ...     logger.warning('Ожидание истекло')
        """
        if timeout is not None:
            if isinstance(timeout, Timer):
                timeout.reset()
            else:
                timeout = Timer.from_seconds(timeout).start()

        while 1:
            if timeout is not None:
                if timeout.reached():
                    return

            if skip_first:
                skip_first = False
            else:
                self.device.screenshot()

            try:
                yield self.device.image
            except AttributeError:
                self.device.screenshot()
                yield self.device.image

    def loop_hierarchy(self, skip_first=True):
        """
        Синтаксический сахар для цикла состояний иерархической структуры с автоматическим получением дерева UI.

        Args:
            skip_first: Если True, повторно использует предыдущие данные иерархии.

        Yields:
            etree._Element: Текущее дерево иерархии UI.
        """
        while 1:
            if skip_first:
                skip_first = False
            else:
                self.device.dump_hierarchy()
            yield self.device.hierarchy

    def loop_screenshot_hierarchy(self, skip_first=True):
        """
        Синтаксический сахар для цикла состояний с одновременным получением снимка экрана и дерева иерархии.

        Args:
            skip_first: Если True, повторно использует предыдущий снимок и данные иерархии.

        Yields:
            tuple[np.ndarray, etree._Element]: (снимок экрана, дерево иерархии UI).
        """
        while 1:
            if skip_first:
                skip_first = False
            else:
                self.device.screenshot()
                self.device.dump_hierarchy()
            yield self.device.image, self.device.hierarchy

    def appear(self, button, offset: Union[bool, int, Tuple[int, int]] = 0, interval=0, similarity=0.85, threshold=10):
        """
        Определяет, отображается ли кнопка/шаблон/элемент иерархии на текущем снимке экрана.

        Поддерживает три режима обнаружения:
        - Обнаружение по цвету (по умолчанию): определение по среднему цвету области
        - Сопоставление с шаблоном (offset не 0): определение по совпадению с шаблоном изображения
        - Обнаружение по иерархии (HierarchyButton): поиск в дереве иерархии UI по xpath

        Args:
            button: Проверяемый Button, Template, HierarchyButton или строка xpath.
            offset: Смещение для сопоставления с шаблоном.
                False/0 означает использование проверки цвета, True использует смещение по умолчанию,
                int/tuple задаёт диапазон смещения.
            interval: Минимальный интервал в секундах между двумя проверками для предотвращения частых срабатываний.
            similarity: Порог сходства шаблона, от 0 до 1.
            threshold: Допуск при проверке цвета, 0~255; чем меньше значение, тем строже проверка.

        Returns:
            bool: Отображается ли элемент.
        """
        button = self.ensure_button(button)
        self.device.stuck_record_add(button)

        if interval:
            if button.name in self.interval_timer:
                if self.interval_timer[button.name].limit != interval:
                    self.interval_timer[button.name] = Timer(interval)
            else:
                self.interval_timer[button.name] = Timer(interval)
            if not self.interval_timer[button.name].reached():
                return False

        if isinstance(button, HierarchyButton):
            appear = bool(button)
        elif offset:
            if isinstance(offset, bool):
                offset = self.config.BUTTON_OFFSET
            appear = button.match(self.device.image, offset=offset, similarity=similarity)
        else:
            appear = button.appear_on(self.device.image, threshold=threshold)

        if appear and interval:
            self.interval_timer[button.name].reset()

        return appear

    def match_template_color(self, button, offset=(20, 20), interval=0, similarity=0.85, threshold=30):
        """
        Одновременно использует сопоставление с шаблоном и проверку цвета для определения наличия кнопки.

        В отличие от `appear()`, данный метод требует успешного прохождения как сопоставления шаблона,
        так и проверки цвета.

        Args:
            button: Проверяемый экземпляр Button.
            offset: Диапазон смещения шаблона.
            interval: Минимальный интервал между проверками в секундах.
            similarity: Порог сходства шаблона, от 0 до 1.
            threshold: Допуск при проверке цвета, от 0 до 255.

        Returns:
            bool: Отображается ли кнопка.
        """
        button = self.ensure_button(button)
        self.device.stuck_record_add(button)

        if interval:
            if button.name in self.interval_timer:
                if self.interval_timer[button.name].limit != interval:
                    self.interval_timer[button.name] = Timer(interval)
            else:
                self.interval_timer[button.name] = Timer(interval)
            if not self.interval_timer[button.name].reached():
                return False

        appear = button.match_template_color(
            self.device.image, offset=offset, similarity=similarity, threshold=threshold)

        if appear and interval:
            self.interval_timer[button.name].reset()

        return appear

    def appear_then_click(self, button, screenshot=False, genre='items',
                          offset: Union[bool, int, Tuple[int, int]] = 0, interval=0, similarity=0.85,
                          threshold=30):
        button = self.ensure_button(button)
        appear = self.appear(button, offset=offset, interval=interval, similarity=similarity, threshold=threshold)
        if appear:
            if screenshot:
                self.device.sleep(self.config.WAIT_BEFORE_SAVING_SCREEN_SHOT)
                self.device.screenshot()
                self.device.save_screenshot(genre=genre)
            self.device.sleep(0.1)  # Трагический случай: из-за слишком быстрого клика отправили в отставку лишний золотой корабль коллаборации QAQ
            self.device.click(button)
        return appear

    def wait_until_appear(self, button, offset=0, skip_first_screenshot=False):
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if self.appear(button, offset=offset):
                break

    def wait_until_appear_then_click(self, button, offset=0):
        self.wait_until_appear(button, offset=offset)
        self.device.click(button)

    def wait_until_disappear(self, button, offset=0):
        while 1:
            self.device.screenshot()
            if not self.appear(button, offset=offset):
                break

    def wait_until_stable(self, button, timer=Timer(0.3, count=1), timeout=Timer(5, count=10), skip_first_screenshot=True):
        button._match_init = False
        timeout.reset()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if button._match_init:
                if button.match(self.device.image, offset=(0, 0)):
                    if timer.reached():
                        break
                else:
                    button.load_color(self.device.image)
                    timer.reset()
            else:
                button.load_color(self.device.image)
                button._match_init = True

            if timeout.reached():
                logger.warning(f'[Базовый класс] Истекло время ожидания wait_until_stable({button})')
                break

    def image_crop(self, button, copy=True):
        """
        Обрезает заданную область из текущего снимка экрана.

        Args:
            button: Экземпляр Button или кортеж области (x1, y1, x2, y2).
            copy: Копировать ли результат обрезки; если False, возвращает срез исходного изображения для экономии памяти.

        Returns:
            np.ndarray: Обрезанное изображение.
        """
        if isinstance(button, Button):
            return crop(self.device.image, button.area, copy=copy)
        elif hasattr(button, 'area'):
            return crop(self.device.image, button.area, copy=copy)
        else:
            return crop(self.device.image, button, copy=copy)

    def image_color_count(self, button, color, threshold=221, count=50):
        """
        Подсчитывает количество пикселей, близких к целевому цвету в указанной области, определяя достижение порога.

        Args:
            button: Экземпляр Button, кортеж области или изображение np.ndarray.
            color: Целевое значение цвета RGB.
            threshold: Допуск сходства цвета, 255 означает полное совпадение; чем меньше значение, тем строже критерий.
            count: Порог количества пикселей; при превышении возвращает True.

        Returns:
            bool: Превышает ли число совпадающих пикселей заданный порог.
        """
        if isinstance(button, np.ndarray):
            image = button
        else:
            image = self.image_crop(button, copy=False)
        return image_color_count(image, color, threshold, count)

    def image_color_button(self, area, color, color_threshold=250, encourage=5, name='COLOR_BUTTON'):
        """
        Находит однотонную область в пределах заданных координат и преобразует её в кликабельный Button.

        Args:
            area: Область поиска (x1, y1, x2, y2).
            color: Целевое значение цвета RGB.
            color_threshold: Допуск совпадения цвета, 0~255, где 255 — точное совпадение.
            encourage: Радиус генерируемой кнопки.
            name: Имя кнопки.

        Returns:
            Button: При успешном совпадении возвращает экземпляр Button, иначе None.
        """
        image = color_similarity_2d(self.image_crop(area, copy=False), color=color)
        points = np.array(np.where(image > color_threshold)).T[:, ::-1]
        if points.shape[0] < encourage ** 2:
            # Недостаточно подходящих пикселей для создания корректной кнопки
            return None

        point = fit_points(points, mod=image_size(image), encourage=encourage)
        point = ensure_int(point + area[:2])
        button_area = area_offset((-encourage, -encourage, encourage, encourage), offset=point)
        color = get_color(self.device.image, button_area)
        return Button(area=button_area, color=color, button=button_area, name=name)

    def get_interval_timer(self, button, interval=5, renew=False) -> Timer:
        if hasattr(button, 'name'):
            name = button.name
        elif callable(button):
            name = button.__name__
        else:
            name = str(button)

        try:
            timer = self.interval_timer[name]
            if renew and timer.limit != interval:
                timer = Timer(interval)
                self.interval_timer[name] = timer
            return timer
        except KeyError:
            timer = Timer(interval)
            self.interval_timer[name] = timer
            return timer

    def interval_reset(self, button, interval=3):
        if isinstance(button, (list, tuple)):
            for b in button:
                self.interval_reset(b)
            return

        if button is not None:
            if button.name in self.interval_timer:
                self.interval_timer[button.name].reset()
            else:
                self.interval_timer[button.name] = Timer(interval).reset()

    def interval_clear(self, button, interval=3):
        if isinstance(button, (list, tuple)):
            for b in button:
                self.interval_clear(b)
            return

        if button is not None:
            if button.name in self.interval_timer:
                self.interval_timer[button.name].clear()
            else:
                self.interval_timer[button.name] = Timer(interval).clear()

    _image_file = ''

    @property
    def image_file(self):
        return self._image_file

    @image_file.setter
    def image_file(self, value):
        """
        Загружает тестовое изображение из локального файла, используется для разработки и отладки.

        Загружает изображение в self.device.image, позволяя тестировать логику распознавания
        изображений без подключения к эмулятору.
        """
        if isinstance(value, Image.Image):
            value = np.array(value)
        elif isinstance(value, str):
            value = load_image(value)

        width, height = image_size(value)
        set_template_match_non_native_720p(width != 1280 or height != 720, resolution=(width, height))
        self.device.image = value

    def set_server(self, server):
        """
        Переключает игровой сервер глобально (используется только для разработки и отладки).

        После переключения влияет на пути к файлам ресурсов и диспетчеризацию специфичных для сервера методов.
        """
        package = to_package(server)
        self.device.package = package
        set_server(server)
        logger.attr('Сервер', self.config.SERVER)
