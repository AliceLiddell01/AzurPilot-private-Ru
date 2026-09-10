"""Небольшой outbound-only SSE/ACK client для Desktop Agent."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp

from deploy.atomic import atomic_write
from module.application.notifications.agent import (
    DESKTOP_AGENT_ACK_PATH,
    DESKTOP_AGENT_STREAM_PATH,
    MAX_AGENT_BATCH_SIZE,
    MAX_AGENT_PROFILES,
    RECOVERABLE_AGENT_ACK_REASONS,
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
_CURSOR_V1_FIELDS = frozenset({"v", "profiles"})
_CURSOR_V2_FIELDS = frozenset({"v", "profiles", "presented"})
_SAFE_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class DesktopAgentClientError(RuntimeError):
    """Ошибка client-side протокола без raw response/credential в тексте."""


class DesktopAgentProtocolError(DesktopAgentClientError):
    """Сервер или SSE frame нарушил bounded contract."""


class DesktopAgentTransportError(DesktopAgentClientError):
    """Временная ошибка исходящего HTTPS соединения."""


class DesktopAgentRecoverableAckError(DesktopAgentClientError):
    """Ожидаемый stale ACK, после которого профиль должен переподключиться."""

    def __init__(self, reason: str) -> None:
        if reason not in RECOVERABLE_AGENT_ACK_REASONS:
            raise ValueError("Причина ACK не разрешена для recoverable retry.")
        self.reason = reason
        super().__init__("Desktop Agent ACK требует bounded reconnect.")


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
    # Незавершённый frame без пустой строки отбрасывается при закрытии SSE.


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
        telemetry: object | None = None,
    ) -> None:
        self.config = config
        if not callable(on_notification):
            raise TypeError("on_notification должен быть callable.")
        self._on_notification = on_notification
        self._telemetry = telemetry
        self._cursor_lock = threading.Lock()

    async def run_once(self, profile_id: str, *, session: aiohttp.ClientSession | None = None) -> str | None:
        if profile_id not in self.config.credential.profiles:
            raise DesktopAgentProtocolError("Профиль отсутствует в Agent scope.")
        cursor, presented_delivery_id = await asyncio.to_thread(
            self._read_profile_state, profile_id
        )
        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {self.config.credential.token}",
            "Accept-Encoding": "identity",
        }
        if cursor is not None:
            headers["Last-Event-ID"] = cursor
        own_session = session is None
        connector: aiohttp.TCPConnector | None = None
        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=self.config.connect_timeout_seconds,
            sock_read=self.config.read_timeout_seconds,
        )
        try:
            if own_session:
                connector = aiohttp.TCPConnector(ssl=True)
                session = aiohttp.ClientSession(
                    connector=connector,
                    auto_decompress=False,
                )
            if session is None:
                raise DesktopAgentClientError("Desktop Agent HTTPS session не создана.")
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
                content_encoding = response.headers.get("Content-Encoding", "")
                if content_encoding and content_encoding.casefold() != "identity":
                    raise DesktopAgentProtocolError(
                        "Desktop Agent stream имеет неподдерживаемое сжатие."
                    )
                self._record_agent_connection("started")
                latest = cursor
                saw_notification = False
                async for event in iter_sse_events(response.content):
                    if event.event != "notification":
                        continue
                    saw_notification = True
                    if event.event_id is None or not event.data:
                        raise DesktopAgentProtocolError("SSE notification frame не имеет id/data.")
                    try:
                        document = validate_agent_delivery_document(json.loads(event.data))
                        frame_cursor = NotificationCursor.decode(
                            event.event_id, expected_profile_id=profile_id
                        )
                    except (ValueError, TypeError):
                        raise DesktopAgentProtocolError("SSE notification data не является корректным JSON.") from None
                    if (
                        frame_cursor is None
                        or frame_cursor.event_id != document["event_id"]
                        or frame_cursor.profile_sequence != document["profile_sequence"]
                    ):
                        raise DesktopAgentProtocolError("SSE id не совпадает с notification identity.")
                    if document["profile_id"] != profile_id:
                        raise DesktopAgentProtocolError("SSE notification profile не совпадает с запросом.")
                    if cursor is not None:
                        saved_cursor = NotificationCursor.decode(
                            cursor, expected_profile_id=profile_id
                        )
                        if saved_cursor is None:
                            raise DesktopAgentProtocolError(
                                "Сохранённый cursor не имеет корректной identity."
                            )
                        if (
                            frame_cursor.profile_sequence < saved_cursor.profile_sequence
                            or (
                                frame_cursor.profile_sequence == saved_cursor.profile_sequence
                                and frame_cursor.event_id != saved_cursor.event_id
                            )
                        ):
                            raise DesktopAgentProtocolError(
                                "SSE cursor движется назад или имеет неверный tie-break."
                            )
                    delivery_id = str(document["delivery_id"])
                    if delivery_id != presented_delivery_id:
                        try:
                            await _maybe_await(self._on_notification(document))
                        except DesktopAgentClientError:
                            raise
                        except Exception as exc:
                            raise DesktopAgentTransportError(
                                "Локальная презентация Desktop Agent недоступна."
                            ) from exc
                        await asyncio.to_thread(
                            self._mark_presented, profile_id, delivery_id
                        )
                        presented_delivery_id = delivery_id
                    ack_status = await self._post_ack(session, document, timeout=timeout)
                    if ack_status not in {"acknowledged", "duplicate"}:
                        raise DesktopAgentProtocolError("Desktop Agent ACK имеет неизвестный статус.")
                    await asyncio.to_thread(
                        self._write_cursor, profile_id, event.event_id
                    )
                    latest = event.event_id
                self._record_agent_backlog("available" if saw_notification else "empty")
                return latest
        except DesktopAgentClientError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise DesktopAgentTransportError("Desktop Agent HTTPS transport недоступен.") from exc
        finally:
            if own_session:
                if session is not None:
                    await session.close()
                elif connector is not None:
                    await connector.close()

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
            except DesktopAgentRecoverableAckError:
                self._record_agent_reconnect()
                await _wait_or_stop(stop_event, delay)
                delay = min(CLIENT_RECONNECT_MAX_SECONDS, delay * 2)
            except (TimeoutError, DesktopAgentTransportError, OSError):
                self._record_agent_connection("unavailable")
                self._record_agent_reconnect()
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
            "Accept-Encoding": "identity",
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
                    if response.status == 409:
                        try:
                            payload = await response.json()
                        except (aiohttp.ClientError, ValueError) as exc:
                            raise DesktopAgentProtocolError(
                                "Desktop Agent ACK response имеет невалидный JSON."
                            ) from exc
                        if (
                            not isinstance(payload, Mapping)
                            or set(payload) != {"status", "reason"}
                            or payload.get("status") != "rejected"
                            or not isinstance(payload.get("reason"), str)
                            or payload["reason"] not in RECOVERABLE_AGENT_ACK_REASONS
                        ):
                            raise DesktopAgentProtocolError(
                                "Desktop Agent ACK response имеет неверную stale-структуру."
                            )
                        self._record_agent_ack("rejected")
                        raise DesktopAgentRecoverableAckError(payload["reason"])
                    if response.status in {400, 401, 403}:
                        raise DesktopAgentProtocolError("Desktop Agent ACK отклонён сервером.")
                    raise DesktopAgentTransportError("Desktop Agent ACK временно недоступен.")
                try:
                    payload = await response.json()
                except (aiohttp.ClientError, ValueError) as exc:
                    raise DesktopAgentProtocolError(
                        "Desktop Agent ACK response имеет невалидный JSON."
                    ) from exc
        except DesktopAgentClientError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise DesktopAgentTransportError("Desktop Agent ACK transport недоступен.") from exc
        if not isinstance(payload, Mapping) or set(payload) != {"status"}:
            raise DesktopAgentProtocolError("Desktop Agent ACK response имеет неверную структуру.")
        status = payload.get("status")
        if not isinstance(status, str):
            raise DesktopAgentProtocolError("Desktop Agent ACK response имеет неверный статус.")
        if status not in {"acknowledged", "duplicate"}:
            raise DesktopAgentProtocolError("Desktop Agent ACK response имеет неизвестный статус.")
        self._record_agent_ack(status)
        return status

    def _record_agent_connection(self, status: str) -> None:
        _record_client_telemetry(self._telemetry, "record_agent_connection", status=status)

    def _record_agent_ack(self, status: str) -> None:
        _record_client_telemetry(self._telemetry, "record_agent_ack", status=status)

    def _record_agent_backlog(self, status: str) -> None:
        _record_client_telemetry(self._telemetry, "record_agent_backlog", status=status)

    def _record_agent_reconnect(self) -> None:
        _record_client_telemetry(self._telemetry, "record_agent_reconnect")

    def _endpoint(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def _read_cursor(self, profile_id: str) -> str | None:
        return self._read_profile_state(profile_id)[0]

    def _read_profile_state(self, profile_id: str) -> tuple[str | None, str | None]:
        with self._cursor_lock:
            document = self._load_cursor_document()
            profiles = cast(dict[str, str], document["profiles"])
            presented = cast(dict[str, str], document["presented"])
            value = profiles.get(profile_id)
            presented_delivery_id = presented.get(profile_id)
            return value, presented_delivery_id

    def _write_cursor(self, profile_id: str, cursor: str) -> None:
        try:
            NotificationCursor.decode(cursor, expected_profile_id=profile_id)
        except NotificationCursorError as exc:
            raise DesktopAgentProtocolError("Cursor имеет некорректную identity.") from exc
        with self._cursor_lock:
            current = self._load_cursor_document()
            profiles = cast(dict[str, str], current["profiles"])
            previous = profiles.get(profile_id)
            if previous is not None:
                previous_cursor = NotificationCursor.decode(
                    previous, expected_profile_id=profile_id
                )
                next_cursor = NotificationCursor.decode(
                    cursor, expected_profile_id=profile_id
                )
                if previous_cursor is None or next_cursor is None:
                    raise DesktopAgentProtocolError(
                        "Сохранённый cursor не имеет корректной identity."
                    )
                if (
                    next_cursor.profile_sequence < previous_cursor.profile_sequence
                    or (
                        next_cursor.profile_sequence == previous_cursor.profile_sequence
                        and next_cursor.event_id != previous_cursor.event_id
                    )
                ):
                    raise DesktopAgentProtocolError("Cursor не может двигаться назад.")
            profiles[profile_id] = cursor
            self._save_cursor_document(current)

    def _mark_presented(self, profile_id: str, delivery_id: str) -> None:
        try:
            UUID(delivery_id)
        except (TypeError, ValueError):
            raise DesktopAgentProtocolError("Presentation identity имеет неверный UUID.") from None
        if _SAFE_PROFILE_RE.fullmatch(profile_id) is None:
            raise DesktopAgentProtocolError("Presentation profile имеет неверный формат.")
        with self._cursor_lock:
            current = self._load_cursor_document()
            presented = cast(dict[str, str], current["presented"])
            presented[profile_id] = delivery_id
            self._save_cursor_document(current)

    def _load_cursor_document(self) -> dict[str, object]:
        path = self.config.cursor_file
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"v": 2, "profiles": {}, "presented": {}}
        except OSError as exc:
            raise DesktopAgentClientError("Cursor file недоступен.") from exc
        if len(raw) > MAX_CURSOR_FILE_BYTES:
            raise DesktopAgentProtocolError("Cursor file превысил bounded размер.")
        try:
            loaded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DesktopAgentProtocolError("Cursor file имеет неверный формат.") from None
        if not isinstance(loaded, Mapping):
            raise DesktopAgentProtocolError("Cursor file имеет неверную структуру.")
        keys = frozenset(loaded)
        version = loaded.get("v")
        profiles = loaded.get("profiles")
        if keys == _CURSOR_V1_FIELDS and version == 1:
            presented: Mapping[object, object] = {}
        elif keys == _CURSOR_V2_FIELDS and version == 2:
            presented = loaded.get("presented")
            if not isinstance(presented, Mapping):
                raise DesktopAgentProtocolError("Cursor file имеет неверную presentation map.")
        else:
            raise DesktopAgentProtocolError("Cursor file имеет неизвестные поля или версию.")
        if not isinstance(profiles, Mapping) or len(profiles) > MAX_AGENT_PROFILES:
            raise DesktopAgentProtocolError("Cursor file имеет неверный profile scope.")
        normalized_profiles: dict[str, str] = {}
        for stored_profile, stored_cursor in profiles.items():
            if (
                not isinstance(stored_profile, str)
                or _SAFE_PROFILE_RE.fullmatch(stored_profile) is None
                or not isinstance(stored_cursor, str)
            ):
                raise DesktopAgentProtocolError("Cursor file содержит неверную profile identity.")
            try:
                NotificationCursor.decode(
                    stored_cursor, expected_profile_id=stored_profile
                )
            except NotificationCursorError as exc:
                raise DesktopAgentProtocolError(
                    "Cursor file содержит некорректный cursor."
                ) from exc
            normalized_profiles[stored_profile] = stored_cursor
        if not isinstance(presented, Mapping) or len(presented) > MAX_AGENT_PROFILES:
            raise DesktopAgentProtocolError("Cursor file имеет неверный presentation scope.")
        normalized_presented: dict[str, str] = {}
        for stored_profile, delivery_id in presented.items():
            if (
                not isinstance(stored_profile, str)
                or _SAFE_PROFILE_RE.fullmatch(stored_profile) is None
                or not isinstance(delivery_id, str)
            ):
                raise DesktopAgentProtocolError("Cursor file содержит неверную presentation identity.")
            try:
                UUID(delivery_id)
            except ValueError:
                raise DesktopAgentProtocolError(
                    "Cursor file содержит некорректную presentation identity."
                ) from None
            normalized_presented[stored_profile] = delivery_id
        return {"v": 2, "profiles": normalized_profiles, "presented": normalized_presented}

    def _save_cursor_document(self, document: Mapping[str, object]) -> None:
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_CURSOR_FILE_BYTES:
            raise DesktopAgentProtocolError("Cursor file превысил bounded размер.")
        try:
            atomic_write(str(self.config.cursor_file), encoded)
        except OSError as exc:
            raise DesktopAgentClientError("Cursor file невозможно сохранить атомарно.") from exc


class DesktopAgentClientRuntime:
    """Управляет outbound Agent client в существующем процессе WebUI."""

    def __init__(
        self,
        config: DesktopAgentClientConfig,
        *,
        on_notification: Callable[[Mapping[str, object]], Awaitable[None] | None],
        telemetry: object | None = None,
        client_factory: Callable[..., DesktopAgentClient] = DesktopAgentClient,
    ) -> None:
        if not callable(client_factory):
            raise TypeError("client_factory должен быть callable.")
        self._client = client_factory(
            config,
            on_notification=on_notification,
            telemetry=telemetry,
        )
        self._telemetry = telemetry
        self._thread: threading.Thread | None = None
        self._loop = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = threading.Event()
        self._error: str | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def fatal_error(self) -> str | None:
        return self._error

    def start(self) -> None:
        if self.running:
            return
        self._stop_requested.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name="azurpilot-notification-agent-client",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None:
            def request_stop() -> None:
                stop_event.set()
                task = self._task
                if task is not None and not task.done():
                    task.cancel()

            try:
                loop.call_soon_threadsafe(request_stop)
            except RuntimeError:
                # Event loop уже закрыт: клиентский поток завершился самостоятельно.
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        if thread is None or not thread.is_alive():
            self._thread = None

    def _run(self) -> None:
        async def runner() -> None:
            self._loop = asyncio.get_running_loop()
            self._stop_event = asyncio.Event()
            self._task = asyncio.current_task()
            if self._stop_requested.is_set():
                self._stop_event.set()
            try:
                await self._client.run_forever(stop_event=self._stop_event)
            finally:
                self._task = None
                self._stop_event = None
                self._loop = None

        try:
            asyncio.run(runner())
        except asyncio.CancelledError:
            if not self._stop_requested.is_set():
                self._error = "client_runtime_cancelled"
                _record_client_telemetry(
                    self._telemetry, "record_agent_connection", status="unavailable"
                )
        except DesktopAgentClientError as exc:
            self._error = type(exc).__name__
            _record_client_telemetry(
                self._telemetry, "record_agent_connection", status="unavailable"
            )
        except Exception:
            self._error = "client_runtime_failed"
            _record_client_telemetry(
                self._telemetry, "record_agent_connection", status="unavailable"
            )


def present_desktop_agent_notification(document: Mapping[str, object]) -> None:
    """Передать уже проверенную frame в существующее локальное WebUI представление."""

    from module.notify.notify import notify_webui

    result = notify_webui(
        str(document["profile_id"]),
        str(document["title"]),
        str(document["body"]),
    )
    if result is not True:
        raise DesktopAgentTransportError("Локальное WebUI представление недоступно.")


def _record_client_telemetry(
    telemetry: object | None, method_name: str, **kwargs: object
) -> None:
    method = getattr(telemetry, method_name, None)
    if not callable(method):
        return
    try:
        method(**kwargs)
    except Exception:
        return


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
    "DesktopAgentClientRuntime",
    "DesktopAgentProtocolError",
    "DesktopAgentRecoverableAckError",
    "DesktopAgentTransportError",
    "SSEEvent",
    "iter_sse_events",
    "present_desktop_agent_notification",
]
