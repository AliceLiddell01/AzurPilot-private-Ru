from __future__ import annotations

import asyncio
import json
import socket
import struct
import subprocess
import sys
import unittest
from json.decoder import JSONDecodeError
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

if sys.platform != "win32":
    sys.modules.setdefault("winreg", MagicMock())

import cv2
import numpy as np
from adbutils import AdbTimeout
from adbutils.errors import AdbError

from dev_tools import stage8a_device_acceptance as acceptance
from module.device.connection import Connection, retry as connection_retry
from module.device.method.ldopengl import (
    DataLDPlayerInfo,
    LDOpenGLError,
    LDOpenGLImpl,
    LDOpenGLIncompatible,
)
from module.device.method.minitouch import (
    Command,
    CommandBuilder,
    MinitouchNotInstalledError,
    retry as minitouch_retry,
)
from module.device.method.nemu_ipc import (
    NemuIpcError,
    NemuIpcImpl,
    retry as nemu_retry,
)
from module.device.method.scrcpy import const as scrcpy_const
from module.device.method.scrcpy.control import ControlSender
from module.device.method.scrcpy.core import ScrcpyCore, ScrcpyError
from module.device.method.uiautomator_2 import (
    Uiautomator2,
    retry as uiautomator2_retry,
)
from module.device.method.utils import handle_adb_error, handle_unknown_host_service, recv_all
from module.device.platform.platform_base import PlatformBase
from module.device.platform.platform_mac import PlatformMac
from module.device.platform.platform_windows import PlatformWindows
from module.device.screenshot import Screenshot
from module.exception import EmulatorNotRunningError, RequestHumanTakeover, ScriptError
from module.map.map_grids import SelectedGrids
from module.webui import api as webui_api


class _DeviceRow:
    def __init__(self, serial: str, status: str, *, port: int = 0, may_mumu12_family: bool = False):
        self.serial = serial
        self.status = status
        self.port = port
        self.may_mumu12_family = may_mumu12_family

    def __repr__(self) -> str:
        return f"_DeviceRow({self.serial!r}, {self.status!r})"


class _FakeSocket:
    def __init__(self, recv_values=()):
        self.recv_values = list(recv_values)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout = None
        self.blocking = True

    def recv(self, _size: int) -> bytes:
        if not self.recv_values:
            return b""
        value = self.recv_values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True

    def settimeout(self, value) -> None:
        self.timeout = value

    def setblocking(self, value: bool) -> None:
        self.blocking = value


class _AsyncWebSocket:
    def __init__(self, messages: list[str]):
        self.messages = list(messages)
        self.query_params = {"instance": "alas"}
        self.sent: list[str] = []
        self.accepted = False
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self.messages:
            return self.messages.pop(0)
        raise webui_api.WebSocketDisconnect()

    async def send_text(self, value: str):
        self.sent.append(value)

    async def close(self, *_args, **_kwargs):
        self.closed = True


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _connection_stub(*, serial="127.0.0.1:5555", http=False):
    obj = Connection.__new__(Connection)
    obj.config = SimpleNamespace(
        DEVICE_OVER_HTTP=http,
        Emulator_Serial=serial,
        Emulator_AdbRestart=False,
        Emulator_PackageName="auto",
    )
    obj.serial = serial
    obj.is_mumu12_family = False
    obj.port = 5555
    obj.list_device = MagicMock(return_value=SelectedGrids([]))
    obj.adb_client = MagicMock()
    obj.adb = MagicMock()
    obj.release_resource = MagicMock()
    obj.check_mumu_bridge_network = MagicMock(return_value=True)
    obj.detect_device = MagicMock()
    return obj


def _retry_probe(error: BaseException, *, name="probe", success="ok"):
    calls = {"count": 0}

    def probe(_self):
        calls["count"] += 1
        if calls["count"] == 1:
            raise error
        return success

    probe.__name__ = name
    return probe, calls


def _test_adb_state(scenario: str) -> None:
    if scenario in {"device", "no_device", "more_than_one_device"}:
        obj = _connection_stub(serial="auto")
        obj.config.Emulator_Serial = "auto"
        obj.serial = "auto"
        if scenario == "device":
            devices = SelectedGrids([_DeviceRow("USB123", "device")])
            obj.list_device = MagicMock(return_value=devices)
            Connection.detect_device(obj)
            assert obj.serial == "USB123"
            assert obj.config.Emulator_Serial == "USB123"
            return
        if scenario == "no_device":
            obj.list_device = MagicMock(return_value=SelectedGrids([]))
        else:
            obj.list_device = MagicMock(
                return_value=SelectedGrids([
                    _DeviceRow("USB123", "device"),
                    _DeviceRow("USB456", "device"),
                ])
            )
        with patch("module.device.connection.IS_WINDOWS", False):
            with unittest.TestCase().assertRaises(RequestHumanTakeover):
                Connection.detect_device(obj)
        return

    if scenario in {"offline", "unauthorized", "wrong_serial"}:
        obj = _connection_stub()
        status = "offline" if scenario == "offline" else "unauthorized"
        obj.list_device = MagicMock(
            return_value=SelectedGrids([_DeviceRow(obj.serial, status)])
        )
        if scenario == "wrong_serial":
            obj.list_device = MagicMock(return_value=SelectedGrids([]))
            obj.adb_client.connect.return_value = "bad port number"
            with unittest.TestCase().assertRaises(RequestHumanTakeover):
                Connection.adb_connect(obj, wait_device=False)
            return
        obj.adb_client.connect.return_value = f"already connected to {obj.serial}"
        with patch("module.device.connection.logger") as logger:
            assert Connection.adb_connect(obj, wait_device=False) is True
            if scenario == "offline":
                obj.adb_client.disconnect.assert_called_once_with(obj.serial)
            else:
                assert any("не авторизовано" in str(c) for c in logger.error.call_args_list)
        return

    if scenario == "unknown_host_service":
        obj = _connection_stub()
        obj.adb_reconnect = MagicMock()
        obj.adb_start_server = MagicMock()
        probe, calls = _retry_probe(AdbError("unknown host service"))
        with patch("module.device.connection.RETRY_TRIES", 2), patch(
            "module.device.connection.retry_sleep", return_value=0
        ), patch("module.device.connection.time.sleep"):
            result = connection_retry(probe)(obj)
        assert result == "ok" and calls["count"] == 2
        obj.adb_start_server.assert_called_once_with()
        obj.adb_reconnect.assert_called_once_with()
        return

    if scenario == "connection_reset":
        obj = _connection_stub()
        obj.adb_reconnect = MagicMock()
        probe, calls = _retry_probe(ConnectionResetError("reset by peer"))
        with patch("module.device.connection.RETRY_TRIES", 2), patch(
            "module.device.connection.retry_sleep", return_value=0
        ), patch("module.device.connection.time.sleep"), patch(
            "module.device.connection.logger"
        ) as logger:
            result = connection_retry(probe)(obj)
        assert result == "ok" and calls["count"] == 2
        obj.adb_reconnect.assert_called_once_with()
        assert "reset by peer" in str(logger.error.call_args)
        return

    if scenario == "read_timeout":
        stream = MagicMock()
        stream.recv.side_effect = socket.timeout("read timed out")
        stream.settimeout = MagicMock()
        with unittest.TestCase().assertRaises(AdbTimeout):
            recv_all(stream)
        return

    if scenario == "closed":
        assert handle_adb_error(AdbError("transport closed")) is True
        return

    if scenario == "device_not_found":
        assert handle_adb_error(AdbError("device not found")) is True
        return

    if scenario == "server_unavailable":
        obj = _connection_stub()
        probe, _ = _retry_probe(AdbError("cannot connect to adb server"), name="adb_connect")
        with patch("module.device.connection.RETRY_TRIES", 1):
            with unittest.TestCase().assertRaises(EmulatorNotRunningError):
                connection_retry(probe)(obj)
        return

    if scenario == "server_restart":
        obj = _connection_stub()
        obj.config.Emulator_AdbRestart = True
        parent = MagicMock()
        obj.list_device = MagicMock(return_value=[])
        obj.adb_restart = parent.adb_restart
        obj.adb_connect = parent.adb_connect
        obj.detect_device = parent.detect_device
        Connection.adb_reconnect(obj)
        assert parent.mock_calls == [call.adb_restart(), call.adb_connect(), call.detect_device()]
        return

    if scenario == "tcp_reconnect":
        with patch.object(acceptance, "_confirm", return_value=True), patch.object(
            acceptance, "_run_adb", return_value=_completed("reconnecting\n")
        ) as run_adb, patch.object(
            acceptance, "_run_adb_connect", return_value=_completed("connected\n")
        ) as connect, patch.object(
            acceptance,
            "_wait_for_target_device",
            return_value={
                "restored": True,
                "attempts": 1,
                "last_state": {"returncode": 0, "stdout": "device", "stderr": ""},
            },
        ):
            evidence = acceptance._check_reconnect("adb", "127.0.0.1:16416", False)
        assert evidence["status"] == "PASS"
        run_adb.assert_called_once_with("adb", "127.0.0.1:16416", "reconnect", timeout=30)
        connect.assert_called_once_with("adb", "127.0.0.1:16416", timeout=30)
        return

    raise AssertionError(f"Unhandled ADB scenario: {scenario}")


def _test_device_readiness(scenario: str) -> None:
    if scenario == "adb_state_device":
        with patch.object(
            acceptance, "_run_adb", return_value=_completed("device\n")
        ) as run_adb, patch.object(
            acceptance.time, "monotonic", side_effect=[0.0, 0.0]
        ):
            evidence = acceptance._wait_for_target_device(
                "adb", "serial", timeout=1.0
            )
        assert evidence["restored"] is True
        assert evidence["attempts"] == 1
        assert evidence["last_state"]["stdout"] == "device\n"
        run_adb.assert_called_once_with("adb", "serial", "get-state", timeout=10)
        return
    if scenario == "android_boot_incomplete":
        with patch.object(
            acceptance, "_run_adb", return_value=_completed("0\n")
        ):
            with unittest.TestCase().assertRaises(acceptance.AcceptanceFailure):
                acceptance._check_android_boot_completed("adb", "serial")
        return
    if scenario == "package_unavailable":
        with patch.object(acceptance, "_run_adb", return_value=_completed("", returncode=1)):
            with unittest.TestCase().assertRaises(acceptance.AcceptanceFailure):
                acceptance._detect_package("adb", "serial", "com.YoStarEN.AzurLane")
        return
    if scenario == "screenshot_unavailable":
        with unittest.TestCase().assertRaises(acceptance.AcceptanceFailure):
            acceptance._decode_screenshot(b"not-a-png")
        return
    if scenario == "input_unavailable":
        device = MagicMock()
        device.minitouch_init.side_effect = MinitouchNotInstalledError("missing")
        with patch.object(acceptance, "_new_device", return_value=device):
            with unittest.TestCase().assertRaises(acceptance.AcceptanceFailure):
                acceptance._check_configured_control_backend("alas", "minitouch")
        return
    raise AssertionError(f"Unhandled readiness scenario: {scenario}")


def _test_package_detection(scenario: str) -> None:
    if scenario == "configured_package":
        with patch.object(
            acceptance,
            "_run_adb",
            return_value=_completed("package:/data/app/base.apk\n"),
        ) as run:
            package = acceptance._detect_package(
                "adb", "serial", "com.YoStarEN.AzurLane"
            )
        assert package == "com.YoStarEN.AzurLane"
        run.assert_called_once_with(
            "adb", "serial", "shell", "pm", "path", "com.YoStarEN.AzurLane"
        )
        return

    if scenario in {"auto_detection", "package_absent", "multiple_known_packages"}:
        from module.config.server import VALID_PACKAGE

        valid = sorted(VALID_PACKAGE)
        assert valid
        if scenario == "auto_detection":
            stdout = f"package:{valid[0]}\n"
        elif scenario == "package_absent":
            stdout = "package:example.invalid\n"
        else:
            assert len(valid) >= 2
            stdout = f"package:{valid[0]}\npackage:{valid[1]}\n"
        with patch.object(acceptance, "_run_adb", return_value=_completed(stdout)):
            if scenario == "auto_detection":
                assert acceptance._detect_package("adb", "serial", "auto") == valid[0]
            else:
                with unittest.TestCase().assertRaises(acceptance.AcceptanceFailure):
                    acceptance._detect_package("adb", "serial", "auto")
        return

    if scenario in {"en_global_package", "unsupported_package"}:
        obj = _connection_stub(serial="USB123")
        obj.config.Emulator_PackageName = "auto"
        obj.list_known_packages = MagicMock(
            return_value=(
                ["com.YoStarEN.AzurLane"] if scenario == "en_global_package" else []
            )
        )
        with patch("module.device.connection.set_server") as set_server:
            if scenario == "en_global_package":
                Connection.detect_package(obj)
                assert obj.package == "com.YoStarEN.AzurLane"
                set_server.assert_called_once_with("com.YoStarEN.AzurLane")
            else:
                with unittest.TestCase().assertRaises(RequestHumanTakeover):
                    Connection.detect_package(obj)
        return

    if scenario == "remote_http_mode":
        obj = _connection_stub(serial="http://127.0.0.1:7912", http=True)
        assert Connection.adb_connect(obj, wait_device=False) is True
        obj.adb_client.connect.assert_not_called()
        return

    raise AssertionError(f"Unhandled package scenario: {scenario}")


def _test_emulator_lifecycle(scenario: str) -> None:
    if scenario in {"emulator_found", "emulator_not_found"}:
        obj = PlatformBase.__new__(PlatformBase)
        instance = SimpleNamespace(
            serial="127.0.0.1:5555",
            name="test",
            path="/tmp/emulator",
            type="Test",
            MuMuPlayer12_id=None,
        )
        obj.all_emulator_instances = [instance] if scenario == "emulator_found" else []
        with patch("module.device.platform.platform_base.os.path.exists", return_value=False):
            found = PlatformBase.find_emulator_instance(
                obj,
                serial="127.0.0.1:5555",
            )
        if scenario == "emulator_found":
            assert found is instance
        else:
            assert found is None
        return

    if scenario in {"start_success", "start_timeout", "stop_success", "stop_timeout"}:
        obj = PlatformWindows.__new__(PlatformWindows)
        obj.config = SimpleNamespace(EmulatorInfo_Emulator="")
        obj._emulator_instance = None
        parent = MagicMock()
        obj._emulator_stop = MagicMock()
        obj._emulator_start = MagicMock()
        obj._emulator_function_wrapper = parent.wrapper
        obj.emulator_start_watch = parent.watch
        if scenario == "start_success":
            parent.wrapper.side_effect = [True, True]
            parent.watch.return_value = True
            assert PlatformWindows.emulator_start(obj) is True
        elif scenario == "start_timeout":
            parent.wrapper.side_effect = [True] * 9
            parent.watch.return_value = False
            assert PlatformWindows.emulator_start(obj) is False
            assert parent.watch.call_count == 3
        elif scenario == "stop_success":
            parent.wrapper.return_value = True
            assert PlatformWindows.emulator_stop(obj) is True
        else:
            parent.wrapper.side_effect = [False, True, False, True, False, True]
            assert PlatformWindows.emulator_stop(obj) is False
        return

    if scenario == "platform_unsupported":
        obj = PlatformBase.__new__(PlatformBase)
        with patch("module.device.platform.platform_base.logger") as logger:
            assert PlatformBase.emulator_start(obj) is None
            assert PlatformBase.emulator_stop(obj) is None
        assert logger.info.call_count == 2
        return

    if scenario == "dead_process":
        process = SimpleNamespace(pid=100, kill=MagicMock())
        with patch("module.device.platform.platform_windows.psutil.process_iter", return_value=[process]), patch(
            "module.device.platform.platform_windows.DataProcessInfo",
            return_value=SimpleNamespace(cmdline="NemuPlayer.exe"),
        ):
            assert PlatformWindows.kill_process_by_regex("NemuPlayer",) == 1
        process.kill.assert_called_once_with()
        return

    if scenario in {"command_nonzero", "windows"}:
        result = _completed(returncode=9)
        with patch("module.device.platform.platform_windows.subprocess.CREATE_NO_WINDOW", 0, create=True), patch(
            "module.device.platform.platform_windows.subprocess.run", return_value=result
        ):
            assert PlatformWindows.execute("echo test", wait=True).returncode == 9
        return

    if scenario == "remote_ssh_disabled":
        obj = PlatformBase.__new__(PlatformBase)
        obj.config = SimpleNamespace(EmulatorInfo_EnableRemoteSSH=False)
        with patch("module.device.platform.platform_base.subprocess.Popen") as popen:
            assert PlatformBase.run_remote_ssh_command(obj, "echo test") is None
        popen.assert_not_called()
        return

    if scenario == "macos":
        obj = PlatformMac.__new__(PlatformMac)
        obj.run_remote_ssh_command = MagicMock()
        obj._emulator_stop = MagicMock()
        obj._emulator_start = MagicMock()
        obj._emulator_function_wrapper = MagicMock(side_effect=[True, True])
        obj.boost_emulator_priority = MagicMock()
        obj.emulator_instance = SimpleNamespace()
        obj.emulator_start_watch = MagicMock(return_value=True)
        assert PlatformMac.emulator_start(obj) is True
        obj.run_remote_ssh_command.assert_called_once_with()
        return

    raise AssertionError(f"Unhandled emulator scenario: {scenario}")


def _screenshot_stub(image: np.ndarray | None = None):
    obj = Screenshot.__new__(Screenshot)
    obj.config = SimpleNamespace(
        Emulator_ScreenshotMethod="ADB",
        Emulator_ScreenshotDedithering=False,
        Error_SaveError=False,
        Emulator_Serial="USB123",
    )
    obj._screenshot_interval = MagicMock()
    obj.screenshot_method_override = ""
    obj.screenshot_adb = MagicMock(return_value=image)
    obj.screenshot_methods = {"ADB": obj.screenshot_adb}
    obj._handle_orientated_image = MagicMock(side_effect=lambda value: value)
    obj.check_screen_size = MagicMock(return_value=True)
    obj.check_screen_black = MagicMock(return_value=True)
    return obj


def _test_screenshot_backend(scenario: str) -> None:
    if scenario == "init_success":
        obj = Screenshot.__new__(Screenshot)
        for name in (
            "screenshot_adb", "screenshot_adb_nc", "screenshot_uiautomator2",
            "screenshot_ascreencap", "screenshot_ascreencap_nc", "screenshot_droidcast",
            "screenshot_droidcast_raw", "screenshot_scrcpy", "screenshot_nemu_ipc",
            "screenshot_ldopengl",
        ):
            setattr(obj, name, MagicMock())
        methods = Screenshot.screenshot_methods.func(obj)
        assert {"ADB", "uiautomator2", "aScreenCap", "DroidCast", "scrcpy", "nemu_ipc", "ldopengl"} <= set(methods)
        return

    if scenario in {"init_failure", "timeout", "truncated_frame", "empty_frame"}:
        errors = {
            "init_failure": RuntimeError("backend init failed"),
            "timeout": TimeoutError("frame timeout"),
            "truncated_frame": acceptance.AcceptanceFailure("PNG stream снимка экрана усечён."),
            "empty_frame": acceptance.AcceptanceFailure("empty frame"),
        }
        obj = _screenshot_stub(np.zeros((720, 1280, 3), dtype=np.uint8))
        obj.screenshot_adb.side_effect = errors[scenario]
        with unittest.TestCase().assertRaises(type(errors[scenario])):
            Screenshot.screenshot(obj)
        return

    if scenario == "first_frame":
        image = np.full((720, 1280, 3), 7, dtype=np.uint8)
        obj = _screenshot_stub(image)
        result = Screenshot.screenshot(obj)
        assert result is image
        obj.screenshot_adb.assert_called_once_with()
        return

    if scenario == "black_frame":
        obj = Screenshot.__new__(Screenshot)
        obj.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        obj._screen_black_checked = False
        obj._minicap_uninstalled = True
        obj.config = SimpleNamespace(Emulator_Serial="USB123", Emulator_ScreenshotMethod="ADB")
        obj.serial = "USB123"
        obj.is_mumu_family = False
        obj.adb_reconnect = MagicMock()
        with patch("module.device.screenshot.get_color", return_value=(0, 0, 0)):
            assert Screenshot.check_screen_black(obj) is False
        return

    if scenario == "invalid_size":
        obj = Screenshot.__new__(Screenshot)
        obj.image = np.zeros((480, 640, 3), dtype=np.uint8)
        obj._screen_size_checked = False
        obj.config = SimpleNamespace(Emulator_Serial="USB123")
        with unittest.TestCase().assertRaises(RequestHumanTakeover):
            Screenshot.check_screen_size(obj)
        return

    if scenario == "rotated_frame":
        obj = Screenshot.__new__(Screenshot)
        obj.image = np.zeros((1280, 720, 3), dtype=np.uint8)
        obj.orientation = 1
        rotated = Screenshot._handle_orientated_image(obj, obj.image)
        assert rotated.shape == (720, 1280, 3)
        return

    if scenario == "stream_close":
        obj = ScrcpyCore.__new__(ScrcpyCore)
        obj._scrcpy_alive = True
        obj._scrcpy_stream_loop_thread = None
        obj._scrcpy_control_socket = _FakeSocket()
        obj._scrcpy_video_socket = _FakeSocket()
        obj._scrcpy_server_stream = _FakeSocket()
        ScrcpyCore._scrcpy_server_stop(obj)
        assert obj._scrcpy_control_socket is None
        assert obj._scrcpy_video_socket is None
        assert obj._scrcpy_server_stream is None
        return

    if scenario == "fallback":
        session = MagicMock(alive=True, resolution=(640, 360))
        session.read_video.side_effect = [None, b""]
        first = np.zeros((720, 1280, 3), dtype=np.uint8)
        device = MagicMock()
        device.screenshot.return_value = first.copy()
        live = MagicMock()
        live.acquire.return_value = session
        with patch.object(
            acceptance,
            "_preview_dependencies",
            return_value=(live, lambda: "ffmpeg", lambda _profile: (device, first)),
        ):
            result = acceptance._check_preview("alas", "nemu_ipc")
        assert result["mode"] == "screenshot_fallback"
        assert result["frames_verified"] == 2
        return

    raise AssertionError(f"Unhandled screenshot scenario: {scenario}")



def _test_screenshot_backend_matrix(backend: str) -> None:
    method_names = {
        "adb": "screenshot_adb",
        "adb_nc": "screenshot_adb_nc",
        "uiautomator2": "screenshot_uiautomator2",
        "ascreencap": "screenshot_ascreencap",
        "ascreencap_nc": "screenshot_ascreencap_nc",
        "droidcast": "screenshot_droidcast",
        "droidcast_raw": "screenshot_droidcast_raw",
        "scrcpy": "screenshot_scrcpy",
        "nemu_ipc": "screenshot_nemu_ipc",
        "ldopengl": "screenshot_ldopengl",
    }
    config_values = {
        "adb": "ADB",
        "adb_nc": "ADB_nc",
        "uiautomator2": "uiautomator2",
        "ascreencap": "aScreenCap",
        "ascreencap_nc": "aScreenCap_nc",
        "droidcast": "DroidCast",
        "droidcast_raw": "DroidCast_raw",
        "scrcpy": "scrcpy",
        "nemu_ipc": "nemu_ipc",
        "ldopengl": "ldopengl",
    }
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    obj = Screenshot.__new__(Screenshot)
    obj.config = SimpleNamespace(
        Emulator_ScreenshotMethod=config_values[backend],
        Emulator_ScreenshotDedithering=False,
        Error_SaveError=False,
        Emulator_Serial="USB123",
    )
    obj._screenshot_interval = MagicMock()
    obj.screenshot_method_override = ""
    method = MagicMock(return_value=image)
    for name in method_names.values():
        setattr(obj, name, method if name == method_names[backend] else MagicMock())
    obj.screenshot_methods = Screenshot.screenshot_methods.func(obj)
    obj._handle_orientated_image = MagicMock(side_effect=lambda value: value)
    obj.check_screen_size = MagicMock(return_value=True)
    obj.check_screen_black = MagicMock(return_value=True)
    result = Screenshot.screenshot(obj)
    assert result is image
    method.assert_called_once_with()


def _test_input_backend_matrix(backend: str) -> None:
    config_values = {
        "adb": "ADB",
        "uiautomator2": "uiautomator2",
        "minitouch": "minitouch",
        "hermit": "Hermit",
        "maatouch": "MaaTouch",
        "nemu_ipc": "nemu_ipc",
    }
    method_names = {
        "adb": "click_adb",
        "uiautomator2": "click_uiautomator2",
        "minitouch": "click_minitouch",
        "hermit": "click_hermit",
        "maatouch": "click_maatouch",
        "nemu_ipc": "click_nemu_ipc",
    }
    from module.device.control import Control

    obj = Control.__new__(Control)
    obj.config = SimpleNamespace(Emulator_ControlMethod=config_values[backend])
    obj.handle_control_check = MagicMock()
    selected = MagicMock()
    for name in method_names.values():
        setattr(obj, name, selected if name == method_names[backend] else MagicMock())
    obj.click_methods = Control.click_methods.func(obj)
    button = SimpleNamespace(button=(10, 20, 10, 20))
    with patch("module.device.control.random_rectangle_point", return_value=(10, 20)):
        Control.click(obj, button)
    selected.assert_called_once_with(10, 20)

def _test_image_contract(scenario: str) -> None:
    if scenario in {"numpy_ndarray", "bgr", "width_height"}:
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        metadata = acceptance._validate_bgr_image(image)
        assert metadata["array_shape"] == [720, 1280, 3]
        assert metadata["color_contract"] == "BGR"
        return
    if scenario == "normalization_1280x720":
        image = np.zeros((1080, 1920, 3), dtype=np.uint8)
        result = Screenshot.resize_screenshot_to_720p(image)
        assert result.shape == (720, 1280, 3)
        return
    if scenario == "orientation":
        obj = Screenshot.__new__(Screenshot)
        obj.image = np.zeros((1280, 720, 3), dtype=np.uint8)
        obj.orientation = 3
        assert Screenshot._handle_orientated_image(obj, obj.image).shape == (720, 1280, 3)
        return
    if scenario == "no_binary_log":
        from dev_tools.stage8a_binary_log_audit import find_binary_payload_log_findings
        from pathlib import Path

        assert find_binary_payload_log_findings(Path(__file__).resolve().parents[1]) == []
        return
    raise AssertionError(f"Unhandled image scenario: {scenario}")


def _control_sender():
    parent = SimpleNamespace(
        _scrcpy_control_socket=None,
        _scrcpy_control_socket_lock=MagicMock(),
        _scrcpy_resolution=(1280, 720),
    )
    return ControlSender(parent)


def _test_input_backend(scenario: str) -> None:
    sender = _control_sender()
    if scenario == "click":
        packet = sender.touch(100, 200, scrcpy_const.ACTION_DOWN)
        assert packet[0] == scrcpy_const.TYPE_INJECT_TOUCH_EVENT
        assert len(packet) > 20
        return
    if scenario == "swipe":
        down = sender.touch(10, 20, scrcpy_const.ACTION_DOWN)
        move = sender.touch(30, 40, scrcpy_const.ACTION_MOVE)
        up = sender.touch(30, 40, scrcpy_const.ACTION_UP)
        assert down[0] == move[0] == up[0] == scrcpy_const.TYPE_INJECT_TOUCH_EVENT
        assert down != move != up
        return
    if scenario == "key":
        packet = sender.keycode(scrcpy_const.KEYCODE_BACK)
        assert packet[0] == scrcpy_const.TYPE_INJECT_KEYCODE
        assert struct.unpack(">Biii", packet[1:])[:2] == (scrcpy_const.ACTION_DOWN, scrcpy_const.KEYCODE_BACK)
        return
    if scenario == "text":
        packet = sender.text("тест")
        size = struct.unpack(">i", packet[1:5])[0]
        assert packet[0] == scrcpy_const.TYPE_INJECT_TEXT
        assert packet[5:] == "тест".encode("utf-8")
        assert size == len(packet[5:])
        return
    if scenario == "empty_command":
        device = SimpleNamespace(orientation=0, max_x=1280, max_y=720, config=SimpleNamespace(DEVICE_OVER_HTTP=False))
        builder = CommandBuilder(device)
        builder.commit().wait(10)
        with patch("module.device.method.minitouch.logger") as logger:
            text = builder.to_minitouch()
        assert text == "c\nw 10\n"
        logger.warning.assert_called_once()
        return
    if scenario == "invalid_orientation":
        device = SimpleNamespace(orientation=9, max_x=1280, max_y=720, config=SimpleNamespace(DEVICE_OVER_HTTP=False))
        with unittest.TestCase().assertRaises(ScriptError):
            CommandBuilder(device).down(1, 2)
        return
    if scenario == "socket_close":
        device = SimpleNamespace(_minitouch_client=_FakeSocket(), _minitouch_port=12345, adb_forward_remove=MagicMock())
        acceptance._close_minitouch_probe(device)
        assert device._minitouch_client.closed
        device.adb_forward_remove.assert_called_once_with("tcp:12345")
        return
    if scenario == "backend_unavailable":
        obj = MagicMock()
        obj._minitouch_port = 0
        obj.install_uiautomator2 = MagicMock()
        probe, _ = _retry_probe(MinitouchNotInstalledError("missing"))
        with patch("module.device.method.minitouch.RETRY_TRIES", 1):
            with unittest.TestCase().assertRaises(RequestHumanTakeover):
                minitouch_retry(probe)(obj)
        return
    if scenario == "timeout":
        obj = MagicMock()
        obj._minitouch_port = 0
        probe, _ = _retry_probe(socket.timeout("timeout"))
        with patch("module.device.method.minitouch.RETRY_TRIES", 1):
            with unittest.TestCase().assertRaises(RequestHumanTakeover):
                minitouch_retry(probe)(obj)
        return
    if scenario == "reconnect":
        obj = MagicMock()
        obj._minitouch_port = 0
        obj.adb_reconnect = MagicMock()
        probe, calls = _retry_probe(ConnectionResetError("reset"))
        with patch("module.device.method.minitouch.RETRY_TRIES", 2), patch(
            "module.device.method.minitouch.retry_sleep", return_value=0
        ), patch("module.device.method.minitouch.time.sleep"):
            assert minitouch_retry(probe)(obj) == "ok"
        assert calls["count"] == 2
        obj.adb_reconnect.assert_called_once_with()
        return
    if scenario == "fallback":
        obj = SimpleNamespace(
            config=SimpleNamespace(Emulator_ControlMethod="ADB"),
            click_adb=MagicMock(),
            click_uiautomator2=MagicMock(),
            click_minitouch=MagicMock(),
            click_maatouch=MagicMock(),
            click_scrcpy=MagicMock(),
            click_hermit=MagicMock(),
            click_nemu_ipc=MagicMock(),
        )
        from module.device.control import Control

        methods = Control.click_methods.func(obj)
        assert methods["ADB"] is obj.click_adb
        return
    raise AssertionError(f"Unhandled input scenario: {scenario}")


def _test_scrcpy(scenario: str) -> None:
    if scenario in {"server_push", "server_startup", "video_stream", "control_stream", "initial_metadata"}:
        obj = ScrcpyCore.__new__(ScrcpyCore)
        server = MagicMock()
        server.read.return_value = b"[server] I"
        server.conn = MagicMock()
        server.conn.recv.return_value = b""
        video = _FakeSocket([
            b"\x00",
            b"TestDevice" + b"\x00" * (64 - len("TestDevice")),
            struct.pack(">HH", 640, 360),
        ])
        control = _FakeSocket()
        obj.config = SimpleNamespace(
            SCRCPY_FILEPATH_LOCAL="local.jar",
            SCRCPY_FILEPATH_REMOTE="/data/local/tmp/server.jar",
        )
        obj.adb_push = MagicMock()
        obj.adb = MagicMock()
        obj.adb.shell.return_value = server
        obj.adb.create_connection.side_effect = [video, control]
        obj._scrcpy_control_socket_lock = MagicMock()
        obj._scrcpy_stream_loop = MagicMock()
        obj.sleep = MagicMock()
        obj._scrcpy_stream_loop_thread = None
        with patch("module.device.method.scrcpy.core.threading.Thread") as thread_cls:
            thread = thread_cls.return_value
            thread.is_alive.return_value = True
            obj.scrcpy_init()
        obj.adb_push.assert_called_once_with("local.jar", "/data/local/tmp/server.jar")
        assert obj._scrcpy_alive is True
        assert obj._scrcpy_video_socket is video
        assert obj._scrcpy_control_socket is control
        assert obj._scrcpy_resolution == (640, 360)
        return

    if scenario == "stream_close":
        _test_screenshot_backend("stream_close")
        return

    if scenario == "version_mismatch":
        obj = ScrcpyCore.__new__(ScrcpyCore)
        server = MagicMock()
        server.read.return_value = b"[server] E"
        server.conn = MagicMock()
        obj.config = SimpleNamespace(SCRCPY_FILEPATH_REMOTE="remote.jar")
        obj.adb = MagicMock()
        obj.adb.shell.return_value = server
        obj._scrcpy_control_socket_lock = MagicMock()
        with patch(
            "module.device.method.scrcpy.core.recv_all",
            return_value=b" server version does not match the client",
        ):
            with unittest.TestCase().assertRaisesRegex(ScrcpyError, "Версия сервера"):
                ScrcpyCore._scrcpy_server_start(obj)
        return

    if scenario == "fallback":
        _test_screenshot_backend("fallback")
        return

    if scenario == "live_preview":
        session = MagicMock(alive=True, resolution=(640, 360))
        session.read_video.return_value = b"h264"
        live = MagicMock()
        live.acquire.return_value = session
        with patch.object(
            acceptance,
            "_preview_dependencies",
            return_value=(live, lambda: "ffmpeg", MagicMock()),
        ):
            result = acceptance._check_preview("alas", "ADB")
        assert result["mode"] == "scrcpy"
        assert result["first_video_chunk_bytes"] == 4
        return

    if scenario == "device_messages":
        payload = "clip".encode("utf-8")
        sock = _FakeSocket([BlockingIOError(), b"\x00", struct.pack(">i", len(payload)), payload])
        parent = SimpleNamespace(
            _scrcpy_control_socket=sock,
            _scrcpy_control_socket_lock=MagicMock(),
            _scrcpy_resolution=(1280, 720),
        )
        assert ControlSender(parent).get_clipboard() == "clip"
        assert sock.sent == [struct.pack(">B", scrcpy_const.TYPE_GET_CLIPBOARD)]
        return

    if scenario == "control_error":
        parent = SimpleNamespace(
            _scrcpy_control_socket=_FakeSocket(),
            _scrcpy_control_socket_lock=MagicMock(),
            _scrcpy_resolution=(1280, 720),
        )
        parent._scrcpy_control_socket.send = MagicMock(side_effect=OSError("broken"))
        with unittest.TestCase().assertRaises(OSError):
            ControlSender(parent).keycode(scrcpy_const.KEYCODE_BACK)
        return

    raise AssertionError(f"Unhandled scrcpy scenario: {scenario}")


def _uia_stub():
    obj = Uiautomator2.__new__(Uiautomator2)
    obj.u2 = MagicMock()
    obj.sleep = MagicMock()
    obj.adb_reconnect = MagicMock()
    obj.adb_start_server = MagicMock()
    obj.install_uiautomator2 = MagicMock()
    obj.detect_package = MagicMock()
    return obj


def _test_uiautomator2(scenario: str) -> None:
    obj = _uia_stub()
    if scenario == "connect":
        from module.device.connection_attr import ConnectionAttr
        fake = ConnectionAttr.__new__(ConnectionAttr)
        fake.config = SimpleNamespace(DEVICE_OVER_HTTP=False)
        fake.serial = "127.0.0.1:5555"
        fake.is_over_http = False
        fake.is_local_network_device = True
        with patch("module.device.connection_attr.u2.connect_usb") as connect_usb:
            device = connect_usb.return_value
            result = ConnectionAttr.u2.func(fake)
        assert result is device
        connect_usb.assert_called_once_with("127.0.0.1:5555")
        device.set_new_command_timeout.assert_called_once_with(604800)
        return
    if scenario == "info":
        obj.adb_shell = MagicMock(return_value="")
        response = MagicMock()
        response.json.return_value = {"display": {"width": 1280, "height": 720}}
        obj.u2.http.get.return_value = response
        obj.get_orientation = MagicMock(return_value=0)
        assert Uiautomator2.resolution_uiautomator2(obj, cal_rotation=False) == (1280, 720)
        obj.u2.http.get.assert_called_once_with("/info")
        return
    if scenario in {"click_timeout", "drag_timeout", "external_exception_context"}:
        if scenario == "click_timeout":
            method = Uiautomator2.click_uiautomator2
            args = (1, 2)
        elif scenario == "drag_timeout":
            method = Uiautomator2.swipe_uiautomator2
            args = ((1, 2), (3, 4))
        else:
            def raw(_self):
                raise ValueError("external raw failure")
            raw.__name__ = "probe"
            method = uiautomator2_retry(raw)
            args = ()
        if scenario != "external_exception_context":
            target = obj.u2.click if scenario == "click_timeout" else obj.u2.swipe
            target.side_effect = TimeoutError("timeout")
        with patch("module.device.method.uiautomator_2.RETRY_TRIES", 1), patch(
            "module.device.method.uiautomator_2.logger"
        ) as logger:
            with unittest.TestCase().assertRaises(RequestHumanTakeover):
                method(obj, *args)
        assert "timeout" in str(logger.exception.call_args) or "external raw failure" in str(logger.exception.call_args)
        return
    if scenario == "text_input":
        obj.u2.send_keys = MagicMock()
        Uiautomator2.u2_send_keys(obj, "abc", clear=True)
        obj.u2.send_keys.assert_called_once_with(text="abc", clear=True)
        return
    if scenario == "screenshot":
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", image)
        assert ok
        obj.u2.screenshot.return_value = encoded.tobytes()
        result = Uiautomator2.screenshot_uiautomator2(obj)
        assert isinstance(result, np.ndarray)
        assert result.shape == (8, 8, 3)
        return
    if scenario == "service_init":
        from module.device.connection_attr import ConnectionAttr
        fake = ConnectionAttr.__new__(ConnectionAttr)
        fake.config = SimpleNamespace(DEVICE_OVER_HTTP=True)
        fake.serial = "http://127.0.0.1:7912"
        fake.is_over_http = True
        with patch("module.device.connection_attr.u2.connect") as connect:
            device = connect.return_value
            assert ConnectionAttr.u2.func(fake) is device
        connect.assert_called_once_with("http://127.0.0.1:7912")
        device.set_new_command_timeout.assert_called_once_with(604800)
        return
    if scenario == "implicit_wait":
        import importlib.metadata

        import uiautomator2 as u2

        assert importlib.metadata.version("uiautomator2") == "2.16.17"
        device = SimpleNamespace(settings={"wait_timeout": 20.0})
        assert u2.Device.implicitly_wait(device, 0.25) == 0.25
        assert device.settings["wait_timeout"] == 0.25
        assert u2.Device.implicitly_wait(device) == 0.25
        return
    if scenario == "http_timeout":
        response = MagicMock()
        obj.u2.http.get.return_value = response
        Uiautomator2.proc_list_uiautomator2(obj)
        obj.u2.http.get.assert_called_once_with("/proc/list", timeout=10)
        return
    if scenario == "long_click":
        Uiautomator2.long_click_uiautomator2(obj, 1, 2, duration=1.5)
        obj.u2.long_click.assert_called_once_with(1, 2, duration=1.5)
        return
    if scenario == "xpath_wait_get":
        from uiautomator2 import xpath as u2_xpath

        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<hierarchy rotation="0">'
            '<node index="0" text="ready" resource-id="target" '
            'class="android.widget.TextView" package="example" '
            'content-desc="" checkable="false" checked="false" '
            'clickable="true" enabled="true" focusable="false" '
            'focused="false" scrollable="false" long-clickable="false" '
            'password="false" selected="false" bounds="[0,0][10,10]" />'
            '</hierarchy>'
        )
        watcher = SimpleNamespace(run=MagicMock(return_value=False))
        device = SimpleNamespace(
            click=MagicMock(),
            swipe=MagicMock(),
            window_size=MagicMock(return_value=(100, 100)),
            dump_hierarchy=MagicMock(return_value=xml),
            screenshot=MagicMock(),
            wait_timeout=0.25,
            settings={"xpath_debug": False},
            watcher=watcher,
        )
        selector = u2_xpath.XPath(device)('//*[@text="ready"]')
        waited = selector.wait(timeout=0.25)
        assert waited is not None
        element = selector.get(timeout=0.25)
        assert element is not None
        assert element.text == "ready"
        assert device.dump_hierarchy.call_count >= 2
        watcher.run.assert_called()
        return
    raise AssertionError(f"Unhandled uiautomator2 scenario: {scenario}")


def _test_nemu_ldopengl(scenario: str) -> None:
    if scenario == "correct_emulator_family":
        assert NemuIpcImpl.serial_to_id("127.0.0.1:16416") == 1
        assert LDOpenGLImpl.serial_to_id("127.0.0.1:5555") == 0
        return
    if scenario == "unsupported_emulator":
        assert NemuIpcImpl.serial_to_id("emulator-5554") is None
        assert LDOpenGLImpl.serial_to_id("USB123") is None
        return
    if scenario == "version_requirement":
        with patch("module.device.method.ldopengl.os.path.exists", return_value=False), patch(
            "module.device.method.ldopengl.ctypes.WinDLL", side_effect=OSError("missing"), create=True
        ):
            with unittest.TestCase().assertRaisesRegex(LDOpenGLIncompatible, "requires LDPlayer"):
                LDOpenGLImpl("/missing", 0)
        return
    if scenario == "dead_instance":
        obj = LDOpenGLImpl.__new__(LDOpenGLImpl)
        obj.console = MagicMock()
        obj.console.list2.return_value = [
            DataLDPlayerInfo(0, b"dead", 0, 0, 0, -1, -1, 1280, 720, 240)
        ]
        with unittest.TestCase().assertRaises(LDOpenGLError):
            LDOpenGLImpl.get_player_info_by_index(obj, 0)
        return
    if scenario == "native_library_error":
        with patch("module.device.method.ldopengl.os.path.exists", return_value=True), patch(
            "module.device.method.ldopengl.ctypes.WinDLL", side_effect=OSError("bad dll"), create=True
        ):
            with unittest.TestCase().assertRaisesRegex(LDOpenGLIncompatible, "cannot be loaded"):
                LDOpenGLImpl("/bad", 0)
        return
    if scenario == "screenshot_failure":
        obj = MagicMock()
        probe, _ = _retry_probe(NemuIpcError("capture failed"), name="screenshot")
        obj.reconnect = MagicMock()
        with patch("module.device.method.nemu_ipc.RETRY_TRIES", 1):
            with unittest.TestCase().assertRaises(EmulatorNotRunningError):
                nemu_retry(probe)(obj)
        return
    if scenario == "control_failure":
        obj = MagicMock()
        probe, _ = _retry_probe(NemuIpcError("control failed"), name="down")
        obj.reconnect = MagicMock()
        with patch("module.device.method.nemu_ipc.RETRY_TRIES", 1):
            with unittest.TestCase().assertRaises(EmulatorNotRunningError):
                nemu_retry(probe)(obj)
        return
    if scenario == "windows_only_fallback":
        from module.device.method.ldopengl import LDOpenGL
        obj = LDOpenGL.__new__(LDOpenGL)
        obj.config = SimpleNamespace(Emulator_ScreenshotMethod="ldopengl")
        with patch("module.device.method.ldopengl.IS_WINDOWS", False):
            assert obj.ldopengl_available() is False
        return
    raise AssertionError(f"Unhandled Nemu/LD scenario: {scenario}")


def _run_ws_control(payload: dict) -> tuple[MagicMock, _AsyncWebSocket]:
    target = MagicMock()
    ws = _AsyncWebSocket([json.dumps(payload)])
    with patch.object(webui_api, "is_demo_mode", return_value=False), patch.object(
        webui_api.LiveWsScrcpySession, "get", return_value=target
    ), patch.object(webui_api.asyncio, "to_thread", new=asyncio.to_thread):
        asyncio.run(webui_api.ws_live_control(ws))
    return target, ws


def _test_webui_live_control(scenario: str) -> None:
    if scenario in {"start", "socket_close"}:
        ws = _AsyncWebSocket([])
        with patch.object(webui_api, "is_demo_mode", return_value=False), patch.object(
            webui_api.LiveWsScrcpySession, "get", return_value=MagicMock()
        ):
            asyncio.run(webui_api.ws_live_control(ws))
        assert ws.accepted
        return
    if scenario == "stop":
        session = MagicMock()
        webui_api.LiveScrcpySession._sessions = {"alas": session}
        webui_api.LiveScrcpySession.release("alas", session=session)
        session.stop.assert_called_once_with()
        return
    if scenario == "fallback":
        target = MagicMock()
        with patch.object(webui_api.LiveWsScrcpySession, "get", return_value=None), patch.object(
            webui_api.LiveScrcpySession, "get", return_value=None
        ), patch.object(webui_api, "LiveControlDevice", return_value=target):
            ws = _AsyncWebSocket([json.dumps({"type": "tap", "x": 1, "y": 2})])
            with patch.object(webui_api, "is_demo_mode", return_value=False):
                asyncio.run(webui_api.ws_live_control(ws))
        target.tap.assert_called_once_with(1, 2)
        return
    if scenario == "resolution":
        session = webui_api.LiveScrcpySession.__new__(webui_api.LiveScrcpySession)
        session.resolution = (640, 360)
        assert session.scale_point(1280, 720) == (640, 360)
        return
    if scenario == "prebuffer":
        payload = (
            b"\x00\x00\x00\x01\x67\x64\x00\x1f"
            b"\x00\x00\x00\x01\x68\xee\x3c\x80"
            b"\x00\x00\x00\x01\x65\x88\x84"
        )
        session = MagicMock(alive=True)
        session.read_video.side_effect = [payload]
        stop_event = MagicMock()
        stop_event.is_set.return_value = False
        assert webui_api._collect_h264_preroll(session, stop_event, max_wait=0.1) == payload
        return
    payloads = {
        "click": {"type": "tap", "x": 10, "y": 20},
        "drag": {"type": "drag", "start": {"x": 1, "y": 2}, "end": {"x": 3, "y": 4}, "duration_ms": 220},
        "key": {"type": "key", "keycode": 4},
        "text": {"type": "text", "text": "secret"},
        "back": {"type": "back"},
        "system_key": {"type": "home"},
    }
    if scenario in payloads:
        target, _ = _run_ws_control(payloads[scenario])
        expected = {
            "click": call.tap(10, 20),
            "drag": call.drag({"x": 1, "y": 2}, {"x": 3, "y": 4}, 220),
            "key": call.keycode(4),
            "text": call.text("secret"),
            "back": call.keycode(scrcpy_const.KEYCODE_BACK),
            "system_key": call.keycode(scrcpy_const.KEYCODE_HOME),
        }[scenario]
        assert expected in target.mock_calls
        return
    if scenario == "resource_cleanup":
        session = webui_api.LiveScrcpySession.__new__(webui_api.LiveScrcpySession)
        session.alive = True
        session.control_socket = _FakeSocket()
        session.video_socket = _FakeSocket()
        session.server_stream = _FakeSocket()
        session.stop()
        assert session.control_socket is None and session.video_socket is None and session.server_stream is None
        return
    if scenario == "no_user_text_leak":
        target = MagicMock()
        ws = _AsyncWebSocket([json.dumps({"type": "text", "text": "do-not-log"})])
        with patch.object(webui_api, "is_demo_mode", return_value=False), patch.object(
            webui_api.LiveWsScrcpySession, "get", return_value=target
        ), patch.object(webui_api.logger, "info") as info:
            asyncio.run(webui_api.ws_live_control(ws))
        assert all("do-not-log" not in str(item) for item in info.call_args_list)
        target.text.assert_called_once_with("do-not-log")
        return
    raise AssertionError(f"Unhandled WebUI scenario: {scenario}")


SCENARIO_HANDLERS = {
    "adb_state": _test_adb_state,
    "device_readiness": _test_device_readiness,
    "package_detection": _test_package_detection,
    "emulator_lifecycle": _test_emulator_lifecycle,
    "screenshot_backend": _test_screenshot_backend,
    "screenshot_backend_matrix": _test_screenshot_backend_matrix,
    "image_contract": _test_image_contract,
    "input_backend": _test_input_backend,
    "input_backend_matrix": _test_input_backend_matrix,
    "scrcpy": _test_scrcpy,
    "uiautomator2": _test_uiautomator2,
    "nemu_ldopengl": _test_nemu_ldopengl,
    "webui_live_control": _test_webui_live_control,
}


class Stage8ARuntimeScenarioMatrixTests(unittest.TestCase):
    """Executable synthetic/recorded fixtures for every Stage 8A scenario row."""


def _install_tests() -> None:
    from dev_tools.stage8a_evidence_policy import SCENARIO_REQUIREMENTS

    for category, scenarios in SCENARIO_REQUIREMENTS.items():
        handler = SCENARIO_HANDLERS[category]
        for scenario in scenarios:
            name = f"test_{category}__{scenario}"

            def test(self, *, _handler=handler, _scenario=scenario):
                _handler(_scenario)

            test.__name__ = name
            test.__qualname__ = f"Stage8ARuntimeScenarioMatrixTests.{name}"
            setattr(Stage8ARuntimeScenarioMatrixTests, name, test)


_install_tests()


if __name__ == "__main__":
    unittest.main()
