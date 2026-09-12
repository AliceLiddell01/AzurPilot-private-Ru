
from __future__ import annotations

import ast
import importlib
import re
import tomllib
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


def _dependency_name(specifier: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", specifier)
    if match is None:
        raise AssertionError(f"Не удалось разобрать dependency specifier: {specifier!r}")
    return match.group(0).lower().replace("_", "-")


def _direct_dependency_spec(name: str) -> str:
    dependencies = tomllib.loads(_text("pyproject.toml"))["project"]["dependencies"]
    normalized_name = name.lower().replace("_", "-")
    for specifier in dependencies:
        if _dependency_name(specifier) == normalized_name:
            return specifier
    raise AssertionError(f"{name} не является direct dependency проекта")


def _pinned_version(specifier: str) -> str:
    match = re.search(r"==\s*([0-9][^;\s,]*)", specifier)
    if match is None:
        raise AssertionError(f"Dependency не закреплена exact pin: {specifier!r}")
    return match.group(1)


def _locked_versions() -> dict[str, str]:
    packages = tomllib.loads(_text("uv.lock"))["package"]
    return {
        package["name"].lower().replace("_", "-"): package["version"]
        for package in packages
    }


class DeviceRuntimeContractTests(unittest.TestCase):
    def test_external_device_dependencies_remain_pinned(self) -> None:
        locked_versions = _locked_versions()
        pinned_dependencies = ("adbutils", "uiautomator2", "uiautomator2cache")

        for name in pinned_dependencies:
            with self.subTest(name=name):
                specifier = _direct_dependency_spec(name)
                version = _pinned_version(specifier)
                self.assertEqual(version, locked_versions[name])

        for name, expected_major in {"adbutils": "1", "uiautomator2": "2"}.items():
            with self.subTest(compatibility_major=name):
                version = _pinned_version(_direct_dependency_spec(name))
                self.assertEqual(version.split(".", 1)[0], expected_major)

    def test_direct_zmq_usage_retains_direct_pyzmq_dependency(self) -> None:
        source = _text("module/ocr/rpc.py")
        tree = ast.parse(source, filename="module/ocr/rpc.py")
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }

        self.assertIn("zmq", imported_modules)
        self.assertIn("zmq.error.ZMQError", source)
        specifier = _direct_dependency_spec("pyzmq")
        self.assertEqual(_pinned_version(specifier), _locked_versions()["pyzmq"])

        zmq = importlib.import_module("zmq")
        self.assertTrue(zmq.__version__)

    def test_pkg_resources_shim_uses_installed_metadata(self) -> None:
        from importlib import metadata

        from module.device.pkg_resources import get_distribution, resource_filename

        for name in ("adbutils", "uiautomator2"):
            with self.subTest(name=name):
                self.assertEqual(
                    get_distribution(name).version,
                    metadata.version(name),
                )

        resource_path = resource_filename("adbutils", "binaries")
        self.assertIsNotNone(resource_path)
        self.assertTrue(Path(resource_path).is_dir())

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
