from __future__ import annotations

import ast
import unittest
from pathlib import Path

from dev_tools.stage8a_evidence_policy import SCENARIO_REQUIREMENTS

ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _functions(path: str) -> set[str]:
    tree = ast.parse(_text(path), filename=path)
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class Stage8AScenarioContractTests(unittest.TestCase):
    def _assert_complete(self, category: str, covered: set[str]) -> None:
        self.assertEqual(set(SCENARIO_REQUIREMENTS[category]), covered)

    def test_adb_state_contracts(self):
        connection = _text("module/device/connection.py")
        utils = _text("module/device/method/utils.py")
        acceptance = _text("dev_tools/stage8a_device_acceptance.py")
        checks = {
            "device": "device.status == 'device'" in connection,
            "offline": "device.status == 'offline'" in connection,
            "no_device": "available.count == 0" in connection,
            "unauthorized": "device.status == 'unauthorized'" in connection,
            "unknown_host_service": "unknown host service" in utils,
            "connection_reset": "except ConnectionResetError as e:" in connection,
            "read_timeout": "raise AdbTimeout('adb read timeout')" in utils,
            "closed": "elif 'closed' in text" in utils,
            "device_not_found": "if 'not found' in text" in utils,
            "more_than_one_device": "Найдено несколько устройств" in connection,
            "wrong_serial": "Серийный идентификатор указан неверно" in connection,
            "server_unavailable": "Не удалось подключиться к службе ADB" in connection,
            "server_restart": "def adb_start_server" in connection
                and "self.adb_start_server()" in connection,
            "tcp_reconnect": "explicit_tcp_connect" in acceptance
                and "_wait_for_target_device" in acceptance,
        }
        for scenario, passed in checks.items():
            with self.subTest(scenario=scenario):
                self.assertTrue(passed)
        self._assert_complete("adb_state", set(checks))

    def test_package_detection_contracts(self):
        connection = _text("module/device/connection.py")
        checks = {
            "configured_package": "self.package = self.config.Emulator_PackageName" in connection,
            "auto_detection": "def detect_package" in connection
                and "len(packages) == 1" in connection,
            "package_absent": "len(packages) == 0" in connection,
            "multiple_known_packages": "Найдено несколько пакетов Azur Lane" in connection,
            "en_global_package": "VALID_PACKAGE" in connection
                and "VALID_CHANNEL_PACKAGE" in connection,
            "unsupported_package": "Пакет Azur Lane не найден" in connection,
            "remote_http_mode": "@Config.when(DEVICE_OVER_HTTP=True)" in connection,
        }
        for scenario, passed in checks.items():
            with self.subTest(scenario=scenario):
                self.assertTrue(passed)
        self._assert_complete("package_detection", set(checks))

    def test_emulator_lifecycle_contracts(self):
        base = _text("module/device/platform/platform_base.py")
        windows = _text("module/device/platform/platform_windows.py")
        macos = _text("module/device/platform/platform_mac.py")
        base_functions = _functions("module/device/platform/platform_base.py")
        windows_functions = _functions("module/device/platform/platform_windows.py")
        mac_functions = _functions("module/device/platform/platform_mac.py")
        checks = {
            "emulator_found": "Найден экземпляр эмулятора" in base,
            "emulator_not_found": "Экземпляр эмулятора" in base and "не найден" in base,
            "start_success": "Запуск эмулятора завершён" in windows
                and "Запуск эмулятора завершён" in macos,
            "start_timeout": "Истёк тайм-аут запуска эмулятора" in windows
                and "Истёк тайм-аут запуска эмулятора" in macos,
            "stop_success": "def emulator_stop" in windows
                and "return True" in windows,
            "stop_timeout": "timeout=30" in windows
                and "subprocess.TimeoutExpired" in windows,
            "platform_unsupported": "не поддерживает запуск эмулятора" in base,
            "dead_process": "proc.kill()" in windows and "proc.kill()" in macos,
            "command_nonzero": "returncode" in windows and "returncode" in macos,
            "remote_ssh_disabled": "EnableRemoteSSH=False" in base,
            "windows": "PlatformWindows" in windows,
            "macos": "PlatformMac" in macos,
        }
        for function in ("emulator_start", "emulator_stop", "emulator_instance"):
            self.assertIn(function, base_functions)
        for function in ("emulator_start", "emulator_stop", "emulator_start_watch"):
            self.assertIn(function, windows_functions)
            self.assertIn(function, mac_functions)
        for scenario, passed in checks.items():
            with self.subTest(scenario=scenario):
                self.assertTrue(passed)
        self._assert_complete("emulator_lifecycle", set(checks))

    def test_screenshot_backend_contracts(self):
        screenshot = _text("module/device/screenshot.py")
        backends = "\n".join(
            _text(path)
            for path in (
                "module/device/method/adb.py",
                "module/device/method/ascreencap.py",
                "module/device/method/droidcast.py",
                "module/device/method/nemu_ipc.py",
                "module/device/method/ldopengl.py",
                "module/device/method/scrcpy/core.py",
            )
        )
        checks = {
            "init_success": "screenshot_methods" in screenshot,
            "init_failure": "RequestHumanTakeover" in backends,
            "first_frame": "def screenshot" in screenshot,
            "timeout": "timeout" in backends or "JobTimeout" in backends,
            "truncated_frame": "ImageTruncated" in backends,
            "empty_frame": "Empty" in backends,
            "black_frame": "def check_screen_black" in screenshot,
            "invalid_size": "def check_screen_size" in screenshot,
            "rotated_frame": "def _handle_orientated_image" in screenshot
                and "cv2.rotate" in screenshot,
            "stream_close": ".close()" in backends,
            "fallback": "screenshot_method_override" in screenshot
                and "screenshot_methods" in screenshot,
        }
        for scenario, passed in checks.items():
            with self.subTest(scenario=scenario):
                self.assertTrue(passed)
        self._assert_complete("screenshot_backend", set(checks))

    def test_input_backend_contracts(self):
        control = _text("module/device/control.py")
        input_source = _text("module/device/input.py")
        minitouch = _text("module/device/method/minitouch.py")
        maatouch = _text("module/device/method/maatouch.py")
        scrcpy_control = _text("module/device/method/scrcpy/control.py")
        combined = "\n".join((control, input_source, minitouch, maatouch, scrcpy_control))
        checks = {
            "click": "def click" in control,
            "swipe": "def swipe" in control,
            "key": "def keycode" in scrcpy_control,
            "text": "def text_input_and_confirm" in input_source
                and "def text(" in scrcpy_control,
            "empty_command": "self.commands = []" in minitouch
                and "for command in self.commands" in minitouch,
            "invalid_orientation": "Недопустимая ориентация устройства" in minitouch,
            "socket_close": ".close()" in combined,
            "backend_unavailable": "MinitouchNotInstalledError" in minitouch
                and "MaaTouchNotInstalledError" in maatouch,
            "timeout": "socket.timeout" in minitouch
                and "socket.timeout" in maatouch,
            "reconnect": "adb_reconnect" in minitouch and "adb_reconnect" in maatouch,
            "fallback": "Emulator_ControlMethod" in control,
        }
        for scenario, passed in checks.items():
            with self.subTest(scenario=scenario):
                self.assertTrue(passed)
        self._assert_complete("input_backend", set(checks))

    def test_scrcpy_contracts(self):
        core = _text("module/device/method/scrcpy/core.py")
        options = _text("module/device/method/scrcpy/options.py")
        control = _text("module/device/method/scrcpy/control.py")
        webui = _text("module/webui/api.py")
        checks = {
            "server_push": "adb_push" in core,
            "server_startup": "app_process" in options,
            "video_stream": "_scrcpy_video_socket" in core,
            "control_stream": "_scrcpy_control_socket" in core,
            "initial_metadata": "device_name" in core and "resolution" in core,
            "stream_close": "def _scrcpy_server_stop" in core and ".close()" in core,
            "version_mismatch": "Версия сервера не соответствует версии клиента" in core,
            "fallback": "_ws_live_screenshot_fallback" in webui,
            "live_preview": "_ws_live_scrcpy" in webui,
            "control_error": "ScrcpyError" in core,
            "device_messages": "_scrcpy_receive_from_server_stream" in core
                and "get_clipboard" in control,
        }
        for scenario, passed in checks.items():
            with self.subTest(scenario=scenario):
                self.assertTrue(passed)
        self._assert_complete("scrcpy", set(checks))

    def test_uiautomator2_timeout_contracts(self):
        connection = _text("module/device/connection_attr.py")
        uia = _text("module/device/method/uiautomator_2.py")
        external = _text(".codex/reviews/PR20_STAGE8A_EXTERNAL_CONTRACTS.md")
        checks = {
            "implicit_wait": "implicit wait" in external.lower(),
            "http_timeout": "self.u2.http.post" in uia and "timeout=" in uia,
            "click_long_click": "def click_uiautomator2" in uia
                and "def long_click_uiautomator2" in uia,
            "drag_swipe": "def drag_uiautomator2" in uia
                and "def swipe_uiautomator2" in uia,
            "text_input": "def u2_send_keys" in uia,
            "xpath_wait_get": "XPath" in external and "wait/get" in external,
            "service_initialization": "u2.connect(self.serial)" in connection
                and "set_new_command_timeout(604800)" in connection,
        }
        for scenario, passed in checks.items():
            with self.subTest(scenario=scenario):
                self.assertTrue(passed)
        self._assert_complete("uiautomator2_timeout", set(checks))

    def test_webui_live_control_contracts(self):
        source = _text("module/webui/api.py")
        checks = {
            "start": "async def ws_live_screenshot" in source
                and "async def ws_live_control" in source,
            "stop": "LiveScrcpySession.release" in source,
            "fallback": "_ws_live_screenshot_fallback" in source
                and "LiveControlDevice" in source,
            "resolution": "resolution" in source,
            "prebuffer": "_collect_h264_preroll" in source
                and "_collect_ws_scrcpy_preroll" in source,
            "click": 'action == "tap"' in source,
            "drag": 'action == "drag"' in source,
            "key": 'action == "key"' in source,
            "text": 'action == "text"' in source,
            "back": 'action == "back"' in source,
            "system_key": "CONTROL_ACTION_KEYCODES" in source,
            "socket_close": "WebSocketDisconnect" in source,
            "resource_cleanup": "finally:" in source and ".close()" in source,
            "no_user_text_leak": "Управление предпросмотром: ввод текста" in source
                and "data.get(\"text\"" in source,
        }
        for scenario, passed in checks.items():
            with self.subTest(scenario=scenario):
                self.assertTrue(passed)
        self._assert_complete("webui_live_control", set(checks))


if __name__ == "__main__":
    unittest.main()
