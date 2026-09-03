"""设备文本输入模块。

封装 Android 设备端的文本输入功能，包括输入法窗口状态检测
以及通过 uiautomator2 向安卓组件发送文本指令和确认操作。
"""
# 此文件专门用于处理设备端的文本输入功能。
# 封装了检查输入法窗口状态以及向安卓组件发送文本指令的逻辑。
import re

from module.device.method.uiautomator_2 import Uiautomator2
from module.logger import logger


class Input(Uiautomator2):
    """设备文本输入处理器。

    通过 uiautomator2 实现文本输入功能，包括输入法状态检测
    和带确认操作的文本输入。继承自 Uiautomator2 以获取底层输入接口。

    Methods:
        ime_shown: 检测输入法窗口是否显示。
        text_input_and_confirm: 输入文本并发送确认动作。
    """

    _ADB_TEXT_PATTERN = re.compile(r"[A-Za-z0-9 .,'_\-]+")
    _ADB_CLEAR_KEY_COUNT = 96

    def ime_shown(self) -> bool:
        """检测当前输入法（IME）窗口是否正在显示。

        Returns:
            bool: 输入法窗口可见返回 True，否则返回 False。
        """
        _, shown = self.u2_current_ime()
        return shown

    @classmethod
    def _adb_text_supported(cls, text: str) -> bool:
        """ADB ``input text`` используется только для безопасного ASCII subset."""

        return (
            isinstance(text, str)
            and bool(text)
            and cls._ADB_TEXT_PATTERN.fullmatch(text) is not None
        )

    @staticmethod
    def _adb_text_payload(text: str) -> str:
        # Android ``input text`` интерпретирует %s как пробел.
        return text.replace(" ", "%s")

    def _adb_text_input_and_confirm(self, text: str, *, clear: bool) -> None:
        """Ввести ASCII-текст через Android ``input`` без FastInputIME/u2 JSON-RPC."""

        if not self._adb_text_supported(text):
            raise ValueError("ADB fallback не поддерживает этот текст")
        if clear:
            self.adb_shell(["input", "keyevent", "KEYCODE_MOVE_END"])
            self.adb_shell(
                [
                    "input",
                    "keyevent",
                    *(["KEYCODE_DEL"] * self._ADB_CLEAR_KEY_COUNT),
                ]
            )
        self.adb_shell(["input", "text", self._adb_text_payload(text)])
        self.adb_shell(["input", "keyevent", "KEYCODE_ENTER"])

    def text_input_and_confirm(self, text: str, clear: bool = False):
        """向当前焦点输入框发送文本并按确认键（IME_ACTION_DONE）。

        Основной путь остаётся uiautomator2. Если FastInputIME/JSON-RPC ломается,
        для безопасного ASCII используется ADB ``input`` fallback. После первого
        успешного fallback последующие ASCII-запросы этого Device сразу идут по
        ADB, чтобы не перезапускать сломанный uiautomator на каждом Search lookup.

        Args:
            text (str): 要输入的文本内容。
            clear (bool): 输入前是否清空输入框已有内容。
        """
        if not isinstance(text, str) or not text:
            raise ValueError("text должен быть непустой строкой")

        if getattr(self, "_text_input_prefer_adb", False) and self._adb_text_supported(text):
            try:
                self._adb_text_input_and_confirm(text, clear=clear)
                return
            except Exception as exc:
                self._text_input_prefer_adb = False
                logger.exception(
                    f"[Устройство — ввод] ADB text fallback перестал работать: {exc}"
                )

        last_error: Exception | None = None
        for fail_count in range(3):
            try:
                self.u2_send_keys(text=text, clear=clear)
                self.u2_send_action(6)
                return
            except Exception as exc:
                last_error = exc
                if self._adb_text_supported(text):
                    try:
                        self._adb_text_input_and_confirm(text, clear=clear)
                    except Exception as adb_exc:
                        logger.exception(
                            "[Устройство — ввод] ADB fallback после ошибки "
                            f"uiautomator2 тоже не сработал: {adb_exc}"
                        )
                    else:
                        self._text_input_prefer_adb = True
                        logger.warning(
                            "[Устройство — ввод] uiautomator2 text input недоступен; "
                            "использован ADB input fallback"
                        )
                        return
                if fail_count >= 2:
                    raise
                logger.exception(
                    str(exc) + f" Повторная попытка {fail_count + 1}/3"
                )

        assert last_error is not None
        raise last_error
