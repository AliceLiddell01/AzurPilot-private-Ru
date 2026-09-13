from __future__ import annotations

import io
from unittest.mock import Mock, patch

import requests
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


def test_http_adapter_uses_explicit_jsonrpc_shell_and_rgb_pillow_screenshot_contracts():
    device = HttpUiautomator2("http://127.0.0.1:7912")
    assert device.http.trust_env is False
    assert device.path2url("/jsonrpc/0") == "http://127.0.0.1:7912/jsonrpc/0"

    rpc_response = FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {
        "display": {"width": 2, "height": 3},
    }})
    with patch.object(device.http, "post", return_value=rpc_response) as post:
        assert device.info["display"] == {"width": 2, "height": 3}
        payload = post.call_args.kwargs["json"]
        assert post.call_args.args == ("/jsonrpc/0",)
        assert payload["method"] == "deviceInfo"
        assert payload["params"] == {}

    shell_response = FakeResponse({"output": "ok\n", "exitCode": 0})
    with patch.object(device.http, "post", return_value=shell_response) as post:
        result = device.shell(["echo", "ok"])
        assert result == ShellResponse("ok\n", 0)
        assert post.call_args.args == ("/shell",)
        assert post.call_args.kwargs["data"]["command"] == "echo ok"

    with patch.object(device.http, "get", return_value=FakeResponse(content=_png_bytes())):
        image = device.screenshot(format="pillow")
    assert image.mode == "RGB"
    assert image.size == (2, 3)
    assert image.getpixel((0, 0)) == (1, 2, 3)


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


def test_http_adapter_keeps_service_and_input_operations_on_same_endpoint():
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
        device.send_action(3)
        device.clear_text()

    assert shell.call_count == 5
    assert shell.call_args_list[0].args[0] == ["ime", "enable", "com.github.uiautomator/.FastInputIME"]
    assert shell.call_args_list[2].args[0][:3] == ["am", "broadcast", "-a"]
    assert shell.call_args_list[2].args[0][-1] == "dGVzdA=="
