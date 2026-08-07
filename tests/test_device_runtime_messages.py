from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class DeviceRuntimeMessageLocalizationTests(unittest.TestCase):
    def test_translated_baseline_runtime_messages_are_present(self):
        expected = {
            "module/device/connection.py": [
                "Истекло время ожидания подключения к reverse-серверу ADB",
            ],
            "module/device/device.py": [
                "[Устройство — комиссии] Появилась ночная комиссия",
            ],
            "module/device/method/adb.py": [
                "Пустые или неполные данные screencap",
                "Пустые данные изображения от screencap",
                "Пустые данные снимка экрана в __load_screenshot",
                "Пустое изображение после cv2.imdecode",
                "Пустое изображение после cv2.cvtColor",
            ],
            "module/device/method/ascreencap.py": [
                "Не удалось установить указатель байтов: получены повреждённые данные aScreenCap",
                "aScreenCap вернул неполные или пустые данные",
                "Не удалось проверить заголовок aScreenCap: получено повреждённое изображение.",
                "Пустые распакованные данные от aScreenCap",
                "Пустое изображение после cv2.flip",
                "Не удалось загрузить снимок экрана",
            ],
            "module/device/method/droidcast.py": [
                "Сервер DroidCast не поддерживает /preview",
                "Запрошены снимки через `DroidCast`, но сервер работает как `DroidCast_raw`",
                "Пустые данные изображения от DroidCast_raw",
                "Запрошены снимки через `DroidCast_raw`, но сервер работает как `DroidCast`",
                "Если разрешение эмулятора отличается от 1280x720, установите разрешение 1280x720",
            ],
            "module/device/method/ldopengl.py": [
                "для ldopengl требуется LDPlayer >= 9.0.78. Проверьте версию",
                "но не может быть загружен",
            ],
            "module/device/method/maatouch.py": [
                "Получено слишком много некорректных ответов синхронизации",
            ],
            "module/device/method/nemu_ipc.py": [
                "Для NemuIpc требуется MuMu12 версии >= 3.8.13. Проверьте версию",
                "Ни один из следующих путей не существует",
            ],
            "module/device/method/uiautomator_2.py": [
                "Пустые данные изображения от uiautomator2",
                "Пустое изображение после чтения из буфера",
                "Пустое изображение после cv2.imdecode",
                "Пустое изображение после cv2.cvtColor",
            ],
            "module/device/platform/platform_windows.py": [
                "Не удалось запустить неизвестный экземпляр эмулятора",
                "Не удалось остановить неизвестный экземпляр эмулятора",
            ],
            "module/device/screenshot.py": [
                "Разрешение экрана не поддерживается",
                "Текущее разрешение снимка экрана — ",
                "проект поддерживает только 1280x720.",
                "Надёжное распознавание игрового интерфейса невозможно; задача будет остановлена.",
                "Установите разрешение эмулятора и окна игры 1280x720, затем повторно подключите устройство.",
            ],
        }

        for path, messages in expected.items():
            text = source(path)
            for message in messages:
                with self.subTest(path=path, message=message):
                    self.assertIn(message, text)

    def test_replaced_baseline_runtime_messages_are_absent(self):
        forbidden = {
            "module/device/connection.py": [
                "reverse server accept timeout",
            ],
            "module/device/device.py": [
                "[设备-委托] 夜间委托出现",
            ],
            "module/device/method/adb.py": [
                "Empty or incomplete screencap data",
                "Empty image data from screencap",
                "Empty screenshot payload in __load_screenshot",
                "Empty image after reading from buffer",
                "Empty image after cv2.imdecode",
                "Empty image after cv2.cvtColor",
            ],
            "module/device/method/ascreencap.py": [
                "Repositioning byte pointer failed, corrupted aScreenCap data received",
                "aScreenCap returned incomplete data or empty payload",
                "aScreenCap header verification failure, corrupted image received.",
                "Empty uncompressed data from aScreenCap",
                "Empty image after cv2.flip",
                "cannot load screenshot",
            ],
            "module/device/method/droidcast.py": [
                "DroidCast server does not have /preview",
                "Requesting screenshots from `DroidCast` but server is `DroidCast_raw`",
                "Empty image content from DroidCast_raw",
                "Requesting screenshots from `DroidCast_raw` but server is `DroidCast`",
                "If your emulator resolution not 1280x720, please set emulator resolution to 1280x720",
            ],
            "module/device/method/ldopengl.py": [
                "does not exist, ldopengl requires LDPlayer >= 9.0.78",
                "exist, but cannot be loaded",
            ],
            "module/device/method/maatouch.py": [
                "Too many incorrect sync response",
            ],
            "module/device/method/nemu_ipc.py": [
                "NemuIpc requires MuMu12 version >= 3.8.13",
            ],
            "module/device/method/uiautomator_2.py": [
                "Empty image content from uiautomator2",
                "Empty image after reading from buffer",
                "Empty image after cv2.imdecode",
                "Empty image after cv2.cvtColor",
            ],
            "module/device/platform/platform_windows.py": [
                "Cannot start an unknown emulator instance",
                "Cannot stop an unknown emulator instance",
            ],
            "module/device/screenshot.py": [
                "设备分辨率不受支持",
                "当前截图分辨率为 {width}x{height}，项目只支持 1280x720。",
                "无法可靠识别游戏界面，任务将停止。",
                "将模拟器和游戏窗口调整为 1280x720 后重新连接设备。",
            ],
        }

        for path, messages in forbidden.items():
            text = source(path)
            for message in messages:
                with self.subTest(path=path, message=message):
                    self.assertNotIn(message, text)

    def test_technical_and_raw_contract_tokens_are_preserved(self):
        connection = source("module/device/connection.py")
        minitouch = source("module/device/method/minitouch.py")
        scrcpy = source("module/device/method/scrcpy/core.py")
        nemu = source("module/device/method/nemu_ipc.py")
        uia = source("module/device/method/uiautomator_2.py")
        screenshot = source("module/device/screenshot.py")

        self.assertIn("logger.attr('MuMu Pro', True)", connection)
        self.assertIn("/minitouch", minitouch)
        self.assertIn("ScrcpyError('Aborted')", scrcpy)

        for token in ("NemuIpc", "MuMu12", ">= 3.8.13", "nemu_capture_display"):
            with self.subTest(token=token):
                self.assertIn(token, nemu)
        self.assertIn("raise NemuIpcError(self.stderr)", nemu)
        self.assertIn("b'cannot find rpc connection'", nemu)

        self.assertIn("logger.attr('Размер экрана', f'{width}x{height}')", uia)
        self.assertIn("logger.attr('Разрешение экрана', f'{width}x{height}')", screenshot)

    def test_screenshot_backend_registry_is_unchanged(self):
        text = source("module/device/screenshot.py")
        backends = (
            "ADB",
            "ADB_nc",
            "uiautomator2",
            "aScreenCap",
            "aScreenCap_nc",
            "DroidCast",
            "DroidCast_raw",
            "scrcpy",
            "nemu_ipc",
            "ldopengl",
        )
        for backend in backends:
            with self.subTest(backend=backend):
                self.assertIn(f"'{backend}':", text)


if __name__ == "__main__":
    unittest.main()
