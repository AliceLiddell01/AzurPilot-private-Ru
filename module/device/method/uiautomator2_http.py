"""HTTP-адаптер для legacy uiautomator2 endpoint в API 3.x.

Локальное подключение использует встроенный ``uiautomator2`` server и ADB.
Телефонное облако по-прежнему предоставляет старый HTTP-контракт, поэтому
для него нужен небольшой адаптер поверх публичного ``uiautomator2.Device``.
"""

from __future__ import annotations

import base64
import io
import re
from urllib.parse import urljoin

import cv2
import numpy as np
import requests
import uiautomator2 as u2
from PIL import Image
from uiautomator2.abstract import ShellResponse
from uiautomator2.utils import list2cmdline


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


class _HttpService:
    """Минимальный service API, совместимый с endpoint старого агента."""

    def __init__(self, name: str, device: "HttpUiautomator2") -> None:
        self.name = name
        self.device = device
        self.service_url = f"/services/{name}"

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise u2.DeviceError(str(exc)) from exc

    def start(self) -> None:
        response = self.device.http.post(self.service_url, timeout=30)
        self._raise_for_status(response)

    def stop(self) -> None:
        response = self.device.http.delete(self.service_url, timeout=30)
        self._raise_for_status(response)

    def running(self) -> bool:
        response = self.device.http.get(self.service_url, timeout=10)
        self._raise_for_status(response)
        return bool(response.json().get("running"))


class HttpUiautomator2(u2.Device):
    """Сохранить HTTP transport, используя публичную модель объектов u2 3.x."""

    def __init__(self, serial: str, port: int = 9008) -> None:
        if not re.match(r"^https?://", serial):
            raise ValueError(f"HTTP serial is required, got {serial!r}")

        # Не вызываем u2.Device.__init__: он запускает локальный u2.jar через ADB.
        self._BaseClient__serial = serial
        self._dev = None
        self._debug = False
        self._device_server_port = port
        self._process = None
        self._http = _HttpSession(serial)

    @property
    def http(self) -> _HttpSession:
        return self._http

    @property
    def adb_device(self):
        raise u2.DeviceError("HTTP device не предоставляет локальный AdbDevice")

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
        except (requests.RequestException, ValueError) as exc:
            raise u2.DeviceError(f"Ошибка HTTP JSON-RPC uiautomator2: {exc}") from exc

        if not isinstance(data, dict):
            raise u2.RPCInvalidError("Ответ JSON-RPC не является объектом")

        error = data.get("error")
        if error:
            message = str(error.get("message", ""))
            details = error.get("data")
            if "UiAutomation not connected" in response.text:
                raise u2.UiAutomationNotConnectedError(message)
            if "uiautomator.UiObjectNotFoundException" in message:
                raise u2.UiObjectNotFoundError(message)
            raise u2.RPCUnknownError(f"JSON-RPC {error.get('code')}: {message}", params, details)
        if "result" not in data:
            raise u2.RPCInvalidError("Ответ JSON-RPC не содержит result")
        return data["result"]

    def shell(self, cmdargs, stream: bool = False, timeout=60):
        if isinstance(cmdargs, (list, tuple)):
            command = list2cmdline(cmdargs)
        elif isinstance(cmdargs, str):
            command = cmdargs
        else:
            raise TypeError("Недопустимый тип команды shell", type(cmdargs))

        if stream:
            try:
                response = self.http.get(
                    "/shell/stream",
                    params={"command": command},
                    timeout=None,
                    stream=True,
                )
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                raise u2.DeviceError(f"Ошибка HTTP потокового shell uiautomator2: {exc}") from exc

        try:
            response = self.http.post(
                "/shell",
                data={"command": command, "timeout": str(timeout)},
                timeout=(timeout or 60) + 10,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Ответ HTTP shell не является объектом")
        except (requests.RequestException, ValueError) as exc:
            raise u2.DeviceError(f"Ошибка HTTP shell uiautomator2: {exc}") from exc

        exit_code = int(data.get("exitCode", 1 if data.get("error") else 0))
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
            if response.ok:
                return response.json()
        except (requests.RequestException, ValueError):
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
            return response.json()
        except u2.AppNotFoundError:
            raise
        except (requests.RequestException, ValueError) as exc:
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
            ip = self.http.get("/wlan/ip", timeout=5).text.strip()
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

    def show_float_window(self, show=True):
        del show
