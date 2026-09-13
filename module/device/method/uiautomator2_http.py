"""Project-owned adapter for the phone-cloud uiautomator2 HTTP protocol.

Локальный transport создаётся самим ``uiautomator2`` и использует ADB.
Phone-cloud transport реализует только общий project contract через HTTP и
JSON-RPC; он не является частично инициализированным ``u2.Device``.
"""

from __future__ import annotations

import base64
import io
import re
import shlex
from collections.abc import Sequence
from urllib.parse import urljoin

import cv2
import numpy as np
import requests
import uiautomator2 as u2
from PIL import Image
from uiautomator2.abstract import ShellResponse
from uiautomator2.xpath import XPathEntry


HTTP_DEVICE_SERVER_PORT = 9008
"""Стандартный порт внешнего uiautomator2 HTTP protocol."""

DEFAULT_WAIT_TIMEOUT = 20.0


class _HttpSession(requests.Session):
    """Сессия requests с абсолютным адресом HTTP device endpoint."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/") + "/"
        self.trust_env = False
        self.headers.update({"Accept-Encoding": ""})

    def request(self, method, url, **kwargs):
        if not re.match(r"^https?://", url):
            url = urljoin(self.base_url, url.lstrip("/"))
        return super().request(method, url, **kwargs)


class _JsonRpcMethod:
    def __init__(self, device: HttpUiautomator2, method: str) -> None:
        self._device = device
        self._method = method

    def __call__(self, *args, **kwargs):
        timeout = kwargs.pop("http_timeout", 10)
        params = args if args else kwargs
        return self._device.jsonrpc_call(self._method, params, timeout)


class _JsonRpcProxy:
    """Публичный project facade для методов JSON-RPC."""

    def __init__(self, device: HttpUiautomator2) -> None:
        self._device = device

    def __getattr__(self, method: str) -> _JsonRpcMethod:
        if method.startswith("_"):
            raise AttributeError(method)
        return _JsonRpcMethod(self._device, method)


class _HttpTouch:
    """Touch facade поверх JSON-RPC injectInputEvent."""

    ACTION_DOWN = 0
    ACTION_UP = 1
    ACTION_MOVE = 2

    def __init__(self, device: HttpUiautomator2) -> None:
        self._device = device

    def down(self, x, y):
        x, y = self._device.pos_rel2abs(x, y)
        self._device.jsonrpc.injectInputEvent(self.ACTION_DOWN, x, y, 0)
        return self

    def move(self, x, y):
        x, y = self._device.pos_rel2abs(x, y)
        self._device.jsonrpc.injectInputEvent(self.ACTION_MOVE, x, y, 0)
        return self

    def up(self, x, y):
        x, y = self._device.pos_rel2abs(x, y)
        self._device.jsonrpc.injectInputEvent(self.ACTION_UP, x, y, 0)
        return self


class _HttpService:
    """Минимальный service API внешнего uiautomator2 endpoint."""

    def __init__(self, name: str, device: HttpUiautomator2) -> None:
        self.name = name
        self.device = device
        self.service_url = f"/services/{name}"

    def _request(self, method: str, timeout: float) -> requests.Response:
        try:
            response = getattr(self.device.http, method)(
                self.service_url,
                timeout=timeout,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            raise u2.DeviceError(f"Ошибка HTTP службы {self.name}: {exc}") from exc

    def start(self) -> None:
        self._request("post", 30)

    def stop(self) -> None:
        self._request("delete", 30)

    def running(self) -> bool:
        response = self._request("get", 10)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise u2.DeviceError(f"Служба {self.name} вернула недопустимый JSON") from exc
        if not isinstance(payload, dict):
            raise u2.DeviceError(f"Служба {self.name} вернула не объект JSON")
        return bool(payload.get("running"))


class HttpUiautomator2:
    """Стабильный phone-cloud adapter без наследования от ``u2.Device``."""

    def __init__(self, serial: str, port: int = HTTP_DEVICE_SERVER_PORT) -> None:
        if not re.match(r"^https?://", serial):
            raise ValueError(f"HTTP serial is required, got {serial!r}")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"HTTP device port is invalid, got {port!r}")

        self._serial = serial
        self._device_server_port = port
        self._http = _HttpSession(serial)
        self._jsonrpc = _JsonRpcProxy(self)
        self._wait_timeout = DEFAULT_WAIT_TIMEOUT
        self._xpath = XPathEntry(self)

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def http(self) -> _HttpSession:
        return self._http

    @property
    def jsonrpc(self) -> _JsonRpcProxy:
        return self._jsonrpc

    @property
    def xpath(self) -> XPathEntry:
        return self._xpath

    @property
    def touch(self) -> _HttpTouch:
        return _HttpTouch(self)

    @property
    def wait_timeout(self) -> float:
        return self._wait_timeout

    @wait_timeout.setter
    def wait_timeout(self, value: float) -> None:
        self._wait_timeout = float(value)

    @property
    def adb_device(self):
        """Запретить случайный переход phone-cloud в локальный ADB."""
        raise u2.DeviceError("HTTP device не предоставляет локальный AdbDevice")

    @property
    def pos_rel2abs(self):
        """Преобразовать координаты в долях экрана в пиксели."""
        size: list[int] = []

        def convert(x, y):
            if x < 0 or y < 0:
                raise ValueError("Координаты устройства не могут быть отрицательными")
            if (x < 1 or y < 1) and not size:
                size.extend(self.window_size())
            if x < 1:
                x = int(size[0] * x)
            if y < 1:
                y = int(size[1] * y)
            return x, y

        return convert

    def path2url(self, path: str) -> str:
        if re.match(r"^(?:ws|wss|http|https)://", path):
            return path
        return urljoin(self._http.base_url, path.lstrip("/"))

    def jsonrpc_call(self, method: str, params=None, timeout: float = 10):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        try:
            response = self.http.post("/jsonrpc/0", json=payload, timeout=timeout)
            if response.status_code == 410:
                raise u2.SessionBrokenError("HTTP uiautomator2 service is unavailable")
            response.raise_for_status()
            data = response.json()
        except u2.SessionBrokenError:
            raise
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise u2.DeviceError(f"Ошибка HTTP JSON-RPC uiautomator2: {exc}") from exc

        if not isinstance(data, dict):
            raise u2.RPCInvalidError("Ответ JSON-RPC не является объектом")

        error = data.get("error")
        if error is not None:
            if not isinstance(error, dict):
                raise u2.RPCInvalidError("Поле error JSON-RPC не является объектом")
            message = str(error.get("message", ""))
            details = error.get("data")
            response_text = getattr(response, "text", "")
            if "UiAutomation not connected" in response_text:
                raise u2.UiAutomationNotConnectedError(message)
            if "uiautomator.UiObjectNotFoundException" in message:
                raise u2.UiObjectNotFoundError(message)
            raise u2.RPCUnknownError(
                f"JSON-RPC {error.get('code')}: {message}",
                params,
                details,
            )
        if "result" not in data:
            raise u2.RPCInvalidError("Ответ JSON-RPC не содержит result")
        return data["result"]

    def click(self, x, y):
        x, y = self.pos_rel2abs(x, y)
        return self.jsonrpc.click(x, y)

    def long_click(self, x, y, duration: float = 0.5):
        x, y = self.pos_rel2abs(x, y)
        return self.jsonrpc.click(x, y, int(duration * 1000))

    def swipe(self, fx, fy, tx, ty, duration: float | None = None, steps: int | None = None):
        if duration is not None and steps is not None:
            raise ValueError("Нельзя одновременно задавать duration и steps")
        if duration:
            steps = int(duration * 200)
        if not steps:
            steps = 20
        fx, fy = self.pos_rel2abs(fx, fy)
        tx, ty = self.pos_rel2abs(tx, ty)
        return self.jsonrpc.swipe(fx, fy, tx, ty, max(2, steps))

    @staticmethod
    def _shell_command(cmdargs: str | Sequence[str]) -> str:
        if isinstance(cmdargs, str):
            return cmdargs
        if isinstance(cmdargs, (list, tuple)):
            return shlex.join([str(argument) for argument in cmdargs])
        raise TypeError("Недопустимый тип команды shell", type(cmdargs))

    def shell(self, cmdargs, stream: bool = False, timeout: float | None = 60):
        command = self._shell_command(cmdargs)
        timeout_value = 60 if timeout is None else timeout

        if stream:
            try:
                response = self.http.get(
                    "/shell/stream",
                    params={"command": command},
                    timeout=(10, timeout_value),
                    stream=True,
                )
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                raise u2.DeviceError(f"Ошибка HTTP потокового shell uiautomator2: {exc}") from exc

        try:
            response = self.http.post(
                "/shell",
                data={"command": command, "timeout": str(timeout_value)},
                timeout=timeout_value + 10,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError("Ответ HTTP shell не является объектом")
            exit_code = int(data.get("exitCode", 1 if data.get("error") else 0))
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise u2.DeviceError(f"Ошибка HTTP shell uiautomator2: {exc}") from exc
        return ShellResponse(data.get("output", ""), exit_code)

    @property
    def info(self):
        return self.jsonrpc.deviceInfo(http_timeout=10)

    def window_size(self):
        info = self.info
        try:
            display = info.get("display", info)
            if "width" in display and "height" in display:
                return int(display["width"]), int(display["height"])
            return int(info["displayWidth"]), int(info["displayHeight"])
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise u2.DeviceError("HTTP uiautomator2 не вернул размер экрана") from exc

    def screenshot(self, filename=None, format="pillow", display_id=None):
        del display_id
        try:
            response = self.http.get("/screenshot/0", timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise u2.DeviceError(f"Ошибка HTTP screenshot uiautomator2: {exc}") from exc

        if filename:
            with open(filename, "wb") as image_file:
                image_file.write(response.content)
            return None
        if format == "raw":
            return response.content
        if format == "opencv":
            image = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise u2.DeviceError("HTTP uiautomator2 вернул повреждённый screenshot")
            return image
        try:
            return Image.open(io.BytesIO(response.content)).convert("RGB")
        except (OSError, ValueError) as exc:
            raise u2.DeviceError("HTTP uiautomator2 вернул повреждённый screenshot") from exc

    def dump_hierarchy(self, compressed=False, pretty=False, max_depth=None, root_in_active=None):
        del pretty, root_in_active
        if max_depth is None:
            max_depth = 50
        return self.jsonrpc.dumpWindowHierarchy(compressed, max_depth)

    def app_current(self):
        try:
            response = self.http.get("/current", timeout=10)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and "package" in payload:
                return payload
        except (requests.RequestException, TypeError, ValueError):
            pass

        output = self.shell(["dumpsys", "window", "windows"]).output
        match = re.search(r"mCurrentFocus=Window\{.*?\s+(?P<package>[^\s]+)/(?P<activity>[^\s}]+)\}", output)
        if match:
            return match.groupdict()
        raise u2.DeviceError("Не удалось определить текущее приложение через HTTP uiautomator2")

    def app_info(self, package_name: str):
        try:
            response = self.http.get(f"/packages/{package_name}/info", timeout=10)
            if response.status_code == 404:
                raise u2.AppNotFoundError(f"Пакет не найден: {package_name}")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Ответ сведений о пакете не является объектом")
            return payload
        except u2.AppNotFoundError:
            raise
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise u2.DeviceError(f"Не удалось получить сведения о пакете {package_name}") from exc

    def app_stop(self, package_name: str):
        return self.shell(["am", "force-stop", package_name])

    def reset_uiautomator(self):
        service = self.service("uiautomator")
        try:
            service.stop()
        except u2.DeviceError:
            pass
        service.start()

    def start_uiautomator(self):
        self.service("uiautomator").start()

    def stop_uiautomator(self, wait=True):
        del wait
        self.service("uiautomator").stop()

    def service(self, name: str) -> _HttpService:
        return _HttpService(name, self)

    @property
    def uiautomator(self) -> _HttpService:
        return self.service("uiautomator")

    @property
    def wlan_ip(self):
        try:
            response = self.http.get("/wlan/ip", timeout=5)
            response.raise_for_status()
            ip = response.text.strip()
        except requests.RequestException:
            return None
        return ip if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) else None

    def set_fastinput_ime(self, enable: bool = True):
        ime = "com.github.uiautomator/.FastInputIME"
        if enable:
            self.shell(["ime", "enable", ime])
            self.shell(["ime", "set", ime])
        else:
            self.shell(["ime", "disable", ime])

    def send_keys(self, text: str, clear: bool = False):
        action = "ADB_SET_TEXT" if clear else "ADB_INPUT_TEXT"
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return self.shell(["am", "broadcast", "-a", action, "--es", "text", encoded])

    def send_action(self, code=None):
        if code is None:
            return self.shell(["am", "broadcast", "-a", "ADB_KEYBOARD_SMART_ENTER"])
        return self.shell(["am", "broadcast", "-a", "ADB_KEYBOARD_EDITOR_CODE", "--ei", "code", str(code)])

    def clear_text(self):
        return self.shell(["am", "broadcast", "-a", "ADB_KEYBOARD_CLEAR_TEXT"])

    def current_ime(self):
        return self.shell(["settings", "get", "secure", "default_input_method"]).output.strip()

    @property
    def clipboard(self):
        return self.jsonrpc.getClipboard()

    def set_clipboard(self, text, label=None):
        return self.jsonrpc.setClipboard(label, text)
