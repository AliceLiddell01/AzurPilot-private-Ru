"""Небольшой outbound-only SSE/ACK client для Desktop Agent."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from deploy.atomic import atomic_write
from module.application.notifications.agent import (
    DESKTOP_AGENT_ACK_PATH,
    DESKTOP_AGENT_STREAM_PATH,
    MAX_AGENT_BATCH_SIZE,
    DesktopAgentConfigurationError,
    DesktopAgentCredential,
    NotificationCursor,
    NotificationCursorError,
    validate_agent_delivery_document,
)

MAX_CURSOR_FILE_BYTES = 64 * 1024
CLIENT_CONNECT_TIMEOUT_SECONDS = 10.0
CLIENT_READ_TIMEOUT_SECONDS = 60.0
CLIENT_RECONNECT_MIN_SECONDS = 1.0
CLIENT_RECONNECT_MAX_SECONDS = 30.0
MAX_HANDLED_NOTIFICATION_KEYS = 4096


class DesktopAgentClientError(RuntimeError):
    """Ошибка client-side протокола без raw response/credential в тексте."""


class DesktopAgentProtocolError(DesktopAgentClientError):
    """Сервер или SSE frame нарушил bounded contract."""


class DesktopAgentTransportError(DesktopAgentClientError):
    """Временная ошибка исходящего HTTPS соединения."""


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str
    event_id: str | None
    data: str


async def iter_sse_events(
    chunks: AsyncIterable[bytes | str],
    *,
    max_event_bytes: int = 256 * 1024,
) -> AsyncIterable[SSEEvent]:
    """Разобрать SSE line protocol с ограничением одного frame."""

    if not 1024 <= max_event_bytes <= 1024 * 1024:
        raise ValueError("SSE frame limit вне bounded диапазона.")
    buffer = ""
    event_name = "message"
    event_id: str | None = None
    data_lines: list[str] = []
    frame_bytes = 0

    async def feed_line(line: str) -> SSEEvent | None:
        nonlocal event_name, event_id, data_lines, frame_bytes
        if line == "":
            if not data_lines:
                event_name = "message"
                event_id = None
                frame_bytes = 0
                return None
            result = SSEEvent(event_name, event_id, "\n".join(data_lines))
            event_name = "message"
            event_id = None
            data_lines = []
            frame_bytes = 0
            return result
        if line.startswith(":"):
            return None
        field_name, separator, field_value = line.partition(":")
        if separator and field_value.startswith(" "):
            field_value = field_value[1:]
        if field_name == "event":
            if len(field_value) > 128:
                raise DesktopAgentProtocolError("SSE event name слишком длинный.")
            event_name = field_value or "message"
        elif field_name == "id":
            if len(field_value) > 512 or "\x00" in field_value:
                raise DesktopAgentProtocolError("SSE event id имеет неверный размер.")
            event_id = field_value
        elif field_name == "data":
            frame_bytes += len(field_value.encode("utf-8"))
            if frame_bytes > max_event_bytes:
                raise DesktopAgentProtocolError("SSE frame превысил bounded размер.")
            data_lines.append(field_value)
        return None

    async for chunk in chunks:
        if isinstance(chunk, bytes):
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                raise DesktopAgentProtocolError("SSE поток имеет неверную UTF-8 кодировку.") from None
        elif isinstance(chunk, str):
            text = chunk
        else:
            raise DesktopAgentProtocolError("SSE поток содержит неизвестный тип chunk.")
        buffer += text
        if len(buffer.encode("utf-8")) > max_event_bytes + 65_536:
            raise DesktopAgentProtocolError("SSE line buffer превысил bounded размер.")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.removesuffix("\r")
            result = await feed_line(line)
            if result is not None:
                yield result
    if buffer:
        buffer = buffer.removesuffix("\r")
        result = await feed_line(buffer)
        if result is not None:
            yield result
    result = await feed_line("")
    if result is not None:
        yield result


@dataclass(frozen=True, slots=True)
class DesktopAgentClientConfig:
    base_url: str
    credential: DesktopAgentCredential
    cursor_file: Path
    batch_size: int = 32
    connect_timeout_seconds: float = CLIENT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = CLIENT_READ_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            raise DesktopAgentConfigurationError("Desktop Agent URL должен использовать HTTPS.")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise DesktopAgentConfigurationError("Desktop Agent URL содержит запрещённые части.")
        if not isinstance(self.cursor_file, Path):
            raise DesktopAgentConfigurationError("Cursor file имеет неверный тип.")
        if not 1 <= self.batch_size <= MAX_AGENT_BATCH_SIZE:
            raise DesktopAgentConfigurationError("Desktop Agent batch size вне bounded диапазона.")
        for value in (self.connect_timeout_seconds, self.read_timeout_seconds):
            if type(value) not in (int, float) or not 0 < float(value) <= 300:
                raise DesktopAgentConfigurationError("Desktop Agent timeout вне bounded диапазона.")

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str | None] | None = None
    ) -> DesktopAgentClientConfig:
        source = environment
        if source is None:
            import os

            source = os.environ
        url = source.get("AZURPILOT_NOTIFICATION_AGENT_URL")
        if not isinstance(url, str) or not url:
            raise DesktopAgentConfigurationError("Desktop Agent URL не задан.")
        credential = DesktopAgentCredential.from_environment(source)
        cursor_value = source.get(
            "AZURPILOT_NOTIFICATION_AGENT_CURSOR_FILE",
            "config/state/notification-agent-cursor.json",
        )
        if not isinstance(cursor_value, str) or not cursor_value or len(cursor_value) > 4096:
            raise DesktopAgentConfigurationError("Desktop Agent cursor file path имеет неверный формат.")
        return cls(url.rstrip("/"), credential, Path(cursor_value))


class DesktopAgentClient:
    """Клиент не открывает listener: только исходящий verified HTTPS stream и ACK."""

    def __init__(
        self,
        config: DesktopAgentClientConfig,
        *,
        on_notification: Callable[[Mapping[str, object]], Awaitable[None] | None],
    ) -> None:
        self.config = config
        if not callable(on_notification):
            raise TypeError("on_notification должен быть callable.")
        self._on_notification = on_notification
        self._handled: dict[tuple[str, int, str], None] = {}

    async def run_once(self, profile_id: str, *, session: aiohttp.ClientSession | None = None) -> str | None:
        if profile_id not in self.config.credential.profiles:
            raise DesktopAgentProtocolError("Профиль отсутствует в Agent scope.")
        cursor = self._read_cursor(profile_id)
        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {self.config.credential.token}",
        }
        if cursor is not None:
            headers["Last-Event-ID"] = cursor
        own_session = session is None
        if own_session:
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=True),
                auto_decompress=False,
            )
        assert session is not None
        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=self.config.connect_timeout_seconds,
            sock_read=self.config.read_timeout_seconds,
        )
        try:
            async with session.get(
                self._endpoint(DESKTOP_AGENT_STREAM_PATH),
                params={"profile": profile_id, "limit": str(self.config.batch_size)},
                headers=headers,
                timeout=timeout,
                ssl=True,
            ) as response:
                if response.status != 200:
                    if response.status in {401, 403, 400}:
                        raise DesktopAgentProtocolError("Desktop Agent stream отклонён сервером.")
                    raise DesktopAgentTransportError("Desktop Agent stream временно недоступен.")
                content_type = response.headers.get("Content-Type", "")
                if not content_type.casefold().startswith("text/event-stream"):
                    raise DesktopAgentProtocolError("Desktop Agent stream имеет неверный Content-Type.")
                latest = cursor
                async for event in iter_sse_events(response.content):
                    if event.event != "notification":
                        continue
                    if event.event_id is None or not event.data:
                        raise DesktopAgentProtocolError("SSE notification frame не имеет id/data.")
                    try:
                        document = validate_agent_delivery_document(json.loads(event.data))
                        frame_cursor = NotificationCursor.decode(
                            event.event_id, expected_profile_id=profile_id
                        )
                    except (ValueError, TypeError):
                        raise DesktopAgentProtocolError("SSE notification data не является корректным JSON.") from None
                    if frame_cursor is None or frame_cursor.event_id != document["event_id"]:
                        raise DesktopAgentProtocolError("SSE id не совпадает с notification identity.")
                    if document["profile_id"] != profile_id:
                        raise DesktopAgentProtocolError("SSE notification profile не совпадает с запросом.")
                    key = (
                        str(document["delivery_id"]),
                        int(document["attempt_ordinal"]),
                        str(document["lease_token"]),
                    )
                    if key not in self._handled:
                        await _maybe_await(self._on_notification(document))
                        self._handled[key] = None
                        if len(self._handled) > MAX_HANDLED_NOTIFICATION_KEYS:
                            self._handled.pop(next(iter(self._handled)))
                    ack_status = await self._post_ack(session, document, timeout=timeout)
                    if ack_status not in {"acknowledged", "duplicate"}:
                        raise DesktopAgentProtocolError("Desktop Agent ACK имеет неизвестный статус.")
                    self._write_cursor(profile_id, event.event_id)
                    latest = event.event_id
                return latest
        except DesktopAgentClientError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise DesktopAgentTransportError("Desktop Agent HTTPS transport недоступен.") from exc
        finally:
            if own_session:
                await session.close()

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        tasks = [
            asyncio.create_task(self._run_profile_forever(profile_id, stop_event))
            for profile_id in sorted(self.config.credential.profiles)
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_profile_forever(
        self, profile_id: str, stop_event: asyncio.Event
    ) -> None:
        delay = CLIENT_RECONNECT_MIN_SECONDS
        while not stop_event.is_set():
            try:
                await self.run_once(profile_id)
                delay = CLIENT_RECONNECT_MIN_SECONDS
                await _wait_or_stop(stop_event, delay)
            except DesktopAgentProtocolError:
                raise
            except (TimeoutError, DesktopAgentTransportError, OSError):
                await _wait_or_stop(stop_event, delay)
                delay = min(CLIENT_RECONNECT_MAX_SECONDS, delay * 2)

    async def _post_ack(
        self,
        session: aiohttp.ClientSession,
        document: Mapping[str, object],
        *,
        timeout: aiohttp.ClientTimeout,
    ) -> str:
        body = {
            "delivery_id": str(document["delivery_id"]),
            "event_id": str(document["event_id"]),
            "event_source": document["event_source"],
            "profile_id": document["profile_id"],
            "attempt_ordinal": document["attempt_ordinal"],
            "lease_token": str(document["lease_token"]),
            "session_epoch": str(document["session_epoch"]),
            "payload_digest": document["payload_digest"],
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.credential.token}",
        }
        try:
            async with session.post(
                self._endpoint(DESKTOP_AGENT_ACK_PATH),
                json=body,
                headers=headers,
                timeout=timeout,
                ssl=True,
            ) as response:
                if response.status != 200:
                    if response.status in {400, 401, 403, 409}:
                        raise DesktopAgentProtocolError("Desktop Agent ACK отклонён сервером.")
                    raise DesktopAgentTransportError("Desktop Agent ACK временно недоступен.")
                payload = await response.json()
        except DesktopAgentClientError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
            raise DesktopAgentTransportError("Desktop Agent ACK transport недоступен.") from exc
        if not isinstance(payload, Mapping) or set(payload) != {"status"}:
            raise DesktopAgentProtocolError("Desktop Agent ACK response имеет неверную структуру.")
        status = payload.get("status")
        if not isinstance(status, str):
            raise DesktopAgentProtocolError("Desktop Agent ACK response имеет неверный статус.")
        return status

    def _endpoint(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def _read_cursor(self, profile_id: str) -> str | None:
        path = self.config.cursor_file
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DesktopAgentClientError("Cursor file недоступен.") from exc
        if len(raw) > MAX_CURSOR_FILE_BYTES:
            raise DesktopAgentProtocolError("Cursor file превысил bounded размер.")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DesktopAgentProtocolError("Cursor file имеет неверный формат.") from None
        if not isinstance(document, Mapping) or set(document) != {"v", "profiles"}:
            raise DesktopAgentProtocolError("Cursor file имеет неизвестные поля.")
        profiles = document.get("profiles")
        if document.get("v") != 1 or not isinstance(profiles, Mapping):
            raise DesktopAgentProtocolError("Cursor file имеет неверную версию.")
        value = profiles.get(profile_id)
        if value is None:
            return None
        if not isinstance(value, str):
            raise DesktopAgentProtocolError("Cursor file содержит неверное значение.")
        try:
            NotificationCursor.decode(value, expected_profile_id=profile_id)
        except NotificationCursorError as exc:
            raise DesktopAgentProtocolError("Cursor file содержит некорректный cursor.") from exc
        return value

    def _write_cursor(self, profile_id: str, cursor: str) -> None:
        current: dict[str, object] = {"v": 1, "profiles": {}}
        path = self.config.cursor_file
        try:
            if path.exists():
                raw = path.read_bytes()
                if len(raw) > MAX_CURSOR_FILE_BYTES:
                    raise DesktopAgentProtocolError("Cursor file превысил bounded размер.")
                loaded = json.loads(raw.decode("utf-8"))
                if not isinstance(loaded, Mapping) or set(loaded) != {"v", "profiles"}:
                    raise DesktopAgentProtocolError("Cursor file имеет неизвестные поля.")
                if loaded.get("v") != 1 or not isinstance(loaded.get("profiles"), Mapping):
                    raise DesktopAgentProtocolError("Cursor file имеет неверную версию.")
                loaded_profiles = loaded["profiles"]
                for stored_profile, stored_cursor in loaded_profiles.items():
                    if not isinstance(stored_profile, str) or not isinstance(stored_cursor, str):
                        raise DesktopAgentProtocolError(
                            "Cursor file содержит неверную profile identity."
                        )
                    try:
                        NotificationCursor.decode(
                            stored_cursor, expected_profile_id=stored_profile
                        )
                    except NotificationCursorError as exc:
                        raise DesktopAgentProtocolError(
                            "Cursor file содержит некорректный cursor."
                        ) from exc
                current["profiles"] = dict(loaded_profiles)
        except DesktopAgentProtocolError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DesktopAgentClientError("Cursor file невозможно обновить.") from exc
        try:
            NotificationCursor.decode(cursor, expected_profile_id=profile_id)
        except NotificationCursorError as exc:
            raise DesktopAgentProtocolError("Cursor имеет некорректную identity.") from exc
        profiles = current["profiles"]
        assert isinstance(profiles, dict)
        profiles[profile_id] = cursor
        encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_CURSOR_FILE_BYTES:
            raise DesktopAgentProtocolError("Cursor file превысил bounded размер.")
        try:
            atomic_write(str(path), encoded)
        except OSError as exc:
            raise DesktopAgentClientError("Cursor file невозможно сохранить атомарно.") from exc


async def _maybe_await(value: Awaitable[None] | None) -> None:
    if inspect.isawaitable(value):
        await value


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


__all__ = [
    "DesktopAgentClient",
    "DesktopAgentClientConfig",
    "DesktopAgentClientError",
    "DesktopAgentProtocolError",
    "DesktopAgentTransportError",
    "SSEEvent",
    "iter_sse_events",
]
