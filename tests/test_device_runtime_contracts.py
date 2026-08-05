
from __future__ import annotations

import ast
import unittest
from pathlib import Path


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


class DeviceRuntimeContractTests(unittest.TestCase):
    def test_external_device_dependencies_remain_pinned(self) -> None:
        pyproject = _text("pyproject.toml")
        lock = _text("uv.lock")

        self.assertIn('"adbutils==0.11.0"', pyproject)
        self.assertIn('"uiautomator2==2.16.17"', pyproject)
        self.assertRegex(lock, r'name = "adbutils"\s+version = "0\.11\.0"')
        self.assertRegex(lock, r'name = "uiautomator2"\s+version = "2\.16\.17"')

    def test_adb_target_and_android_readiness_are_explicit(self) -> None:
        connection_attr = _text("module/device/connection_attr.py")
        acceptance = _text("tools/acceptance/device.py")

        self.assertIn("AdbDevice(self.adb_client, self.serial)", connection_attr)
        self.assertIn('[adb, "-s", serial, *args]', acceptance)
        self.assertIn('"sys.boot_completed"', acceptance)
        self.assertIn("explicit_tcp_connect", acceptance)
        self.assertIn("_wait_for_target_device", acceptance)

    def test_scrcpy_keeps_separate_video_and_control_streams(self) -> None:
        core = _text("module/device/method/scrcpy/core.py")
        options = _text("module/device/method/scrcpy/options.py")
        control = _text("module/device/method/scrcpy/control.py")

        self.assertIn("_scrcpy_video_socket", core)
        self.assertIn("_scrcpy_control_socket", core)
        self.assertIn("device_name", core)
        self.assertIn("resolution", core)
        self.assertIn("command_v120", options)
        self.assertIn("def keycode", control)
        self.assertIn("def text", control)

    def test_uiautomator2_keeps_connection_and_operation_timeout_layers(self) -> None:
        connection_attr = _text("module/device/connection_attr.py")
        uia = _text("module/device/method/uiautomator_2.py")
        functions = _functions("module/device/method/uiautomator_2.py")

        self.assertIn("u2.connect(self.serial)", connection_attr)
        self.assertIn("set_new_command_timeout(604800)", connection_attr)
        self.assertIn("self.u2.http.post", uia)
        self.assertIn("timeout=", uia)
        for name in (
            "click_uiautomator2",
            "long_click_uiautomator2",
            "swipe_uiautomator2",
            "drag_uiautomator2",
            "u2_send_keys",
        ):
            with self.subTest(name=name):
                self.assertIn(name, functions)

    def test_screenshot_pipeline_keeps_bgr_and_backend_fallback_contracts(self) -> None:
        screenshot = _text("module/device/screenshot.py")
        acceptance = _text("tools/acceptance/device.py")

        self.assertIn("screenshot_methods", screenshot)
        self.assertIn("screenshot_method_override", screenshot)
        self.assertIn("def _handle_orientated_image", screenshot)
        self.assertIn("cv2.rotate", screenshot)
        self.assertIn('"color_contract": "BGR"', acceptance)
        self.assertIn("_validate_bgr_image", acceptance)


if __name__ == "__main__":
    unittest.main()
