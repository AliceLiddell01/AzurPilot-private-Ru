"""Модуль панели внутриигровых настроек. Определяет класс Setting, инкапсулирующий
логику переключения опций на панели настроек игры, поддерживает управление несколькими
группами параметров и отслеживание их состояния."""

import copy
import typing as t

from module.base.base import ModuleBase
from module.base.button import Button, ButtonGrid
from module.base.timer import Timer
from module.config.utils import dict_to_kv
from module.exception import ScriptError
from module.logger import logger


class Setting:
    def __init__(self, name='Setting', main: ModuleBase = None):
        self.name = name
        # Объект модуля Alas
        self.main: ModuleBase = main
        # Перед настройкой параметров сначала сбрасывать значения по умолчанию
        self.reset_first = True
        # Нужно ли снимать выбор с уже активных параметров
        self.need_deselect = False
        # (имя настройки, имя параметра): кнопка параметра
        # {
        #     ('sort', 'rarity'): Button(),
        #     ('sort', 'level'): Button(),
        #     ('sort', 'total'): Button(),
        # }
        self.settings: t.Dict[(str, str), Button] = {}
        # Имя настройки: имя параметра
        # {
        #     'sort': 'rarity',
        #     'index': 'all',
        # }
        self.settings_default: t.Dict[str, str] = {}

    def add_setting(self, setting, option_buttons, option_names, option_default):
        """
        Добавить группу параметров настроек.

        Args:
            setting (str): Имя настройки.
            option_buttons (list[Button], ButtonGrid): Список кнопок опций (может быть сгенерирован ButtonGrid.buttons).
            option_names (list[str]): Имена каждой опции; длина должна совпадать с option_buttons.
            option_default (str): Имя опции по умолчанию; должно присутствовать в option_names.
        """
        if isinstance(option_buttons, ButtonGrid):
            option_buttons = option_buttons.buttons
        for option, option_name in zip(option_buttons, option_names):
            if option_name == 'not_available':
                continue
            self.settings[(setting, option_name)] = option

        if option_default not in option_names:
            raise ScriptError(f'Не удалось задать option_default="{option_default}": '
                              f'значение отсутствует в option_names={option_names}')
        self.settings_default[setting] = option_default

    def is_option_active(self, option: Button) -> bool:
        return self.main.image_color_count(option, color=(181, 142, 90), threshold=235, count=250) \
               or self.main.image_color_count(option, color=(74, 117, 189), threshold=235, count=250)

    def _product_setting_status(self, **kwargs) -> t.Dict[Button, bool]:
        """
        Сформировать целевое состояние активности для каждой кнопки опции.

        Args:
            **kwargs: Ключ — имя настройки, значение — требуемая опция или список опций.
                Например, `sort=['rarity', 'level']` или `sort='rarity'`.
                `sort=None` означает, что настройка не изменяется.

        Returns:
            dict: Ключ — кнопка опции, значение — должна ли она быть активна.
        """
        # Add defaults
        required_options = copy.deepcopy(self.settings_default)
        required_options.update(kwargs)

        # option_button: Whether should be active
        # {BUTTON_1: True, BUTTON_2: False, ...}
        status: t.Dict[Button, bool] = {}
        for key, option_button in self.settings.items():
            setting, option_name = key
            required = required_options[setting]
            if required is not None:
                required = required if isinstance(required, list) else [required]
                status[option_button] = option_name in required

        return status

    def show_active_buttons(self):
        """
        Записать в журнал текущие активные кнопки опций.

        Logs:
            [Setting] sort/rarity, sort/level
        """
        active = []
        for key, option_button in self.settings.items():
            setting, option_name = key
            if self.is_option_active(option_button):
                active.append(f'{setting}/{option_name}')

        logger.attr(self.name, ', '.join(active))

    def get_buttons_to_click(self, status: t.Dict[Button, bool]) -> t.List[Button]:
        """
        Вычислить список кнопок для клика на основе целевого состояния.

        Args:
            status: Ключ — кнопка опции, значение — должна ли она быть активна.

        Returns:
            list[Button]: Список кнопок, по которым необходимо кликнуть.
        """
        click = []
        for option_button, enable in status.items():
            active = self.is_option_active(option_button)
            if enable and not active:
                click.append(option_button)
            if self.need_deselect:
                if not enable and active:
                    click.append(option_button)
        return click

    def _set_execute(self, **kwargs):
        """
        Выполнить переключение параметров настроек с механизмом тайм-аута и повторов.

        Args:
            **kwargs: Ключ — имя настройки, значение — требуемая опция или список опций.
                Например, `sort=['rarity', 'level']` или `sort='rarity'`.
                `sort=None` означает, что настройка не изменяется.

        Returns:
            bool: Успешно ли применены настройки.
        """
        status = self._product_setting_status(**kwargs)

        logger.info(f'[UI — Настройки] {self.name}: установка параметров {dict_to_kv(kwargs)}')
        skip_first_screenshot = True
        retry = Timer(1, count=2)
        timeout = Timer(10, count=20).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.main.device.screenshot()

            if timeout.reached():
                logger.warning(f'[UI] Превышено время ожидания установки параметров {self.name}; '
                               f'считаем, что текущие значения уже корректны.')
                return False

            self.show_active_buttons()
            clicks = self.get_buttons_to_click(status)
            if clicks:
                if retry.reached():
                    for button in clicks:
                        self.main.device.click(button)
                    retry.reset()
            else:
                return True

    def set(self, **kwargs):
        """
        Установить параметры настроек; если reset_first=True, предварительно сбрасывает в значения по умолчанию.

        Args:
            **kwargs: Ключ — имя настройки, значение — требуемая опция или список опций.
                Например, `sort=['rarity', 'level']` или `sort='rarity'`.
                `sort=None` означает, что настройка не изменяется.

        Returns:
            bool: Успешно ли применены настройки.
        """
        if self.reset_first:
            self._set_execute()  # Reset options
        self._set_execute(**kwargs)
