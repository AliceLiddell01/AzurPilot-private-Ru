"""Модуль текстового ввода устройства.

Инкапсулирует функции ввода текста на устройствах Android, включая определение
состояния окна IME и отправку текстовых команд и действий подтверждения
компонентам Android через uiautomator2.
"""
# Этот файл предназначен для обработки текстового ввода на устройстве.
# Он инкапсулирует проверку состояния окна IME и отправку текстовых команд Android-компонентам.
from module.device.method.uiautomator_2 import Uiautomator2
from module.logger import logger


class Input(Uiautomator2):
    """Обработчик текстового ввода устройства.

    Реализует функции ввода текста через uiautomator2, включая проверку состояния
    метода ввода (IME) и ввод текста с действием подтверждения. Наследует Uiautomator2
    для доступа к низкоуровневым интерфейсам ввода.

    Methods:
        ime_shown: Проверяет, отображается ли окно метода ввода.
        text_input_and_confirm: Вводит текст и отправляет действие подтверждения.
    """
    def ime_shown(self) -> bool:
        """Проверяет, отображается ли в данный момент окно метода ввода (IME).

        Returns:
            bool: True, если окно ввода видимо, иначе False.
        """
        _, shown = self.u2_current_ime()
        return shown

    def text_input_and_confirm(self, text: str, clear: bool=False):
        """Отправляет текст в текущее поле ввода в фокусе и нажимает кнопку подтверждения (IME_ACTION_DONE).

        При неудаче выполняет до 3 повторных попыток для случаев, когда метод ввода временно не отвечает.

        Args:
            text (str): Вводимый текст.
            clear (bool): Очищать ли существующее содержимое поля перед вводом.
        """
        for fail_count in range(3):
            try:
                self.u2_send_keys(text=text, clear=clear)
                self.u2_send_action(6)
                break
            except EnvironmentError as e:
                if fail_count >= 2:
                    raise e
                logger.exception(str(e) + f'Повторная попытка {fail_count + 1}/3')
