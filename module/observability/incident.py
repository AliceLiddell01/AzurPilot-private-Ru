"""Локальный fail-open incident store с application correlation metadata."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from module.observability._shared import (
    _exception_type_name,
    _metric_label,
    _profile_label,
)
from module.logging_core import sanitize_log_text
from module.observability.scheduler import get_current_task_name
from module.observability.tracing import (
    TraceCorrelation,
    get_current_trace_context,
)

_INCIDENT_SCHEMA_VERSION = 1
_MAX_COLLISION_ATTEMPTS = 10_000
_FILENAME_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_FILENAME_COMPONENT_LIMIT = 128
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_INCIDENT_LOG_MAX_BYTES = 64 * 1024
_INCIDENT_LOG_MAX_LINES = 200
_LEGACY_INCIDENT_DIRECTORY_RE = re.compile(r"\d+")
_CURRENT_INCIDENT_TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.\d{3})(?:_|$)"
)
_INCIDENT_COLLISION_SUFFIX_RE = re.compile(r"_(?P<collision>\d{3,})$")


@dataclass(frozen=True, slots=True)
class IncidentMetadata:
    """Канонический serializable contract локального incident bundle."""

    schema_version: int
    timestamp_utc: str
    profile: str
    task: str | None
    exception_type: str
    trace_id: str | None
    span_id: str | None

    def to_dict(self) -> dict[str, object]:
        """Вернуть независимый JSON-compatible snapshot metadata."""
        return asdict(self)


def _utc_timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Время incident должно содержать timezone information")
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def incident_directory_time_key(name: str) -> tuple[int, int] | None:
    """Вернуть хронологический ключ canonical или legacy incident-каталога."""
    if _LEGACY_INCIDENT_DIRECTORY_RE.fullmatch(name):
        return int(name), 0

    timestamp_match = _CURRENT_INCIDENT_TIMESTAMP_RE.match(name)
    if timestamp_match is None:
        return None
    try:
        timestamp = datetime.strptime(
            timestamp_match.group("timestamp"),
            "%Y-%m-%d_%H-%M-%S.%f",
        ).replace(tzinfo=timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = timestamp - epoch
        timestamp_millis = (
            delta.days * 86_400_000
            + delta.seconds * 1_000
            + delta.microseconds // 1_000
        )
        collision_match = _INCIDENT_COLLISION_SUFFIX_RE.search(name)
        collision = int(collision_match.group("collision")) if collision_match else 0
    except (OverflowError, TypeError, ValueError):
        return None
    return timestamp_millis, collision


def _exception_identity(exception: BaseException | None) -> str:
    if not isinstance(exception, BaseException):
        return "UnknownException"
    return _exception_type_name(type(exception), exception)


def _filename_component(value: str) -> str:
    component = _FILENAME_COMPONENT_RE.sub("_", value).strip(" .")
    if not component or component in {".", ".."}:
        return "Exception"
    return component[:_FILENAME_COMPONENT_LIMIT]


def _correlation_id(value: object, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        return None
    return value


def build_incident_metadata(
    *,
    profile: object,
    exception: BaseException | None,
    timestamp: datetime | None = None,
    task: object | None = None,
    correlation: TraceCorrelation | None = None,
) -> IncidentMetadata:
    """Собрать metadata без raw exception message, path и OTel objects."""
    current_time = _utc_timestamp(timestamp)
    current_task = get_current_task_name() if task is None else task
    task_name = None if current_task is None else _metric_label(current_task)
    current_correlation = (
        get_current_trace_context() if correlation is None else correlation
    )
    return IncidentMetadata(
        schema_version=_INCIDENT_SCHEMA_VERSION,
        timestamp_utc=_format_timestamp(current_time),
        profile=_profile_label(profile),
        task=task_name,
        exception_type=_exception_identity(exception),
        trace_id=_correlation_id(
            current_correlation.trace_id if current_correlation is not None else None,
            _TRACE_ID_RE,
        ),
        span_id=_correlation_id(
            current_correlation.span_id if current_correlation is not None else None,
            _SPAN_ID_RE,
        ),
    )


def create_incident_directory(
    error_root: Path | str,
    *,
    profile: object,
    exception: BaseException | None,
    timestamp: datetime | None = None,
) -> tuple[Path, datetime]:
    """Атомарно создать читаемый и collision-safe каталог incident-а."""
    current_time = _utc_timestamp(timestamp)
    profile_dir = Path(error_root) / _profile_label(profile)
    profile_dir.mkdir(parents=True, exist_ok=True)
    base_name = (
        f"{current_time.strftime('%Y-%m-%d_%H-%M-%S.%f')[:-3]}_"
        f"{_filename_component(_exception_identity(exception))}"
    )
    for attempt in range(_MAX_COLLISION_ATTEMPTS):
        suffix = "" if attempt == 0 else f"_{attempt:03d}"
        candidate = profile_dir / f"{base_name}{suffix}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate, current_time
    raise FileExistsError("Не удалось подобрать свободный каталог incident-а")


def write_incident_metadata(
    folder: Path | str,
    metadata: IncidentMetadata,
) -> Path:
    """Атомарно записать ``incident.json`` и вернуть его путь."""
    target = Path(folder) / "incident.json"
    temporary_path: str | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".incident-",
            suffix=".tmp",
            dir=target.parent,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            json.dump(
                metadata.to_dict(),
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        return target
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def write_incident_log(
    folder: Path | str,
    lines: Iterable[object],
    *,
    max_bytes: int = _INCIDENT_LOG_MAX_BYTES,
    max_lines: int = _INCIDENT_LOG_MAX_LINES,
) -> Path:
    """Атомарно сохранить bounded sanitized context реального incident-а."""
    if max_bytes <= 0 or max_lines <= 0:
        raise ValueError("Ограничения incident log должны быть положительными")
    target = Path(folder) / "log.txt"
    output: list[str] = []
    used_bytes = 0
    for line in lines:
        if len(output) >= max_lines:
            break
        text = (
            sanitize_log_text(line)
            .replace("\r\n", " ")
            .replace("\r", " ")
            .replace("\n", " ")
            .rstrip("\r\n")
        )
        encoded = (text + "\n").encode("utf-8", errors="replace")
        remaining = max_bytes - used_bytes
        if remaining <= 0:
            break
        if len(encoded) > remaining:
            if remaining <= 1:
                break
            text = encoded[: remaining - 1].decode("utf-8", errors="ignore").rstrip(
                "\r\n"
            )
            encoded = (text + "\n").encode("utf-8", errors="replace")
            while len(encoded) > remaining and text:
                text = text[:-1]
                encoded = (text + "\n").encode("utf-8", errors="replace")
        if not text:
            continue
        encoded = (text + "\n").encode("utf-8", errors="replace")
        output.append(text)
        used_bytes += len(encoded)
        if used_bytes >= max_bytes:
            break

    temporary_path: str | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".incident-log-",
            suffix=".tmp",
            dir=target.parent,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write("\n".join(output))
            if output:
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        return target
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


__all__ = (
    "IncidentMetadata",
    "build_incident_metadata",
    "create_incident_directory",
    "incident_directory_time_key",
    "write_incident_log",
    "write_incident_metadata",
)
