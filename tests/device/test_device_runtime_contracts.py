
from __future__ import annotations
from tests.support.paths import REPOSITORY_ROOT


import ast
import importlib
import re
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock


ROOT = REPOSITORY_ROOT


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


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
        pinned_dependencies = ("adbutils", "uiautomator2")

        for name in pinned_dependencies:
            with self.subTest(name=name):
                specifier = _direct_dependency_spec(name)
                version = _pinned_version(specifier)
                self.assertEqual(version, locked_versions[name])

        for name, expected_major in {"adbutils": "2", "uiautomator2": "3"}.items():
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

    def test_device_dependencies_import_without_project_compatibility_layer(self) -> None:
        importlib.import_module("adbutils")
        importlib.import_module("uiautomator2")

        self.assertFalse((ROOT / "module/device/pkg_resources/__init__.py").exists())

    def test_minitouch_cleanup_closes_and_clears_all_transport_resources(self) -> None:
        from module.device.method.minitouch import Minitouch

        class Closable:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        socket_file = Closable()
        client = Closable()
        process = Closable()
        device = SimpleNamespace(
            _minitouch_socket_file=socket_file,
            _minitouch_client=client,
            _minitouch_process=process,
        )

        Minitouch._close_minitouch_transport(device)

        self.assertTrue(socket_file.closed)
        self.assertTrue(client.closed)
        self.assertTrue(process.closed)
        self.assertIsNone(device._minitouch_socket_file)
        self.assertIsNone(device._minitouch_client)
        self.assertIsNone(device._minitouch_process)

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

    def test_uiautomator2_missing_package_maps_to_package_not_installed(self) -> None:
        import uiautomator2 as u2

        from module.device.method.uiautomator_2 import Uiautomator2
        from module.device.method.utils import PackageNotInstalled

        device = Mock()
        device.package = "com.example.missing"
        device.u2.app_info.side_effect = u2.AppNotFoundError("App not installed")

        with self.assertRaises(PackageNotInstalled):
            Uiautomator2._app_start_u2_am.__wrapped__(device)

    def test_uiautomator2_recovery_reinitializes_cached_local_server(self) -> None:
        from module.device.connection import Connection

        device = Mock()
        device.is_over_http = False
        device.u2 = Mock()

        Connection.install_uiautomator2(device)

        device.u2.reset_uiautomator.assert_called_once_with()
        device.uninstall_minicap.assert_called_once_with()

    def test_screenshot_pipeline_keeps_rgb_and_backend_fallback_contracts(self) -> None:
        screenshot = _text("module/device/screenshot.py")
        acceptance = _text("tools/acceptance/device.py")

        self.assertIn("screenshot_methods", screenshot)
        self.assertIn("screenshot_method_override", screenshot)
        self.assertIn("def _handle_orientated_image", screenshot)
        self.assertIn("cv2.rotate", screenshot)
        self.assertIn('"color_contract": "RGB"', acceptance)
        self.assertIn("_validate_rgb_image", acceptance)


if __name__ == "__main__":
    unittest.main()
