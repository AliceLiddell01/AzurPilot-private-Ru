from __future__ import annotations

import io
from unittest.mock import Mock, patch

import pytest
import requests
import uiautomator2 as u2
from PIL import Image
from uiautomator2.abstract import ShellResponse

from module.device.method.uiautomator2_http import HttpUiautomator2


class FakeResponse:
    def __init__(self, payload=None, *, content=b"", status_code=200, url="http://device"):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.url = url
        self.text = content.decode("utf-8", errors="replace")
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _png_bytes() -> bytes:
    image = Image.new("RGB", (2, 3), (1, 2, 3))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_http_adapter_uses_project_boundary_and_public_http_contract():
    device = HttpUiautomator2("http://127.0.0.1:7912")

    assert not isinstance(device, u2.Device)
    assert device.serial == "http://127.0.0.1:7912"
    assert device.http.trust_env is False
    assert device.path2url("/jsonrpc/0") == "http://127.0.0.1:7912/jsonrpc/0"

    rpc_response = FakeResponse({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"display": {"width": 2, "height": 3}},
    })
    with patch.object(device.http, "post", return_value=rpc_response) as post:
        assert device.info["display"] == {"width": 2, "height": 3}
        payload = post.call_args.kwargs["json"]
        assert post.call_args.args == ("/jsonrpc/0",)
        assert payload["method"] == "deviceInfo"
        assert payload["params"] == {}

    shell_response = FakeResponse({"output": "ok\n", "exitCode": 0})
    with patch.object(device.http, "post", return_value=shell_response) as post:
        assert device.shell(["echo", "ok"]) == ShellResponse("ok\n", 0)
        assert post.call_args.args == ("/shell",)
        assert post.call_args.kwargs["data"]["command"] == "echo ok"

    with patch.object(device.http, "get", return_value=FakeResponse(content=_png_bytes())):
        image = device.screenshot(format="pillow")
    assert image.mode == "RGB"
    assert image.size == (2, 3)
    assert image.getpixel((0, 0)) == (1, 2, 3)


def test_http_adapter_coordinates_touch_and_swipe_through_jsonrpc():
    device = HttpUiautomator2("http://127.0.0.1:7912")
    device.window_size = Mock(return_value=(100, 200))
    device.jsonrpc_call = Mock(return_value=True)

    device.click(0.5, 0.25)
    device.long_click(10, 20, duration=1.2)
    device.swipe(0.1, 0.2, 90, 100, duration=0.5)
    device.touch.down(0.1, 0.2).move(0.2, 0.3).up(0.3, 0.4)

    calls = device.jsonrpc_call.call_args_list
    assert calls[0].args == ("click", (50, 50), 10)
    assert calls[1].args == ("click", (10, 20, 1200), 10)
    assert calls[2].args == ("swipe", (10, 40, 90, 100, 100), 10)
    assert [call.args for call in calls[3:]] == [
        ("injectInputEvent", (0, 10, 40, 0), 10),
        ("injectInputEvent", (2, 20, 60, 0), 10),
        ("injectInputEvent", (1, 30, 80, 0), 10),
    ]


def test_http_adapter_xpath_uses_remote_hierarchy_and_project_coordinates():
    device = HttpUiautomator2("http://127.0.0.1:7912")
    hierarchy = (
        '<hierarchy><node text="ОК" bounds="[10,20][30,40]" />'
        "</hierarchy>"
    )
    device.jsonrpc_call = Mock(return_value=hierarchy)

    selector = device.xpath('//*[@text="ОК"]')

    assert selector.exists is True
    assert selector.bounds == (10, 20, 30, 40)
    selector.click()

    methods = [call.args[0] for call in device.jsonrpc_call.call_args_list]
    assert methods.count("dumpWindowHierarchy") >= 2
    assert methods[-1] == "click"
    assert device.jsonrpc_call.call_args_list[-1].args[1] == (20, 30)


def test_project_uiautomator2_screenshot_boundary_returns_rgb_numpy_array():
    from module.device.method.uiautomator_2 import Uiautomator2

    class FakeUiautomator2:
        def screenshot(self, *, format):
            assert format == "pillow"
            return Image.new("RGB", (1, 1), (1, 2, 3))

    device = type("FakeDevice", (), {"u2": FakeUiautomator2()})()
    image = Uiautomator2.screenshot_uiautomator2.__wrapped__(device)

    assert image.shape == (1, 1, 3)
    assert tuple(image[0, 0]) == (1, 2, 3)


def test_http_adapter_window_size_accepts_nested_and_flat_device_info():
    device = HttpUiautomator2("http://127.0.0.1:7912")
    responses = [
        FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {
            "display": {"width": 1080, "height": 1920},
        }}),
        FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {
            "displayWidth": 720, "displayHeight": 1280,
        }}),
    ]

    with patch.object(device.http, "post", side_effect=responses):
        assert device.window_size() == (1080, 1920)
        assert device.window_size() == (720, 1280)


def test_http_adapter_stream_shell_keeps_bounded_timeout():
    device = HttpUiautomator2("http://127.0.0.1:7912")
    response = FakeResponse()

    with patch.object(device.http, "get", return_value=response) as get:
        assert device.shell(["logcat"], stream=True, timeout=7) is response

    assert get.call_args.args == ("/shell/stream",)
    assert get.call_args.kwargs["timeout"] == (10, 7)
    assert get.call_args.kwargs["stream"] is True


def test_http_adapter_keeps_service_input_and_recovery_on_same_endpoint():
    device = HttpUiautomator2("https://phone-cloud.example:7912")
    service = device.service("minitouch")

    with patch.object(device.http, "post", return_value=FakeResponse()) as post:
        service.start()
        assert post.call_args.args == ("/services/minitouch",)

    with patch.object(device.http, "delete", return_value=FakeResponse()) as delete:
        service.stop()
        assert delete.call_args.args == ("/services/minitouch",)

    with patch.object(device.http, "get", return_value=FakeResponse({"running": True})):
        assert service.running() is True

    shell = Mock(return_value=ShellResponse("", 0))
    with patch.object(device, "shell", shell):
        device.set_fastinput_ime(True)
        device.send_keys("test", clear=True)
        device.send_action("done")
        device.clear_text()

    commands = [call.args[0] for call in shell.call_args_list]
    assert commands[:3] == [
        ["ime", "enable", "com.github.uiautomator/.AdbKeyboard"],
        ["ime", "set", "com.github.uiautomator/.AdbKeyboard"],
        ["settings", "put", "secure", "default_input_method", "com.github.uiautomator/.AdbKeyboard"],
    ]
    assert ["am", "broadcast", "-a", "ADB_KEYBOARD_CLEAR_TEXT"] in commands
    assert ["am", "broadcast", "-a", "ADB_KEYBOARD_INPUT_TEXT", "--es", "text", "dGVzdA=="] in commands
    assert ["am", "broadcast", "-a", "ADB_KEYBOARD_HIDE"] in commands
    assert ["am", "broadcast", "-a", "ADB_KEYBOARD_EDITOR_CODE", "--ei", "code", "6"] in commands

    service_mock = Mock()
    service_mock.stop.side_effect = u2.DeviceError("already stopped")
    with patch.object(device, "service", return_value=service_mock):
        device.reset_uiautomator()
    service_mock.stop.assert_called_once_with()
    service_mock.start.assert_called_once_with()


def test_http_adapter_current_ime_uses_remote_shell():
    device = HttpUiautomator2("https://phone-cloud.example:7912")
    shell = Mock(return_value=ShellResponse("com.example/.Ime\n", 0))

    with patch.object(device, "shell", shell):
        assert device.current_ime() == "com.example/.Ime"

    shell.assert_called_once_with(["settings", "get", "secure", "default_input_method"])


def test_http_adapter_maps_rpc_and_network_failures_without_adb_fallback():
    device = HttpUiautomator2("http://127.0.0.1:7912")

    with patch.object(device.http, "post", side_effect=requests.ConnectionError("offline")):
        with pytest.raises(u2.DeviceError, match="JSON-RPC"):
            device.jsonrpc_call("deviceInfo", {})

    error_response = FakeResponse(
        {"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32000,
            "message": "uiautomator.UiObjectNotFoundException: missing",
        }}
    )
    with patch.object(device.http, "post", return_value=error_response):
        with pytest.raises(u2.UiObjectNotFoundError):
            device.jsonrpc_call("click", (1, 2))

    with pytest.raises(u2.DeviceError, match="локальный AdbDevice"):
        device.adb_device
