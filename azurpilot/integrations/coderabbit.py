"""Прямой bounded CodeRabbit agent через явно выбранный WSL2 checkout."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from azurpilot.tooling.contracts import (
    CodeRabbitFinding,
    FindingDisposition,
    FindingSeverity,
    ResultCode,
)
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import (
    ScopedPath,
    StateLayout,
    bounded_read_text,
)
from azurpilot.tooling.git import GitClient
from azurpilot.tooling.process import ProcessSpec, StructuredProcessRunner

from .adapters import (
    AdapterOutcome,
    IntegrationAdapter,
    build_evidence,
    build_record,
)
from .config import IntegrationConfig
from .contracts import (
    MAX_RETAINED_REVIEW_CYCLES,
    MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE,
    CodeRabbitCycleSummary,
    CredentialRef,
    IntegrationFinding,
    IntegrationName,
    IntegrationRecord,
    IntegrationState,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VERSION_OUTPUT_RE = re.compile(
    r"(?<![A-Za-z0-9])v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?(?![A-Za-z0-9])"
)
_MAX_REVIEW_BYTES = 4 * 1024 * 1024
_MAX_REVIEW_LINES = 512
_MAX_STATE_BYTES = 32 * 1024
REVIEW_STATE_SCHEMA_VERSION = 2
CODERABBIT_STATE_SCHEMA = REVIEW_STATE_SCHEMA_VERSION
_MAX_REVIEW_ATTEMPTS = 128
_RATE_LIMIT_MAX_SECONDS = 7 * 24 * 60 * 60
_HEARTBEAT_INTERVAL_SECONDS = 15.0
_STATE_FILE_NAME = "coderabbit-review.json"
_CYCLE_ID_RE = re.compile(r"^(?:coderabbit-cycle|legacy-coderabbit)-[0-9a-f]{16,64}$|^not-started$")
_RETRY_SOURCES = frozenset({"provider", "unknown"})
_RATE_LIMIT_WAITING = "rate_limited_waiting"
_RATE_LIMIT_RETRY_ALLOWED = "rate_limited_retry_allowed"
_DEFAULT_FINDING_IMPACT = "CodeRabbit finding требует независимой проверки."
_DEFAULT_FINDING_RESOLUTION = "Не применено автоматически; требуется независимая проверка."
_MAX_RETAINED_FINDINGS = 128
_PROVIDER_FINDING_HEADER_RE = re.compile(
    r"^\s*(critical|major|minor|trivial|info)\s+\[[^\]]{1,160}\]\s*$",
    re.IGNORECASE,
)
_PROVIDER_FINDING_LOCATION_RE = re.compile(
    r"^\s*→\s+(.+?):([1-9][0-9]*)(?:-([1-9][0-9]*))?\s*$"
)
_PROVIDER_FINDING_SEPARATOR_RE = re.compile(r"^\s*[─-]{8,}\s*$")


@dataclass(frozen=True, slots=True)
class WslDistribution:
    """Одна запись ``wsl.exe --list --verbose`` без machine-specific данных."""

    name: str
    state: str
    version: int


@dataclass(frozen=True, slots=True)
class WslSelection:
    distribution: str | None
    state: IntegrationState
    reason_code: str


@dataclass(frozen=True, slots=True)
class WslReviewEnvironment:
    """Подтверждённая review-среда без сериализации локальных секретов."""

    distro_name: str
    wsl_version: int
    linux_user: str
    home: str
    review_clone: str
    coderabbit_executable: str
    repository_identity: str | None = None


@dataclass(frozen=True, slots=True)
class _WslCommandResult:
    """Ограниченный результат WSL-команды вместе с признаками усечения."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


def parse_wsl_verbose(output: str) -> tuple[WslDistribution, ...]:
    """Разобрать bounded inventory WSL без угадывания имени distro."""

    if not isinstance(output, str):
        raise TypeError("WSL inventory должен быть строкой")
    entries: list[WslDistribution] = []
    line_pattern = re.compile(
        r"^\s*\*?\s*(?P<name>.+?)\s+(?P<state>[A-Za-z][A-Za-z0-9_-]*)\s+(?P<version>[12])\s*$"
    )
    for raw_line in output.replace("\x00", "").splitlines():
        line = raw_line.strip()
        if not line or line.casefold().startswith("name"):
            continue
        match = line_pattern.fullmatch(line)
        if match is None:
            continue
        name = match.group("name").strip()
        if not _NAME_RE.fullmatch(name):
            continue
        entries.append(
            WslDistribution(
                name=name,
                state=match.group("state"),
                version=int(match.group("version")),
            )
        )
    unique: dict[str, WslDistribution] = {}
    for entry in entries:
        unique.setdefault(entry.name.casefold(), entry)
    return tuple(unique.values())


def select_wsl_distribution(
    output: str, *, configured: str | None = None
) -> WslSelection:
    """Выбрать exact WSL2 distro или вернуть fail-closed status."""

    entries = parse_wsl_verbose(output)
    if configured is not None:
        exact = next((item for item in entries if item.name == configured), None)
        if exact is None:
            return WslSelection(None, IntegrationState.NOT_CONFIGURED, "CODERABBIT_WSL_DISTRIBUTION_NOT_FOUND")
        if exact.version != 2:
            return WslSelection(exact.name, IntegrationState.INCOMPATIBLE, "CODERABBIT_WSL2_REQUIRED")
        return WslSelection(exact.name, IntegrationState.READY, "CODERABBIT_WSL_DISTRIBUTION_SELECTED")
    candidates = tuple(item for item in entries if item.version == 2)
    if not candidates:
        if entries:
            return WslSelection(None, IntegrationState.INCOMPATIBLE, "CODERABBIT_WSL2_REQUIRED")
        return WslSelection(None, IntegrationState.NOT_CONFIGURED, "CODERABBIT_WSL_DISTRIBUTION_NOT_CONFIGURED")
    if len(candidates) > 1:
        return WslSelection(None, IntegrationState.INCOMPATIBLE, "CODERABBIT_WSL_DISTRIBUTION_AMBIGUOUS")
    return WslSelection(candidates[0].name, IntegrationState.READY, "CODERABBIT_WSL_DISTRIBUTION_SELECTED")


class CodeRabbitStreamError(ValueError):
    """Ошибка bounded NDJSON потока без вывода provider payload."""

    def __init__(
        self,
        code: str,
        *,
        rate_limited: bool = False,
        retry_not_before: str | None = None,
        retry_source: str = "unknown",
    ) -> None:
        self.code = code
        self.rate_limited = rate_limited
        self.retry_not_before = retry_not_before
        self.retry_source = retry_source if retry_source in _RETRY_SOURCES else "unknown"
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ParsedCodeRabbitReview:
    findings: tuple[CodeRabbitFinding, ...]
    complete: bool
    unknown_events: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodeRabbitProgress:
    """Bounded операторское событие живого review без provider payload."""

    phase: str
    cycle_id: str
    attempt: int
    substantive_iterations: int
    provider_state: str
    message: str
    elapsed_seconds: int = 0


def _emit_progress(
    callback: Callable[[CodeRabbitProgress], None] | None,
    *,
    phase: str,
    cycle_id: str,
    attempt: int,
    substantive_iterations: int,
    provider_state: str,
    message: str,
    started_monotonic: float | None = None,
) -> None:
    """Передать bounded heartbeat; сбой UI не должен ломать review."""

    if callback is None:
        return
    elapsed = 0
    if started_monotonic is not None:
        elapsed = max(0, int(time.monotonic() - started_monotonic))
    event = CodeRabbitProgress(
        phase=phase[:48],
        cycle_id=cycle_id[:80],
        attempt=max(0, min(attempt, _MAX_REVIEW_ATTEMPTS)),
        substantive_iterations=max(
            0, min(substantive_iterations, MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE)
        ),
        provider_state=provider_state[:80],
        message=_bounded_string(message, "Состояние CodeRabbit обновлено.", 240),
        elapsed_seconds=elapsed,
    )
    try:
        callback(event)
    except Exception:  # noqa: BLE001 - operator presentation is best effort.
        return


def _bounded_string(value: object, default: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return " ".join(value.split())[:limit]


def _parse_provider_retry_metadata(
    event: Mapping[str, object], *, now: datetime | None = None
) -> tuple[str | None, str]:
    """Извлечь только bounded retry metadata из error event.

    Произвольный provider payload намеренно не сохраняется.  Поддерживаются
    только стабильные имена времени сброса и bounded numeric retry-after.
    """

    current = now or datetime.now(UTC)
    containers: list[Mapping[str, object]] = [event]
    for key in ("metadata", "details", "rate_limit"):
        nested = event.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    absolute_keys = (
        "retry_not_before",
        "retry_at",
        "retryAt",
        "reset_at",
        "resetAt",
    )
    for container in containers:
        for key in absolute_keys:
            value = container.get(key)
            if not isinstance(value, str) or len(value) > 128:
                continue
            candidate = value.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                continue
            parsed = parsed.astimezone(UTC)
            if parsed > current + timedelta(seconds=_RATE_LIMIT_MAX_SECONDS):
                continue
            return parsed.isoformat(timespec="seconds"), "provider"
    for container in containers:
        for key in ("retry_after_seconds", "retry_after", "retryAfter"):
            value = container.get(key)
            if isinstance(value, bool):
                continue
            try:
                seconds = int(value) if isinstance(value, (int, float, str)) else -1
            except (TypeError, ValueError, OverflowError):
                seconds = -1
            if 1 <= seconds <= _RATE_LIMIT_MAX_SECONDS:
                retry_at = current + timedelta(seconds=seconds)
                return retry_at.isoformat(timespec="seconds"), "provider"
    return None, "unknown"


def _retry_time_has_arrived(
    retry_not_before: object, *, now: datetime | None = None
) -> bool:
    if not isinstance(retry_not_before, str) or not retry_not_before:
        return False
    candidate = retry_not_before.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return (now or datetime.now(UTC)) >= parsed.astimezone(UTC)


def _finding_path(payload: dict[str, object]) -> str:
    candidates: list[object] = [payload.get("path"), payload.get("fileName"), payload.get("file")]
    location = payload.get("location")
    if isinstance(location, dict):
        candidates.extend((location.get("path"), location.get("file")))
    for value in candidates:
        if not isinstance(value, str) or not value.strip():
            continue
        path = value.strip().replace("\\", "/")
        if (
            path.startswith("/")
            or re.match(r"^[A-Za-z]:/", path)
            or any(part == ".." for part in path.split("/"))
        ):
            raise CodeRabbitStreamError("CODERABBIT_FINDING_PATH_INVALID")
        return path[:512]
    raise CodeRabbitStreamError("CODERABBIT_FINDING_PATH_MISSING")


def _severity(value: object) -> FindingSeverity:
    text = str(value).casefold().strip() if value is not None else "info"
    return {
        "critical": FindingSeverity.CRITICAL,
        "blocker": FindingSeverity.CRITICAL,
        "high": FindingSeverity.MAJOR,
        "major": FindingSeverity.MAJOR,
        "warning": FindingSeverity.MINOR,
        "medium": FindingSeverity.MINOR,
        "minor": FindingSeverity.MINOR,
        "low": FindingSeverity.TRIVIAL,
        "trivial": FindingSeverity.TRIVIAL,
    }.get(text, FindingSeverity.INFO)


def _disposition(value: object) -> FindingDisposition:
    text = " ".join(str(value or "").casefold().replace("_", " ").split())
    return {
        "confirmed": FindingDisposition.CONFIRMED,
        "partially confirmed": FindingDisposition.PARTIALLY_CONFIRMED,
        "partial": FindingDisposition.PARTIALLY_CONFIRMED,
        "false positive": FindingDisposition.FALSE_POSITIVE,
        "false": FindingDisposition.FALSE_POSITIVE,
        "insufficient evidence": FindingDisposition.INSUFFICIENT_EVIDENCE,
        "insufficient": FindingDisposition.INSUFFICIENT_EVIDENCE,
    }.get(text, FindingDisposition.INSUFFICIENT_EVIDENCE)


def _finding_text(payload: Mapping[str, object], *keys: str) -> object:
    """Выбрать первый bounded текст из версий provider finding schema."""

    nested_keys = ("message", "body", "comment", "text", "title", "description")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            for nested_key in nested_keys:
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str) and nested_value.strip():
                    return nested_value
        elif isinstance(value, str) and value.strip():
            return value
    return None


def _parse_finding(raw: object) -> CodeRabbitFinding:
    payload = raw if isinstance(raw, dict) else {}
    path = _finding_path(payload)
    impact = _bounded_string(
        _finding_text(
            payload,
            "impact",
            "message",
            "comment",
            "title",
            "description",
            "issue",
            "body",
            "explanation",
            "rationale",
            "problem",
            "details",
            "text",
        ),
        _DEFAULT_FINDING_IMPACT,
        1200,
    )
    disposition = _disposition(
        payload.get("disposition") or payload.get("classification")
    )
    resolution = _bounded_string(
        _finding_text(
            payload,
            "resolution",
            "recommendation",
            "suggested_fix",
            "suggestion",
            "fix",
            "proposed_fix",
            "action",
        ),
        _DEFAULT_FINDING_RESOLUTION,
        1200,
    )
    title = _finding_text(payload, "title", "category", "rule")
    return CodeRabbitFinding(
        severity=_severity(payload.get("severity") or payload.get("priority")),
        path=path,
        title=_bounded_string(title, "", 160) or None,
        impact=impact,
        disposition=disposition,
        resolution=resolution,
    )


def _finding_event_payload(event: dict[str, object]) -> object:
    nested = event.get("finding")
    if not isinstance(nested, dict):
        return nested if nested is not None else event
    payload = {key: value for key, value in event.items() if key != "finding"}
    payload.update(nested)
    return payload


def _complete_findings(
    event: dict[str, object], findings: list[CodeRabbitFinding]
) -> tuple[CodeRabbitFinding, ...]:
    """Разобрать findings из complete, не смешивая список с его счётчиком."""

    nested = event.get("findings")
    if nested is None:
        return ()
    if isinstance(nested, bool):
        raise CodeRabbitStreamError("CODERABBIT_FINDINGS_INVALID")
    if isinstance(nested, int):
        if nested > 128:
            raise CodeRabbitStreamError("CODERABBIT_FINDINGS_TOO_LARGE")
        if nested < 0 or nested != len(findings):
            raise CodeRabbitStreamError("CODERABBIT_FINDINGS_COUNT_MISMATCH")
        return ()
    if isinstance(nested, dict):
        count = nested.get("count")
        items = nested.get("items", nested.get("findings"))
        if items is None and isinstance(count, int) and not isinstance(count, bool):
            if count > 128:
                raise CodeRabbitStreamError("CODERABBIT_FINDINGS_TOO_LARGE")
            if count < 0 or count != len(findings):
                raise CodeRabbitStreamError("CODERABBIT_FINDINGS_COUNT_MISMATCH")
            return ()
        nested = items
    if not isinstance(nested, list):
        raise CodeRabbitStreamError("CODERABBIT_FINDINGS_INVALID")
    if len(findings) + len(nested) > 128:
        raise CodeRabbitStreamError("CODERABBIT_FINDINGS_TOO_LARGE")
    return tuple(_parse_finding(item) for item in nested)


def parse_agent_ndjson(lines: Iterable[str]) -> ParsedCodeRabbitReview:
    """Строго разобрать официальный agent NDJSON stream.

    Provider suggestions/codegen instructions становятся обычным untrusted
    text и никогда не исполняются этим адаптером.
    """

    findings: list[CodeRabbitFinding] = []
    unknown: list[str] = []
    complete = False
    total_bytes = 0
    line_count = 0
    for raw_line in lines:
        line_count += 1
        if line_count > _MAX_REVIEW_LINES:
            raise CodeRabbitStreamError("CODERABBIT_STREAM_TOO_LARGE")
        if not isinstance(raw_line, str):
            raise CodeRabbitStreamError("CODERABBIT_STREAM_LINE_INVALID")
        total_bytes += len(raw_line.encode("utf-8", errors="replace"))
        if total_bytes > _MAX_REVIEW_BYTES:
            raise CodeRabbitStreamError("CODERABBIT_STREAM_TOO_LARGE")
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CodeRabbitStreamError("CODERABBIT_NDJSON_INVALID") from exc
        if not isinstance(event, dict):
            raise CodeRabbitStreamError("CODERABBIT_EVENT_INVALID")
        kind = event.get("type") or event.get("event") or event.get("kind")
        if kind == "finding":
            findings.append(_parse_finding(_finding_event_payload(event)))
            if len(findings) > 128:
                raise CodeRabbitStreamError("CODERABBIT_FINDINGS_TOO_LARGE")
        elif kind == "complete" or event.get("status") == "complete":
            if complete:
                raise CodeRabbitStreamError("CODERABBIT_COMPLETE_DUPLICATE")
            findings.extend(_complete_findings(event, findings))
            complete = True
        elif kind == "error":
            message = " ".join(
                str(event.get(key, "")).casefold()
                for key in ("code", "message", "error")
            )
            rate_limited = any(
                marker in message for marker in ("rate", "429", "too many")
            )
            retry_not_before, retry_source = (
                _parse_provider_retry_metadata(event)
                if rate_limited
                else (None, "unknown")
            )
            raise CodeRabbitStreamError(
                "CODERABBIT_RATE_LIMITED"
                if rate_limited
                else "CODERABBIT_AGENT_ERROR",
                rate_limited=rate_limited,
                retry_not_before=retry_not_before,
                retry_source=retry_source,
            )
        elif isinstance(kind, str) and _NAME_RE.fullmatch(kind):
            unknown.append(kind)
        else:
            unknown.append("unknown")
    if not complete:
        raise CodeRabbitStreamError("CODERABBIT_STREAM_TRUNCATED")
    return ParsedCodeRabbitReview(tuple(findings), complete=True, unknown_events=tuple(unknown[:16]))


def parse_provider_findings_output(output: str) -> tuple[CodeRabbitFinding, ...]:
    """Разобрать bounded human output штатной команды ``review findings``.

    Agent stream CodeRabbit иногда содержит только path/severity, тогда как
    эта read-only команда возвращает полный provider comment.  Из неё берётся
    только bounded title/comment и location; raw output не сохраняется.
    """

    if not isinstance(output, str) or not output.strip():
        return ()
    if len(output.encode("utf-8", errors="replace")) > _MAX_REVIEW_BYTES:
        return ()

    findings: list[CodeRabbitFinding] = []
    current: dict[str, object] | None = None

    def flush() -> None:
        if current is None or len(findings) >= 128:
            return
        raw_path = current.get("path")
        raw_lines = current.get("lines")
        if not isinstance(raw_path, str) or not isinstance(raw_lines, list):
            return
        path = raw_path.strip().replace("\\", "/")
        try:
            normalized_path = _finding_path({"path": path})
        except CodeRabbitStreamError:
            return
        cleaned = [
            line.strip()
            for line in raw_lines
            if isinstance(line, str)
            and line.strip()
            and not _PROVIDER_FINDING_SEPARATOR_RE.fullmatch(line)
        ]
        if not cleaned:
            return
        location_start = current.get("line")
        location_end = current.get("line_end")
        suggestion_index = next(
            (
                index
                for index, line in enumerate(cleaned)
                if "предлагаемое исправление" in line.casefold()
            ),
            None,
        )
        impact_lines = cleaned if suggestion_index is None else cleaned[:suggestion_index]
        resolution_lines = (
            cleaned[suggestion_index + 1 :]
            if suggestion_index is not None
            else []
        )
        impact = _bounded_string(
            " ".join(impact_lines), _DEFAULT_FINDING_IMPACT, 1200
        )
        resolution = _bounded_string(
            " ".join(resolution_lines), _DEFAULT_FINDING_RESOLUTION, 1200
        )
        severity = _severity(current.get("severity"))
        findings.append(
            CodeRabbitFinding(
                severity=severity,
                path=normalized_path,
                title=_bounded_string(current.get("title"), "", 160) or None,
                line=location_start if isinstance(location_start, int) else None,
                line_end=location_end if isinstance(location_end, int) else None,
                impact=impact,
                disposition=FindingDisposition.INSUFFICIENT_EVIDENCE,
                resolution=resolution,
            )
        )

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        header = _PROVIDER_FINDING_HEADER_RE.fullmatch(line)
        if header:
            flush()
            current = {
                "severity": header.group(1),
                "title": header.group(0).split("[", 1)[1].rsplit("]", 1)[0],
                "path": None,
                "line": None,
                "line_end": None,
                "lines": [],
            }
            continue
        if current is None:
            continue
        location = _PROVIDER_FINDING_LOCATION_RE.fullmatch(line)
        if location:
            current["path"] = location.group(1)
            current["line"] = int(location.group(2))
            current["line_end"] = int(location.group(3) or location.group(2))
            continue
        if _PROVIDER_FINDING_SEPARATOR_RE.fullmatch(line):
            flush()
            current = None
            continue
        lines = current["lines"]
        if isinstance(lines, list):
            lines.append(line)
    flush()
    return tuple(findings)


def _findings_need_provider_enrichment(findings: Iterable[CodeRabbitFinding]) -> bool:
    return any(
        finding.impact == _DEFAULT_FINDING_IMPACT
        for finding in findings
    )


def _enrich_provider_findings(
    runtime: _WslRuntime,
    command: str,
    parsed: ParsedCodeRabbitReview,
) -> ParsedCodeRabbitReview:
    """Получить полный bounded comment без запуска новой substantive review."""

    if not parsed.findings or not _findings_need_provider_enrichment(parsed.findings):
        return parsed
    try:
        help_result = runtime.command(command, "review", "--help", timeout=30)
    except (OSError, ToolingError, ValueError):
        # Сбой проверки возможности не доказывает её отсутствие и не запускает
        # предположенную запасную команду.
        return parsed
    if help_result.timed_out or help_result.stdout_truncated or help_result.stderr_truncated:
        return parsed
    help_text = (help_result.stdout + "\n" + help_result.stderr).casefold()
    if help_result.returncode != 0 or "findings" not in help_text:
        return parsed
    try:
        result = runtime.command(command, "review", "findings", timeout=30)
    except (OSError, ToolingError, ValueError):
        # Возможность подтверждена, но ошибка выполнения не превращается в
        # доказательство отсутствия возможности.
        return parsed
    if result.returncode != 0 or result.timed_out or result.stdout_truncated:
        return parsed
    detailed = parse_provider_findings_output(result.stdout)
    if len(detailed) != len(parsed.findings):
        return parsed
    if sorted((item.path, item.severity.value) for item in detailed) != sorted(
        (item.path, item.severity.value) for item in parsed.findings
    ):
        return parsed
    return ParsedCodeRabbitReview(
        detailed,
        complete=parsed.complete,
        unknown_events=parsed.unknown_events,
    )


def _normalized_findings_digest(findings: Iterable[CodeRabbitFinding]) -> str:
    return hashlib.sha256(
        json.dumps(
            [finding.model_dump(mode="json") for finding in findings],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def review_iteration_allowed(iterations: int, *, terminal: bool = False) -> bool:
    """Проверить bounded substantive budget текущего review cycle."""

    return not terminal and 0 <= iterations < MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE


def _default_review_state() -> dict[str, object]:
    """Создать неперсистентное состояние ещё не начатого cycle."""

    return {
        "schema_version": REVIEW_STATE_SCHEMA_VERSION,
        "current_cycle_id": "not-started",
        "cycle_started_at": None,
        "cycle_status": "fresh",
        "substantive_iterations": 0,
        # Совместимый алиас для потребителей schema 1. Это всегда счётчик
        # текущего cycle, а не lifetime-счётчик PR или репозитория.
        "iterations": 0,
        "terminal": False,
        "repository_identity": None,
        "base_sha": None,
        "last_head": None,
        "reviewed_head": None,
        "provider_state": None,
        "recovery": None,
        "rate_limited_at": None,
        "retry_not_before": None,
        "retry_source": "unknown",
        "active": False,
        "operation_id": None,
        "started_at": None,
        "attempt": 0,
        "complete_received": False,
        "last_event_type": None,
        "findings_count": 0,
        "findings_digest": None,
        "rate_limit_hint": None,
        "previous_cycles": [],
        "provider_quota": {
            "state": "unknown",
            "rate_limited_at": None,
            "retry_not_before": None,
            "retry_source": "unknown",
        },
        "updated_at": None,
    }


def _legacy_cycle_id(payload: Mapping[str, object]) -> str:
    """Получить deterministic opaque id для импортированного schema 1."""

    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    ).encode("utf-8")
    return "legacy-coderabbit-" + hashlib.sha256(encoded).hexdigest()[:16]


def _state_summary(
    state: Mapping[str, object], *, finished_at: str, terminal_reason: str
) -> dict[str, object]:
    """Сжать завершённый cycle без raw provider output."""

    return {
        "cycle_id": str(state.get("current_cycle_id") or "not-started")[:80],
        "started_at": state.get("cycle_started_at"),
        "finished_at": finished_at[:40],
        "substantive_iterations": int(state.get("substantive_iterations", 0)),
        "last_reviewed_head": state.get("reviewed_head"),
        "last_findings_count": int(state.get("findings_count", 0)),
        "terminal_reason": terminal_reason[:80],
        "provider_state": str(state.get("provider_state") or "unknown")[:80],
    }


def _bounded_diagnostics(*groups: Iterable[str], limit: int = 16) -> tuple[str, ...]:
    """Собрать bounded diagnostics без потери порядка и повторов."""

    result: list[str] = []
    for group in groups:
        for item in group:
            if item not in result:
                result.append(item)
                if len(result) >= limit:
                    return tuple(result)
    return tuple(result)


def _clean_wsl_output(value: str) -> str:
    return value.replace("\x00", "")


class _WslRuntime:
    def __init__(
        self,
        root: Path,
        executable: str,
        *,
        distro: str,
        user: str,
        home: str,
        clone: str,
        repository_identity: str | None,
        coderabbit_executable: str,
        coderabbit_command: str,
        path: str | None = None,
    ) -> None:
        self.root = root
        self.executable = executable
        self.distro = distro
        self.user = user
        self.home = home
        self.clone = clone
        self.path = path
        self.coderabbit_executable = coderabbit_executable
        self.coderabbit_command = coderabbit_command
        self.environment = WslReviewEnvironment(
            distro_name=distro,
            wsl_version=2,
            linux_user=user,
            home=home,
            review_clone=clone,
            coderabbit_executable=coderabbit_executable,
            repository_identity=repository_identity,
        )
        self.runner = StructuredProcessRunner()

    def run(self, args: tuple[str, ...], *, timeout: float = 30.0) -> _WslCommandResult:
        result = self.runner.run(
            ProcessSpec(
                executable=self.executable,
                argv=(
                    "--distribution",
                    self.distro,
                    "--user",
                    self.user,
                    *args,
                ),
                cwd=self.root,
                timeout_seconds=timeout,
                max_output_bytes=_MAX_REVIEW_BYTES,
                env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
        )
        return _WslCommandResult(
            returncode=int(result.returncode or 0),
            stdout=_clean_wsl_output(result.stdout),
            stderr=_clean_wsl_output(result.stderr),
            timed_out=result.timed_out,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
        )

    def git(self, *arguments: str, timeout: float = 30.0) -> _WslCommandResult:
        return self.run(("--exec", "git", "-C", self.clone, *arguments), timeout=timeout)

    def command(self, command: str, *arguments: str, timeout: float = 30.0) -> _WslCommandResult:
        command_arguments = (command, *arguments)
        if self.path:
            command_arguments = ("env", f"PATH={self.path}", *command_arguments)
        return self.run(
            ("--cd", self.clone, "--exec", *command_arguments), timeout=timeout
        )


class CodeRabbitAdapter(IntegrationAdapter):
    name = IntegrationName.CODERABBIT

    def _wsl_executable(self) -> str | None:
        return shutil.which("wsl.exe") or shutil.which("wsl")

    def _settings(self, config: IntegrationConfig) -> dict[str, object]:
        return config.provider("coderabbit")

    @staticmethod
    def _state_for_code(code: str, default: IntegrationState = IntegrationState.UNAVAILABLE) -> IntegrationState:
        if "NOT_CONFIGURED" in code or "NOT_FOUND" in code:
            return IntegrationState.NOT_CONFIGURED
        if any(
            marker in code
            for marker in (
                "AMBIGUOUS",
                "INCOMPATIBLE",
                "NOT_LINUX_NATIVE",
                "WSL2_REQUIRED",
                "CLONE_DIRTY",
                "REMOTE_MISMATCH",
                "HEAD_MISMATCH",
                "NOT_DETACHED",
                "ROOT_MISMATCH",
                "EXECUTABLE_AMBIGUOUS",
                "RECOVERY_REQUIRED",
            )
        ):
            return IntegrationState.INCOMPATIBLE
        if "AUTH" in code:
            return IntegrationState.UNAUTHENTICATED
        if "RATE_LIMIT" in code:
            return IntegrationState.RATE_LIMITED
        return default

    @staticmethod
    def _wsl_inventory(
        root: Path, executable: str
    ) -> tuple[tuple[WslDistribution, ...], str | None]:
        runner = StructuredProcessRunner()
        results = []
        for arguments in (("--list", "--quiet"), ("--list", "--verbose")):
            try:
                result = runner.run(
                    ProcessSpec(
                        executable=executable,
                        argv=arguments,
                        cwd=root,
                        timeout_seconds=15,
                        max_output_bytes=32 * 1024,
                        env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                    )
                )
            except ToolingError:
                return (), "CODERABBIT_WSL_INVENTORY_UNAVAILABLE"
            if (
                result.timed_out
                or result.returncode != 0
                or result.stdout_truncated
                or result.stderr_truncated
            ):
                return (), "CODERABBIT_WSL_INVENTORY_UNAVAILABLE"
            results.append(result)
        quiet_result, verbose_result = results
        try:
            quiet_names = []
            for raw_line in _clean_wsl_output(quiet_result.stdout).splitlines():
                name = raw_line.strip().lstrip("*").strip()
                if name:
                    if _NAME_RE.fullmatch(name) is None:
                        return (), "CODERABBIT_WSL_INVENTORY_INVALID"
                    quiet_names.append(name)
            entries = parse_wsl_verbose(_clean_wsl_output(verbose_result.stdout))
        except (TypeError, ValueError):
            return (), "CODERABBIT_WSL_INVENTORY_INVALID"
        if {name.casefold() for name in quiet_names} != {
            entry.name.casefold() for entry in entries
        }:
            return (), "CODERABBIT_WSL_INVENTORY_INCONSISTENT"
        return entries, None

    @staticmethod
    def _wsl_selection(
        root: Path, executable: str, configured: str | None
    ) -> WslSelection:
        entries, error_code = CodeRabbitAdapter._wsl_inventory(root, executable)
        if error_code:
            return WslSelection(None, IntegrationState.UNAVAILABLE, error_code)
        return select_wsl_distribution(
            "\n".join(f"{item.name} {item.state} {item.version}" for item in entries),
            configured=configured,
        )

    @staticmethod
    def _wsl_run(
        root: Path,
        executable: str,
        distro: str,
        arguments: tuple[str, ...],
        *,
        user: str | None = None,
        timeout: float = 15,
    ):
        argv = ["--distribution", distro]
        if user is not None:
            argv.extend(("--user", user))
        argv.extend(arguments)
        return StructuredProcessRunner().run(
            ProcessSpec(
                executable=executable,
                argv=tuple(argv),
                cwd=root,
                timeout_seconds=timeout,
                max_output_bytes=64 * 1024,
                env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
        )

    @staticmethod
    def _wsl_identity(
        root: Path, executable: str, distro: str
    ) -> tuple[tuple[str, str] | None, str | None]:
        try:
            uid_result = CodeRabbitAdapter._wsl_run(
                root, executable, distro, ("--exec", "id", "-u")
            )
            user_result = CodeRabbitAdapter._wsl_run(
                root, executable, distro, ("--exec", "id", "-un")
            )
            home_result = CodeRabbitAdapter._wsl_run(
                root, executable, distro, ("--exec", "printenv", "HOME")
            )
        except ToolingError:
            return None, "CODERABBIT_WSL_USER_UNAVAILABLE"
        uid = (
            _clean_wsl_output(uid_result.stdout).strip().splitlines()[-1]
            if uid_result.stdout.strip()
            else ""
        )
        user = (
            _clean_wsl_output(user_result.stdout).strip().splitlines()[-1]
            if user_result.stdout.strip()
            else ""
        )
        home = (
            _clean_wsl_output(home_result.stdout).strip().splitlines()[-1]
            if home_result.stdout.strip()
            else ""
        )
        if (
            uid_result.timed_out
            or user_result.timed_out
            or home_result.timed_out
            or uid_result.stdout_truncated
            or uid_result.stderr_truncated
            or user_result.stdout_truncated
            or user_result.stderr_truncated
            or home_result.stdout_truncated
            or home_result.stderr_truncated
            or uid_result.returncode != 0
            or user_result.returncode != 0
            or home_result.returncode != 0
            or not uid.isdigit()
            or int(uid) <= 0
            or not _NAME_RE.fullmatch(user)
            or not PurePosixPath(home).is_absolute()
            or ".." in PurePosixPath(home).parts
        ):
            return None, "CODERABBIT_WSL_USER_UNAVAILABLE"
        return (user, home), None

    @staticmethod
    def _wsl_path(
        root: Path,
        executable: str,
        distro: str,
        *,
        user: str,
        home: str,
    ) -> tuple[str | None, str | None]:
        """Собрать ограниченный PATH без запуска shell startup files."""

        try:
            result = CodeRabbitAdapter._wsl_run(
                root,
                executable,
                distro,
                ("--exec", "printenv", "PATH"),
                user=user,
                timeout=15,
            )
        except ToolingError:
            return None, "CODERABBIT_EXECUTABLE_PATH_UNAVAILABLE"
        if (
            result.timed_out
            or result.stdout_truncated
            or result.stderr_truncated
            or result.returncode != 0
        ):
            return None, "CODERABBIT_EXECUTABLE_PATH_UNAVAILABLE"
        current = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        raw_entries = [entry for entry in current.split(":") if entry]
        for entry in raw_entries:
            if "\x00" in entry or "\r" in entry or "\n" in entry:
                return None, "CODERABBIT_EXECUTABLE_PATH_INVALID"
        derived_entries = (
            f"{home}/.local/bin",
            f"{home}/bin",
            f"{home}/.cargo/bin",
        )
        entries: list[str] = []
        for entry in (*raw_entries, *derived_entries):
            if entry and entry not in entries:
                entries.append(entry)
        path = ":".join(entries)
        if not path or len(path) > 4096:
            return None, "CODERABBIT_EXECUTABLE_PATH_INVALID"
        return path, None

    @staticmethod
    def _linux_executable(
        runtime: _WslRuntime,
        configured: str | None,
    ) -> tuple[str | None, str | None]:
        """Найти один Linux-native CodeRabbit executable typed способом."""

        if configured is not None and (
            not isinstance(configured, str)
            or not configured.strip()
            or "\x00" in configured
        ):
            return None, "CODERABBIT_COMMAND_INVALID"
        configured_value = configured.strip() if isinstance(configured, str) else None
        if configured_value and PurePosixPath(configured_value).is_absolute():
            names = (configured_value,)
        elif configured_value:
            if not _NAME_RE.fullmatch(configured_value):
                return None, "CODERABBIT_COMMAND_INVALID"
            names = (configured_value,)
        else:
            names = ("coderabbit", "cr")

        resolved: dict[str, str] = {}
        for name in names:
            candidate = name
            if not PurePosixPath(candidate).is_absolute():
                result = runtime.command("which", candidate, timeout=30)
                if (
                    result.timed_out
                    or result.stdout_truncated
                    or result.stderr_truncated
                    or result.returncode != 0
                ):
                    continue
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                if not lines:
                    continue
                candidate = lines[-1]
            parsed = PurePosixPath(candidate)
            if (
                not parsed.is_absolute()
                or ".." in parsed.parts
                or any(parsed.name.casefold().endswith(suffix) for suffix in (".cmd", ".bat", ".exe"))
                or "\\" in candidate
            ):
                return None, "CODERABBIT_EXECUTABLE_NOT_LINUX_NATIVE"
            realpath = runtime.command("realpath", candidate, timeout=30)
            canonical = realpath.stdout.strip().splitlines()[-1] if realpath.stdout.strip() else ""
            if (
                realpath.timed_out
                or realpath.stdout_truncated
                or realpath.stderr_truncated
                or realpath.returncode != 0
                or not PurePosixPath(canonical).is_absolute()
                or ".." in PurePosixPath(canonical).parts
            ):
                return None, "CODERABBIT_EXECUTABLE_NOT_CONFIGURED"
            executable_check = runtime.command("test", "-x", canonical, timeout=30)
            if (
                executable_check.timed_out
                or executable_check.stdout_truncated
                or executable_check.stderr_truncated
                or executable_check.returncode != 0
            ):
                return None, "CODERABBIT_EXECUTABLE_NOT_CONFIGURED"
            file_result = runtime.command("file", "-b", canonical, timeout=30)
            normalized_file = file_result.stdout.casefold()
            if (
                file_result.timed_out
                or file_result.stdout_truncated
                or file_result.stderr_truncated
                or file_result.returncode != 0
                or any(marker in normalized_file for marker in ("pe32", "ms-dos", ".cmd", "windows"))
                or not any(marker in normalized_file for marker in ("elf", "executable", "script"))
            ):
                return None, "CODERABBIT_EXECUTABLE_NOT_LINUX_NATIVE"
            resolved[canonical] = canonical
        if not resolved:
            return None, "CODERABBIT_EXECUTABLE_NOT_CONFIGURED"
        if len(resolved) > 1:
            return None, "CODERABBIT_EXECUTABLE_AMBIGUOUS"
        return next(iter(resolved)), None

    @staticmethod
    def _discover_review_clones(
        root: Path,
        executable: str,
        distro: WslDistribution,
        expected_repository: str | None = None,
    ) -> tuple[tuple[str, ...], str | None]:
        identity, error_code = CodeRabbitAdapter._wsl_identity(
            root, executable, distro.name
        )
        if identity is None:
            return (), error_code
        user, home = identity
        try:
            result = CodeRabbitAdapter._wsl_run(
                root,
                executable,
                distro.name,
                (
                    "--exec",
                    "find",
                    home,
                    "-maxdepth",
                    "5",
                    "-type",
                    "d",
                    "-name",
                    ".git",
                    "-print",
                ),
                user=user,
                timeout=30,
            )
        except ToolingError:
            return (), "CODERABBIT_REVIEW_CLONE_DISCOVERY_FAILED"
        if (
            result.timed_out
            or result.returncode != 0
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            return (), "CODERABBIT_REVIEW_CLONE_DISCOVERY_FAILED"
        candidates: list[str] = []
        for raw_path in _clean_wsl_output(result.stdout).splitlines()[:128]:
            git_path = raw_path.strip()
            parsed = PurePosixPath(git_path)
            if (
                parsed.name != ".git"
                or not parsed.is_absolute()
                or ".." in parsed.parts
            ):
                continue
            clone = parsed.parent.as_posix()
            if clone not in candidates:
                candidates.append(clone)
        expected = expected_repository
        if expected is None:
            expected = CodeRabbitAdapter._expected_repository(root, {})
        if expected is None:
            return (), "CODERABBIT_REPOSITORY_NOT_CONFIGURED"
        canonical_candidates: list[str] = []
        from azurpilot.tooling.git import canonical_remote_identity

        for clone in sorted(candidates):
            try:
                top_level = CodeRabbitAdapter._wsl_run(
                    root,
                    executable,
                    distro.name,
                    ("--exec", "git", "-C", clone, "rev-parse", "--show-toplevel"),
                    user=user,
                    timeout=15,
                )
                remote = CodeRabbitAdapter._wsl_run(
                    root,
                    executable,
                    distro.name,
                    ("--exec", "git", "-C", clone, "remote", "get-url", "origin"),
                    user=user,
                    timeout=15,
                )
            except ToolingError:
                continue
            if (
                top_level.timed_out
                or top_level.stdout_truncated
                or top_level.stderr_truncated
                or top_level.returncode != 0
                or top_level.stdout.strip().rstrip("/") != clone.rstrip("/")
                or remote.timed_out
                or remote.stdout_truncated
                or remote.stderr_truncated
                or remote.returncode != 0
            ):
                continue
            try:
                actual = canonical_remote_identity(remote.stdout.strip()).casefold()
            except ToolingError:
                continue
            if actual == expected.casefold():
                canonical_candidates.append(clone)
        return tuple(sorted(set(canonical_candidates))), None

    @staticmethod
    def _candidate_environment(
        root: Path,
        executable: str,
        distro: WslDistribution,
        clone: str,
        settings: dict[str, object],
    ) -> tuple[_WslRuntime | None, str | None]:
        identity, error_code = CodeRabbitAdapter._wsl_identity(
            root, executable, distro.name
        )
        if identity is None:
            return None, error_code
        user, home = identity
        expected_repository = CodeRabbitAdapter._expected_repository(root, settings)
        if expected_repository is None:
            return None, "CODERABBIT_REPOSITORY_NOT_CONFIGURED"
        path, path_error = CodeRabbitAdapter._wsl_path(
            root, executable, distro.name, user=user, home=home
        )
        if path is None:
            return None, path_error or "CODERABBIT_EXECUTABLE_PATH_UNAVAILABLE"
        runtime = _WslRuntime(
            root,
            executable,
            distro=distro.name,
            user=user,
            home=home,
            clone=clone,
            repository_identity=expected_repository,
            coderabbit_executable="",
            coderabbit_command="",
            path=path,
        )
        ok, reason = CodeRabbitAdapter._verify_clone_identity(runtime, expected_repository)
        if not ok:
            return None, reason
        configured_executable = settings.get("executable")
        configured_command = settings.get("command")
        explicit = (
            configured_executable
            if isinstance(configured_executable, str) and configured_executable.strip()
            else configured_command
            if isinstance(configured_command, str) and configured_command.strip()
            else None
        )
        candidate_path, executable_error = CodeRabbitAdapter._linux_executable(
            runtime, explicit
        )
        if candidate_path is None:
            return None, executable_error or "CODERABBIT_EXECUTABLE_NOT_CONFIGURED"
        runtime.environment = WslReviewEnvironment(
            distro_name=distro.name,
            wsl_version=2,
            linux_user=user,
            home=home,
            review_clone=clone,
            coderabbit_executable=candidate_path,
            repository_identity=expected_repository,
        )
        runtime.coderabbit_command = candidate_path
        realpath_result = runtime.command("realpath", clone, timeout=30)
        if (
            realpath_result.timed_out
            or realpath_result.stdout_truncated
            or realpath_result.stderr_truncated
            or realpath_result.returncode != 0
            or realpath_result.stdout.strip() != clone.rstrip("/")
        ):
            return None, "CODERABBIT_REVIEW_CLONE_NOT_CANONICAL"
        ok, reason = CodeRabbitAdapter._verify_clone_state(runtime, expected_repository)
        if not ok:
            return None, reason
        return runtime, None

    @staticmethod
    def _configured_runtime(
        root: Path, settings: dict[str, object]
    ) -> tuple[_WslRuntime | None, str | None]:
        executable = shutil.which("wsl.exe") or shutil.which("wsl")
        configured_distro = settings.get("wsl_distribution")
        clone = settings.get("review_clone")
        if executable is None:
            return None, "CODERABBIT_WSL_UNAVAILABLE"
        if clone is not None and (
            not isinstance(clone, str)
            or not PurePosixPath(clone).is_absolute()
            or "\x00" in clone
            or ".." in PurePosixPath(clone).parts
        ):
            return None, "CODERABBIT_REVIEW_CLONE_NOT_CONFIGURED"
        if configured_distro is not None and (
            not isinstance(configured_distro, str)
            or not _NAME_RE.fullmatch(configured_distro)
        ):
            return None, "CODERABBIT_WSL_DISTRIBUTION_NOT_CONFIGURED"
        entries, error_code = CodeRabbitAdapter._wsl_inventory(root, executable)
        if error_code:
            return None, error_code
        expected_repository = CodeRabbitAdapter._expected_repository(root, settings)
        if expected_repository is None:
            return None, "CODERABBIT_REPOSITORY_NOT_CONFIGURED"

        def discover(candidate: WslDistribution) -> tuple[tuple[str, ...], str | None]:
            if isinstance(clone, str):
                return (clone,), None
            return CodeRabbitAdapter._discover_review_clones(
                root,
                executable,
                candidate,
                expected_repository,
            )

        if configured_distro is not None:
            selection = select_wsl_distribution(
                "\n".join(f"{item.name} {item.state} {item.version}" for item in entries),
                configured=configured_distro,
            )
            if selection.state is not IntegrationState.READY or selection.distribution is None:
                return None, selection.reason_code
            candidate = next(item for item in entries if item.name == selection.distribution)
            clones, discovery_error = discover(candidate)
            if not clones:
                return None, discovery_error or "CODERABBIT_REVIEW_CLONE_NOT_CONFIGURED"
            if len(clones) > 1:
                return None, "CODERABBIT_REVIEW_CLONE_AMBIGUOUS"
            runtime, failure = CodeRabbitAdapter._candidate_environment(
                root, executable, candidate, clones[0], settings
            )
            if runtime is not None:
                return runtime, None
            return None, failure or "CODERABBIT_REVIEW_ENVIRONMENT_NOT_CONFIGURED"

        candidates = tuple(item for item in entries if item.version == 2)
        canonical_candidates: list[tuple[WslDistribution, str]] = []
        discovery_errors: list[str] = []
        for candidate in candidates:
            clones, discovery_error = discover(candidate)
            if not clones:
                discovery_errors.append(
                    discovery_error or "CODERABBIT_REVIEW_CLONE_NOT_CONFIGURED"
                )
                continue
            if len(clones) > 1:
                return None, "CODERABBIT_REVIEW_CLONE_AMBIGUOUS"
            canonical_candidates.append((candidate, clones[0]))
        if len(canonical_candidates) > 1:
            return None, "CODERABBIT_REVIEW_CLONE_AMBIGUOUS"
        if canonical_candidates:
            candidate, selected_clone = canonical_candidates[0]
            runtime, failure = CodeRabbitAdapter._candidate_environment(
                root, executable, candidate, selected_clone, settings
            )
            if runtime is not None:
                return runtime, None
            return None, failure or "CODERABBIT_REVIEW_ENVIRONMENT_NOT_CONFIGURED"
        if not candidates:
            selection = select_wsl_distribution(
                "\n".join(f"{item.name} {item.state} {item.version}" for item in entries)
            )
            return None, selection.reason_code
        return None, "CODERABBIT_REVIEW_CLONE_NOT_CONFIGURED"

    def _record_from_error(
        self,
        settings: dict[str, object],
        code: str,
        *,
        state: IntegrationState = IntegrationState.UNAVAILABLE,
        diagnostics: tuple[str, ...] = (),
    ) -> IntegrationRecord:
        return build_record(
            self.name,
            state,
            code,
            "Прямой CodeRabbit WSL runtime не подтверждён.",
            build_evidence(
                config=settings,
                credential=CredentialRef(),
                configured=state is not IntegrationState.NOT_CONFIGURED,
                reachable=False,
                diagnostics=diagnostics,
            ),
        )

    @staticmethod
    def _review_state_classification(review_state: dict[str, object]) -> str:
        """Классифицировать durable state, не сводя его к ``active``."""

        if review_state.get("provider_state") == "recovered_interrupted":
            return "complete_non_terminal"
        if not review_state.get("operation_id") and not review_state.get("provider_state"):
            return "fresh"
        if review_state.get("active") is True:
            return "active"
        if review_state.get("complete_received") is True:
            return "complete_terminal" if review_state.get("terminal") is True else "complete_non_terminal"
        provider_state = review_state.get("provider_state")
        if provider_state in {_RATE_LIMIT_WAITING, _RATE_LIMIT_RETRY_ALLOWED, "rate_limited"}:
            if _retry_time_has_arrived(review_state.get("retry_not_before")):
                return "rate_limited_retry_allowed"
            return "rate_limited"
        if provider_state in {"failed", "timeout", "stream_error"}:
            return "incomplete_known_failure"
        return "incomplete_unknown"

    @staticmethod
    def _review_state_diagnostics(review_state: dict[str, object]) -> tuple[str, ...]:
        return (
            "review_state=" + CodeRabbitAdapter._review_state_classification(review_state),
            f"cycle_id={str(review_state.get('current_cycle_id') or 'not-started')[:80]}",
            f"cycle_status={str(review_state.get('cycle_status') or 'fresh')[:80]}",
            f"substantive_iterations={int(review_state.get('substantive_iterations', review_state.get('iterations', 0)))}/{MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE}",
            f"provider_state={str(review_state.get('provider_state') or 'not_observed')[:80]}",
            f"rate_limited_at={str(review_state.get('rate_limited_at') or 'unknown')[:40]}",
            f"retry_not_before={str(review_state.get('retry_not_before') or 'unknown')[:80]}",
            f"retry_source={str(review_state.get('retry_source') or 'unknown')[:20]}",
            f"last_reviewed_head={str(review_state.get('reviewed_head') or 'unknown')[:64]}",
        )

    @staticmethod
    def _cycle_summary(review_state: Mapping[str, object]) -> CodeRabbitCycleSummary:
        """Построить JSON-safe сводку без provider output и secret values."""

        previous = review_state.get("previous_cycles")
        return CodeRabbitCycleSummary(
            cycle_id=str(review_state.get("current_cycle_id") or "not-started"),
            cycle_status=str(review_state.get("cycle_status") or "fresh")[:80],
            substantive_iterations=int(
                review_state.get(
                    "substantive_iterations", review_state.get("iterations", 0)
                )
            ),
            provider_state=str(review_state.get("provider_state") or "not_observed")[:80],
            rate_limited_at=(
                str(review_state["rate_limited_at"])[:80]
                if review_state.get("rate_limited_at") is not None
                else None
            ),
            retry_not_before=(
                str(review_state["retry_not_before"])[:80]
                if review_state.get("retry_not_before") is not None
                else None
            ),
            retry_source=str(review_state.get("retry_source") or "unknown"),
            last_reviewed_head=(
                str(review_state["reviewed_head"])
                if review_state.get("reviewed_head") is not None
                else None
            ),
            previous_cycles_retained=len(previous) if isinstance(previous, list) else 0,
            findings_count=int(review_state.get("findings_count", 0)),
            terminal=bool(review_state.get("terminal", False)),
            active=bool(review_state.get("active", False)),
        )

    def cycle_summary(self, root: Path) -> CodeRabbitCycleSummary:
        """Прочитать текущую bounded cycle summary для CLI result."""

        return self._cycle_summary(self._load_review_state(root))

    def findings(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        base_sha: str,
        head_sha: str,
    ) -> AdapterOutcome:
        """Вернуть сохранённые findings без запуска provider review."""
        settings = self._settings(config)
        state = self._load_review_state(root)
        if (
            state.get("provider_state") != "complete"
            or state.get("complete_received") is not True
            or state.get("active") is True
            or state.get("base_sha") != base_sha
            or state.get("reviewed_head") != head_sha
        ):
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_FINDINGS_HEAD_MISMATCH",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=self._review_state_diagnostics(state),
                ), (), self._cycle_summary(state)
            )
        if "findings" not in state:
            if int(state.get("findings_count", 0)) == 0:
                return AdapterOutcome(
                    build_record(
                        self.name,
                        IntegrationState.READY,
                        "CODERABBIT_REVIEW_FINDINGS_READY",
                        "Authoritative CodeRabbit review завершён без findings.",
                        build_evidence(
                            config=settings,
                            credential=CredentialRef(auth_verified=False),
                            configured=True,
                            reachable=False,
                            diagnostics=self._review_state_diagnostics(state) + ("findings_source=durable_empty",),
                        ),
                    ), (), self._cycle_summary(state)
                )
            runtime, error_code = self._configured_runtime(root, settings)
            if runtime is None:
                return AdapterOutcome(
                    self._record_from_error(
                        settings,
                        "CODERABBIT_REVIEW_FINDINGS_CAPABILITY_UNAVAILABLE",
                        state=IntegrationState.UNAVAILABLE,
                        diagnostics=(error_code or "runtime_unavailable",),
                    ), (), self._cycle_summary(state)
                )
            runtime_state, _reason, diagnostics = self._runtime_preflight(runtime, runtime.coderabbit_command)
            if runtime_state is not IntegrationState.READY:
                return AdapterOutcome(
                    self._record_from_error(
                        settings,
                        "CODERABBIT_REVIEW_FINDINGS_CAPABILITY_UNAVAILABLE",
                        state=runtime_state,
                        diagnostics=diagnostics,
                    ), (), self._cycle_summary(state)
                )
            try:
                help_result = runtime.command(runtime.coderabbit_command, "review", "--help", timeout=30)
                help_text = (help_result.stdout + "\n" + help_result.stderr).casefold()
                if help_result.returncode != 0 or help_result.timed_out or "findings" not in help_text:
                    raise ValueError("capability not advertised")
                result = runtime.command(runtime.coderabbit_command, "review", "findings", timeout=30)
                if result.returncode != 0 or result.timed_out or result.stdout_truncated:
                    raise ValueError("findings capability failed")
                stored = parse_provider_findings_output(result.stdout)
            except (OSError, ToolingError, ValueError):
                return AdapterOutcome(
                    self._record_from_error(
                        settings,
                        "CODERABBIT_REVIEW_FINDINGS_CAPABILITY_UNAVAILABLE",
                        state=IntegrationState.UNAVAILABLE,
                        diagnostics=diagnostics,
                    ), (), self._cycle_summary(state)
                )
            if len(stored) != int(state.get("findings_count", -1)):
                return AdapterOutcome(
                    self._record_from_error(settings, "CODERABBIT_REVIEW_FINDINGS_STATE_MISMATCH", state=IntegrationState.UNKNOWN),
                    (), self._cycle_summary(state)
                )
        else:
            stored = tuple(CodeRabbitFinding.model_validate(item) for item in state.get("findings", []))
        if len(stored) != int(state.get("findings_count", -1)):
            return AdapterOutcome(
                self._record_from_error(settings, "CODERABBIT_REVIEW_FINDINGS_STATE_MISMATCH", state=IntegrationState.UNKNOWN),
                (), self._cycle_summary(state)
            )
        digest = state.get("findings_digest")
        if not isinstance(digest, str) or _normalized_findings_digest(stored) != digest:
            return AdapterOutcome(
                self._record_from_error(settings, "CODERABBIT_REVIEW_FINDINGS_DIGEST_MISMATCH", state=IntegrationState.UNKNOWN),
                (), self._cycle_summary(state)
            )
        findings = tuple(
            IntegrationFinding(
                kind="coderabbit",
                identifier=(item.title or item.path)[:240],
                path=item.path,
                line=item.line,
                line_end=item.line_end,
                title=item.title,
                severity=item.severity.value,
                message=item.impact[:400],
                fingerprint=None,
                reviewed_head=head_sha,
                base_sha=base_sha,
                fix_head=None,
                disposition=item.disposition.value,
                resolution=item.resolution[:400],
            ) for item in stored
        )
        return AdapterOutcome(
            build_record(
                self.name,
                IntegrationState.READY,
                "CODERABBIT_REVIEW_FINDINGS_READY",
                f"Сохранены findings CodeRabbit: {len(findings)}.",
                build_evidence(
                    config=settings,
                    credential=CredentialRef(auth_verified=False),
                    configured=True,
                    reachable=True,
                    diagnostics=self._review_state_diagnostics(state) + ("provider_review_not_started=true",),
                ),
            ), findings, self._cycle_summary(state)
        )

    def recover_interrupted_review(
        self, root: Path, config: IntegrationConfig
    ) -> AdapterOutcome:
        """Безопасно закрыть только доказанно неактивную прерванную попытку."""

        settings = self._settings(config)
        state = self._load_review_state(root)
        if self._review_state_classification(state) != "active":
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_RECOVERY_NOT_REQUIRED",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=self._review_state_diagnostics(state),
                )
            )
        operation_id = state.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            return AdapterOutcome(self._record_from_error(settings, "CODERABBIT_REVIEW_OPERATION_UNKNOWN", state=IntegrationState.INCOMPATIBLE))
        runtime, error_code = self._configured_runtime(root, settings)
        if runtime is None:
            return AdapterOutcome(self._record_from_error(settings, error_code or "CODERABBIT_RUNTIME_UNAVAILABLE", state=IntegrationState.INCOMPATIBLE))
        probe = runtime.command("pgrep", "-x", "coderabbit", timeout=30)
        if probe.timed_out or probe.stdout_truncated or probe.stderr_truncated:
            return AdapterOutcome(self._record_from_error(settings, "CODERABBIT_REVIEW_OPERATION_LIVENESS_UNKNOWN", state=IntegrationState.INCOMPATIBLE))
        if probe.returncode == 0:
            return AdapterOutcome(self._record_from_error(settings, "CODERABBIT_REVIEW_OPERATION_STILL_ALIVE", state=IntegrationState.INCOMPATIBLE, diagnostics=("provider_process_present=true",)))
        if probe.returncode not in {1, 2}:
            return AdapterOutcome(self._record_from_error(settings, "CODERABBIT_REVIEW_OPERATION_LIVENESS_UNKNOWN", state=IntegrationState.INCOMPATIBLE))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        previous = list(state.get("previous_cycles") or [])
        previous.append({"cycle_id": state.get("current_cycle_id", "not-started"), "started_at": state.get("started_at"), "finished_at": now, "last_reviewed_head": None, "terminal_reason": "external_interruption_recovered", "provider_state": "interrupted"})
        previous = previous[-MAX_RETAINED_REVIEW_CYCLES:]
        self._save_state(
            root, iterations=int(state.get("substantive_iterations", 0)), head=str(state.get("last_head") or state.get("base_sha") or "0" * 40),
            terminal=False, base_sha=str(state.get("base_sha") or "0" * 40), repository_identity=str(state.get("repository_identity") or ""),
            attempt=int(state.get("attempt", 0)), operation_id="", started_at="", provider_state="recovered_interrupted", active=False,
            complete_received=False, last_event_type="recovery", reviewed_head=None, cycle_status="recovered", previous_cycles=previous,
            recovery={"reason": "external_interruption", "operation_id": operation_id, "cycle_id": str(state.get("current_cycle_id") or "not-started"), "head": state.get("last_head"), "base_sha": state.get("base_sha"), "substantive_iterations": int(state.get("substantive_iterations", 0)), "verified_at": now},
        )
        return AdapterOutcome(self._record_from_error(settings, "CODERABBIT_REVIEW_RECOVERED", state=IntegrationState.READY, diagnostics=("provider_process_absent=true",)))

    @staticmethod
    def _inventory_diagnostics(
        root: Path, executable: str, configured: str | None
    ) -> tuple[tuple[str, ...], str | None]:
        entries, error_code = CodeRabbitAdapter._wsl_inventory(root, executable)
        if error_code:
            return (), error_code
        diagnostics = [f"wsl_inventory_count={len(entries)}"]
        if configured is not None:
            selection = select_wsl_distribution(
                "\n".join(
                    f"{item.name} {item.state} {item.version}" for item in entries
                ),
                configured=configured,
            )
            if selection.distribution:
                diagnostics.append(f"wsl_distribution={selection.distribution}")
                selected_entry = next(
                    (item for item in entries if item.name == selection.distribution),
                    None,
                )
                if selected_entry is not None:
                    diagnostics.append(f"wsl_version={selected_entry.version}")
            diagnostics.append(f"wsl_selection={selection.reason_code}")
        else:
            wsl2_entries = tuple(item for item in entries if item.version == 2)
            diagnostics.append(f"wsl_wsl2_count={len(wsl2_entries)}")
            if len(wsl2_entries) == 1:
                diagnostics.append(f"wsl_distribution={wsl2_entries[0].name}")
                diagnostics.append("wsl_selection=inventory_candidate_pending_clone")
            elif len(wsl2_entries) > 1:
                diagnostics.append("wsl_selection=requires_clone_validation")
            else:
                selection = select_wsl_distribution(
                    "\n".join(
                        f"{item.name} {item.state} {item.version}" for item in entries
                    )
                )
                diagnostics.append(f"wsl_selection={selection.reason_code}")
        return tuple(diagnostics), None

    def status(self, root: Path, config: IntegrationConfig) -> IntegrationRecord:
        settings = self._settings(config)
        executable = self._wsl_executable()
        if executable is None:
            return self._record_from_error(settings, "CODERABBIT_WSL_UNAVAILABLE")
        configured_distro = settings.get("wsl_distribution")
        if configured_distro is not None and (
            not isinstance(configured_distro, str)
            or not _NAME_RE.fullmatch(configured_distro)
        ):
            return self._record_from_error(
                settings,
                "CODERABBIT_WSL_DISTRIBUTION_NOT_CONFIGURED",
                state=IntegrationState.NOT_CONFIGURED,
            )
        inventory_diagnostics, inventory_error = self._inventory_diagnostics(
            root, executable, configured_distro if isinstance(configured_distro, str) else None
        )
        if inventory_error:
            return self._record_from_error(
                settings,
                inventory_error,
                state=IntegrationState.UNAVAILABLE,
                diagnostics=inventory_diagnostics,
            )
        runtime, error_code = self._configured_runtime(root, settings)
        if runtime is None:
            return self._record_from_error(
                settings,
                error_code or "CODERABBIT_REVIEW_ENVIRONMENT_NOT_CONFIGURED",
                state=self._state_for_code(
                    error_code or "CODERABBIT_REVIEW_ENVIRONMENT_NOT_CONFIGURED",
                    IntegrationState.UNAVAILABLE,
                ),
                diagnostics=inventory_diagnostics,
            )
        try:
            review_state = self._load_review_state(root)
        except ToolingError:
            return self._record_from_error(
                settings,
                "CODERABBIT_REVIEW_STATE_UNAVAILABLE",
                state=IntegrationState.UNKNOWN,
                diagnostics=inventory_diagnostics + ("review_state=unavailable",),
            )
        review_classification = self._review_state_classification(review_state)
        if review_classification in {
            "active",
            "incomplete_known_failure",
            "incomplete_unknown",
            "rate_limited",
        }:
            return self._record_from_error(
                settings,
                "CODERABBIT_REVIEW_RECOVERY_REQUIRED"
                if review_classification != "rate_limited"
                else "CODERABBIT_RATE_LIMITED",
                state=IntegrationState.RATE_LIMITED
                if review_classification == "rate_limited"
                else IntegrationState.INCOMPATIBLE,
                diagnostics=inventory_diagnostics
                + self._review_state_diagnostics(review_state),
            )
        command = runtime.coderabbit_command
        runtime_state, reason, runtime_diagnostics = self._runtime_preflight(
            runtime, command
        )
        diagnostics = _bounded_diagnostics(
            self._review_state_diagnostics(review_state),
            inventory_diagnostics,
            runtime_diagnostics,
        )
        if runtime_state is not IntegrationState.READY:
            return self._record_from_error(
                settings, reason, state=runtime_state, diagnostics=diagnostics
            )
        return build_record(
            self.name,
            IntegrationState.READY,
            "CODERABBIT_DIRECT_ROUTE_READY",
            "CodeRabbit подтверждён через WSL2 runtime, clone, CLI и auth.",
            build_evidence(
                config=settings,
                credential=CredentialRef(),
                configured=True,
                reachable=True,
                authenticated=True,
                diagnostics=diagnostics,
            ),
        )

    @staticmethod
    def _verify_clone_identity(
        runtime: _WslRuntime, expected_repository: str | None
    ) -> tuple[bool, str]:
        """Подтвердить canonical origin до проверки dirty/head состояния."""

        root_result = runtime.git("rev-parse", "--show-toplevel")
        remote_result = runtime.git("remote", "get-url", "origin")
        if (
            root_result.timed_out
            or root_result.stdout_truncated
            or root_result.stderr_truncated
            or root_result.returncode != 0
            or root_result.stdout.strip().rstrip("/") != runtime.clone.rstrip("/")
        ):
            return False, "CODERABBIT_REVIEW_CLONE_ROOT_MISMATCH"
        if (
            remote_result.timed_out
            or remote_result.stdout_truncated
            or remote_result.stderr_truncated
            or remote_result.returncode != 0
            or not remote_result.stdout.strip()
        ):
            return False, "CODERABBIT_REVIEW_REMOTE_UNAVAILABLE"
        try:
            from azurpilot.tooling.git import canonical_remote_identity

            actual_repository = canonical_remote_identity(remote_result.stdout.strip()).casefold()
        except ToolingError:
            return False, "CODERABBIT_REVIEW_REMOTE_MISMATCH"
        if expected_repository and actual_repository != expected_repository.casefold():
            return False, "CODERABBIT_REVIEW_REMOTE_MISMATCH"
        return True, "CODERABBIT_REVIEW_CLONE_IDENTITY_READY"

    @staticmethod
    def _verify_clone_state(
        runtime: _WslRuntime,
        expected_repository: str | None,
        expected_head: str | None = None,
    ) -> tuple[bool, str]:
        identity_ok, identity_reason = CodeRabbitAdapter._verify_clone_identity(
            runtime, expected_repository
        )
        if not identity_ok:
            return False, identity_reason
        checks = {
            "root": runtime.git("rev-parse", "--show-toplevel"),
            "status": runtime.git("status", "--porcelain=v1"),
            "head": runtime.git("rev-parse", "HEAD"),
        }
        for name, result in checks.items():
            if (
                result.timed_out
                or result.stdout_truncated
                or result.stderr_truncated
                or result.returncode != 0
            ):
                return False, f"CODERABBIT_REVIEW_CLONE_{name.upper()}_FAILED"
            if name == "root" and result.stdout.strip() != runtime.clone.rstrip("/"):
                return False, "CODERABBIT_REVIEW_CLONE_ROOT_MISMATCH"
            if name == "status" and result.stdout.strip():
                return False, "CODERABBIT_REVIEW_CLONE_DIRTY"
            if (
                name == "head"
                and expected_head is not None
                and result.stdout.strip().casefold() != expected_head
            ):
                return False, "CODERABBIT_REVIEW_HEAD_MISMATCH"
        return True, "CODERABBIT_REVIEW_CLONE_STATE_READY"

    def _verify_clone(
        self,
        runtime: _WslRuntime,
        expected_head: str | None,
        expected_repository: str | None,
    ) -> tuple[bool, str]:
        state_ok, state_reason = self._verify_clone_state(
            runtime, expected_repository, expected_head
        )
        if not state_ok:
            return False, state_reason
        branch = runtime.git("branch", "--show-current")
        if (
            branch.timed_out
            or branch.stdout_truncated
            or branch.stderr_truncated
            or branch.returncode != 0
        ):
            return False, "CODERABBIT_REVIEW_CLONE_BRANCH_FAILED"
        if branch.stdout.strip():
            return False, "CODERABBIT_REVIEW_CLONE_NOT_DETACHED"
        return True, "CODERABBIT_REVIEW_CLONE_READY"

    def _prepare_clone(
        self,
        runtime: _WslRuntime,
        root: Path,
        *,
        expected_head: str,
        expected_repository: str,
    ) -> tuple[bool, str]:
        """Безопасно подготовить clean dedicated clone к exact committed head."""

        identity_ok, identity_reason = self._verify_clone_identity(
            runtime, expected_repository
        )
        if not identity_ok:
            return False, identity_reason
        ready, reason = self._verify_clone_state(runtime, expected_repository)
        if not ready:
            return False, reason
        current_head = runtime.git("rev-parse", "HEAD")
        current = current_head.stdout.strip().casefold()
        if current == expected_head:
            return self._verify_clone(runtime, expected_head, expected_repository)
        fetched = runtime.git(
            "fetch", "--no-tags", "origin", expected_head, timeout=15 * 60
        )
        if (
            fetched.timed_out
            or fetched.stdout_truncated
            or fetched.stderr_truncated
            or fetched.returncode != 0
        ):
            return False, "CODERABBIT_REVIEW_TARGET_FETCH_FAILED"
        target = runtime.git(
            "cat-file", "-e", f"{expected_head}^{{commit}}", timeout=30
        )
        if (
            target.timed_out
            or target.stdout_truncated
            or target.stderr_truncated
            or target.returncode != 0
        ):
            return False, "CODERABBIT_REVIEW_TARGET_UNAVAILABLE"
        checkout = runtime.git("checkout", "--detach", expected_head, timeout=120)
        if (
            checkout.timed_out
            or checkout.stdout_truncated
            or checkout.stderr_truncated
            or checkout.returncode != 0
        ):
            return False, "CODERABBIT_REVIEW_CLONE_PREPARE_FAILED"
        return self._verify_clone(runtime, expected_head, expected_repository)

    def _auth_ready(self, runtime: _WslRuntime, command: str) -> tuple[bool, str]:
        result = runtime.command(
            command, "auth", "status", "--agent", timeout=45
        )
        if result.timed_out or result.stdout_truncated or result.stderr_truncated:
            return False, "CODERABBIT_AUTH_TIMEOUT"
        if result.returncode != 0:
            return False, "CODERABBIT_AUTH_NOT_CONFIGURED"
        return True, "CODERABBIT_AUTH_READY"

    def _version_ready(
        self, runtime: _WslRuntime, command: str
    ) -> tuple[bool, str, str | None]:
        result = runtime.command(
            command, "--version", timeout=45
        )
        if result.timed_out or result.stdout_truncated or result.stderr_truncated:
            return False, "CODERABBIT_VERSION_TIMEOUT", None
        if result.returncode != 0:
            return False, "CODERABBIT_VERSION_UNAVAILABLE", None
        match = _VERSION_OUTPUT_RE.search(result.stdout)
        if match is None:
            return False, "CODERABBIT_VERSION_INVALID", None
        return True, "CODERABBIT_VERSION_CONFIRMED", match.group(0)

    def _help_ready(self, runtime: _WslRuntime, command: str) -> tuple[bool, str]:
        result = runtime.command(
            command, "review", "--help", timeout=45
        )
        normalized = result.stdout.casefold()
        if result.timed_out or result.stdout_truncated or result.stderr_truncated:
            return False, "CODERABBIT_HELP_TIMEOUT"
        if (
            result.returncode != 0
            or "--agent" not in normalized
            or "--committed" not in normalized
            or "--base-commit" not in normalized
        ):
            return False, "CODERABBIT_REVIEW_SYNTAX_INCOMPATIBLE"
        return True, "CODERABBIT_REVIEW_SYNTAX_READY"

    @staticmethod
    def _environment_diagnostics(
        runtime: _WslRuntime,
        *,
        version: str | None = None,
        auth_state: str = "not_observed",
        review_state: dict[str, object] | None = None,
    ) -> tuple[str, ...]:
        environment = runtime.environment
        diagnostics = [
            f"wsl_distribution={environment.distro_name}",
            f"wsl_version={environment.wsl_version}",
            "linux_user=validated_non_root",
            "home=validated",
            "review_clone=validated",
            "coderabbit_executable=linux_native",
            f"auth={auth_state}",
            "quota=not_observable",
        ]
        if environment.repository_identity:
            diagnostics.append(
                f"repository_identity={environment.repository_identity[:240]}"
            )
        if version:
            diagnostics.append(f"cli_version={version}")
        if review_state is not None:
            diagnostics.extend(CodeRabbitAdapter._review_state_diagnostics(review_state))
        return tuple(diagnostics)

    def _runtime_preflight(
        self, runtime: _WslRuntime, command: str
    ) -> tuple[IntegrationState, str, tuple[str, ...]]:
        ok, reason, version = self._version_ready(runtime, command)
        if not ok:
            return (
                IntegrationState.INCOMPATIBLE,
                reason,
                self._environment_diagnostics(runtime),
            )
        ok, reason = self._help_ready(runtime, command)
        if not ok:
            return (
                IntegrationState.INCOMPATIBLE,
                reason,
                self._environment_diagnostics(runtime, version=version),
            )
        ok, reason = self._auth_ready(runtime, command)
        if not ok:
            return (
                IntegrationState.UNAUTHENTICATED,
                reason,
                self._environment_diagnostics(
                    runtime, version=version, auth_state="not_configured"
                ),
            )
        return (
            IntegrationState.READY,
            "CODERABBIT_RUNTIME_READY",
            self._environment_diagnostics(
                runtime, version=version, auth_state="verified"
            ),
        )

    def _load_review_state(self, root: Path) -> dict[str, object]:
        layout = StateLayout.for_repository(root)
        path = layout.path(_STATE_FILE_NAME)
        if not path.is_file():
            return _default_review_state()
        try:
            payload = json.loads(bounded_read_text(path, max_bytes=_MAX_STATE_BYTES))
        except (OSError, UnicodeError, ValueError, ToolingError):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние CodeRabbit review повреждено.",
            )
        if not isinstance(payload, dict):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние CodeRabbit review имеет неверную схему.",
            )
        schema_version = payload.get("schema_version", 1)
        if schema_version == 1:
            migrated = self._migrate_legacy_state(payload)
            self._write_state(root, migrated)
            return migrated
        if schema_version != REVIEW_STATE_SCHEMA_VERSION:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние CodeRabbit имеет неподдерживаемую версию схемы.",
            )
        if payload.get("provider_state") == _RATE_LIMIT_WAITING and _retry_time_has_arrived(
            payload.get("retry_not_before")
        ):
            payload = dict(payload)
            payload["provider_state"] = _RATE_LIMIT_RETRY_ALLOWED
            payload["cycle_status"] = _RATE_LIMIT_RETRY_ALLOWED
            quota = payload.get("provider_quota")
            if isinstance(quota, dict):
                payload["provider_quota"] = {**quota, "state": _RATE_LIMIT_RETRY_ALLOWED}
            self._validate_review_state(payload)
            self._write_state(root, payload)
        self._validate_review_state(payload)
        return payload

    @staticmethod
    def _migrate_legacy_state(payload: Mapping[str, object]) -> dict[str, object]:
        """Детерминированно импортировать schema 1 как один legacy cycle."""

        legacy = _default_review_state()
        iterations = payload.get("iterations", 0)
        terminal = payload.get("terminal", False)
        active = payload.get("active", False)
        complete_received = payload.get(
            "complete_received", payload.get("provider_state") == "complete"
        )
        if (
            not isinstance(iterations, int)
            or not 0 <= iterations <= MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE
            or not isinstance(terminal, bool)
            or not isinstance(active, bool)
            or not isinstance(complete_received, bool)
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Legacy-состояние CodeRabbit повреждено.",
            )
        for key in (
            "operation_id",
            "started_at",
            "repository_identity",
            "last_head",
            "base_sha",
            "reviewed_head",
            "provider_state",
            "last_event_type",
            "rate_limit_hint",
            "findings_digest",
            "updated_at",
        ):
            value = payload.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 512):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Текстовое поле legacy-состояния CodeRabbit повреждено.",
                )
        for key in ("last_head", "base_sha", "reviewed_head"):
            value = payload.get(key)
            if value is not None and _SHA_RE.fullmatch(value) is None:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "SHA legacy-состояния CodeRabbit повреждён.",
                )
        operation_id = payload.get("operation_id")
        if operation_id is not None and (
            not isinstance(operation_id, str)
            or not operation_id.startswith("coderabbit-")
            or len(operation_id) > 80
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Идентификатор legacy CodeRabbit review повреждён.",
            )
        if active and payload.get("started_at") is None:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Время активного legacy CodeRabbit review отсутствует.",
            )
        if complete_received and payload.get("reviewed_head") is None:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Legacy CodeRabbit review не содержит reviewed head.",
            )
        findings_count = payload.get("findings_count", 0)
        attempt = payload.get("attempt", iterations)
        if not isinstance(findings_count, int) or not 0 <= findings_count <= 128:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Счётчик legacy findings CodeRabbit повреждён.",
            )
        if not isinstance(attempt, int) or not 0 <= attempt <= _MAX_REVIEW_ATTEMPTS:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Счётчик legacy попыток CodeRabbit повреждён.",
            )
        provider_state = payload.get("provider_state")
        if provider_state == "rate_limited":
            provider_state = _RATE_LIMIT_WAITING
        if provider_state is not None and (
            not isinstance(provider_state, str) or len(provider_state) > 80
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние provider в legacy CodeRabbit повреждено.",
            )
        legacy.update(
            {
                "current_cycle_id": _legacy_cycle_id(payload),
                "cycle_started_at": payload.get("started_at") or payload.get("updated_at"),
                "cycle_status": (
                    "reviewing"
                    if active
                    else _RATE_LIMIT_WAITING
                    if provider_state == _RATE_LIMIT_WAITING
                    else "zero_findings"
                    if terminal and findings_count == 0
                    else "budget_exhausted"
                    if iterations >= MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE
                    else str(provider_state or "fresh")[:80]
                ),
                "substantive_iterations": iterations,
                "iterations": iterations,
                "terminal": terminal,
                "repository_identity": payload.get("repository_identity"),
                "base_sha": payload.get("base_sha"),
                "last_head": payload.get("last_head"),
                "reviewed_head": payload.get("reviewed_head"),
                "provider_state": provider_state,
                "rate_limited_at": payload.get("updated_at")
                if provider_state == _RATE_LIMIT_WAITING
                else None,
                "active": active,
                "operation_id": operation_id,
                "started_at": payload.get("started_at"),
                "attempt": attempt,
                "complete_received": complete_received,
                "last_event_type": payload.get("last_event_type"),
                "findings_count": findings_count,
                "findings_digest": payload.get("findings_digest"),
                "rate_limit_hint": payload.get("rate_limit_hint"),
                "updated_at": payload.get("updated_at"),
                "provider_quota": {
                    "state": provider_state or "unknown",
                    "rate_limited_at": payload.get("updated_at")
                    if provider_state == _RATE_LIMIT_WAITING
                    else None,
                    "retry_not_before": None,
                    "retry_source": "unknown",
                },
            }
        )
        CodeRabbitAdapter._validate_review_state(legacy)
        return legacy

    @staticmethod
    def _validate_review_state(payload: Mapping[str, object]) -> None:
        """Fail-closed validation bounded schema 2."""

        required = (
            "current_cycle_id",
            "cycle_status",
            "substantive_iterations",
            "terminal",
            "active",
            "complete_received",
            "findings_count",
            "previous_cycles",
            "provider_quota",
        )
        if any(key not in payload for key in required):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние CodeRabbit не содержит обязательные поля.",
            )
        cycle_id = payload.get("current_cycle_id")
        if not isinstance(cycle_id, str) or not _CYCLE_ID_RE.fullmatch(cycle_id):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Идентификатор cycle CodeRabbit повреждён.",
            )
        cycle_status = payload.get("cycle_status")
        if not isinstance(cycle_status, str) or not 1 <= len(cycle_status) <= 80:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Статус cycle CodeRabbit повреждён.",
            )
        iterations = payload.get("substantive_iterations")
        alias = payload.get("iterations", iterations)
        if (
            not isinstance(iterations, int)
            or not 0 <= iterations <= MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE
            or not isinstance(alias, int)
            or alias != iterations
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Счётчик substantive CodeRabbit review повреждён.",
            )
        for key in ("terminal", "active", "complete_received"):
            if not isinstance(payload.get(key), bool):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Флаг состояния CodeRabbit повреждён.",
                )
        for key in (
            "cycle_started_at",
            "repository_identity",
            "base_sha",
            "last_head",
            "reviewed_head",
            "provider_state",
            "rate_limited_at",
            "retry_not_before",
            "operation_id",
            "started_at",
            "last_event_type",
            "findings_digest",
            "rate_limit_hint",
            "updated_at",
        ):
            value = payload.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 512):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Текстовое поле состояния CodeRabbit повреждено.",
                )
        for key in ("base_sha", "last_head", "reviewed_head"):
            value = payload.get(key)
            if value is not None and _SHA_RE.fullmatch(value) is None:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "SHA состояния CodeRabbit повреждён.",
                )
        retry_source = payload.get("retry_source", "unknown")
        if retry_source not in _RETRY_SOURCES:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Источник retry metadata CodeRabbit повреждён.",
            )
        operation_id = payload.get("operation_id")
        if operation_id is not None and (
            not isinstance(operation_id, str)
            or not operation_id.startswith("coderabbit-")
            or len(operation_id) > 80
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Идентификатор операции CodeRabbit повреждён.",
            )
        if payload.get("active") and payload.get("started_at") is None:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Время активного CodeRabbit review отсутствует.",
            )
        if payload.get("complete_received") and payload.get("reviewed_head") is None:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Завершённый CodeRabbit review не содержит reviewed head.",
            )
        findings_count = payload.get("findings_count")
        if not isinstance(findings_count, int) or not 0 <= findings_count <= _MAX_RETAINED_FINDINGS:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Счётчик findings CodeRabbit повреждён.",
            )
        has_persisted_findings = "findings" in payload
        stored_findings = payload.get("findings", [])
        if not isinstance(stored_findings, list) or len(stored_findings) > _MAX_RETAINED_FINDINGS:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Список findings CodeRabbit повреждён.",
            )
        try:
            normalized = tuple(CodeRabbitFinding.model_validate(item) for item in stored_findings)
        except (TypeError, ValueError):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Нормализованный finding CodeRabbit повреждён.",
            ) from None
        digest = payload.get("findings_digest")
        if has_persisted_findings and len(normalized) != findings_count:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Счётчик findings CodeRabbit не совпадает с сохранёнными findings.",
            )
        if has_persisted_findings and digest is not None and (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or _normalized_findings_digest(normalized) != digest
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Digest findings CodeRabbit не совпадает с сохранёнными findings.",
            )
        attempt = payload.get("attempt", 0)
        if not isinstance(attempt, int) or not 0 <= attempt <= _MAX_REVIEW_ATTEMPTS:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Счётчик попыток CodeRabbit повреждён.",
            )
        previous = payload.get("previous_cycles")
        if not isinstance(previous, list) or len(previous) > MAX_RETAINED_REVIEW_CYCLES:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "История cycles CodeRabbit повреждена.",
            )
        for summary in previous:
            if not isinstance(summary, dict):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Сводка cycle CodeRabbit повреждена.",
                )
            summary_id = summary.get("cycle_id")
            if not isinstance(summary_id, str) or len(summary_id) > 80:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Идентификатор исторического cycle CodeRabbit повреждён.",
                )
            for key in ("started_at", "finished_at", "last_reviewed_head", "terminal_reason", "provider_state"):
                value = summary.get(key)
                if value is not None and (not isinstance(value, str) or len(value) > 512):
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "Историческое поле CodeRabbit повреждено.",
                    )
            if summary.get("last_reviewed_head") is not None and _SHA_RE.fullmatch(
                str(summary["last_reviewed_head"])
            ) is None:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Исторический reviewed head CodeRabbit повреждён.",
                )
        quota = payload.get("provider_quota")
        if not isinstance(quota, dict):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Provider quota metadata CodeRabbit повреждена.",
            )
        if set(quota) - {
            "state",
            "rate_limited_at",
            "retry_not_before",
            "retry_source",
        }:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Provider quota metadata CodeRabbit содержит лишние поля.",
            )
        for key in ("state", "rate_limited_at", "retry_not_before"):
            value = quota.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 512):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Provider quota metadata CodeRabbit повреждена.",
                )
        recovery = payload.get("recovery")
        if recovery is not None:
            if not isinstance(recovery, dict) or set(recovery) - {"reason", "operation_id", "cycle_id", "head", "base_sha", "substantive_iterations", "verified_at"}:
                raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "Evidence recovery CodeRabbit повреждена.")
            for key in ("reason", "operation_id", "cycle_id", "head", "base_sha", "verified_at"):
                value = recovery.get(key)
                if value is not None and (not isinstance(value, str) or len(value) > 512):
                    raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "Evidence recovery CodeRabbit повреждена.")
            for key in ("head", "base_sha"):
                value = recovery.get(key)
                if value is not None and _SHA_RE.fullmatch(value) is None:
                    raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "SHA recovery CodeRabbit повреждён.")
            if not isinstance(recovery.get("substantive_iterations"), int):
                raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "Счётчик recovery CodeRabbit повреждён.")
        quota_source = quota.get("retry_source", "unknown")
        if quota_source not in _RETRY_SOURCES:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Источник provider quota CodeRabbit повреждён.",
            )

    @staticmethod
    def _write_state(root: Path, payload: Mapping[str, object]) -> None:
        layout = StateLayout.for_repository(root)
        layout.ensure()
        ScopedPath(layout.repository_directory).atomic_write_text(
            _STATE_FILE_NAME,
            json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n",
        )

    def _state(self, root: Path) -> tuple[int, bool]:
        payload = self._load_review_state(root)
        return int(payload.get("substantive_iterations", 0)), bool(payload.get("terminal", False))

    @staticmethod
    def _expected_repository(root: Path, settings: dict[str, object]) -> str | None:
        configured = settings.get("repository_slug")
        if isinstance(configured, str) and configured.strip():
            return configured.strip().casefold()
        try:
            from azurpilot.tooling.config import load_deploy_settings
            from azurpilot.tooling.git import canonical_remote_identity

            repository_url = load_deploy_settings(root).repository_url
            return (
                canonical_remote_identity(repository_url).casefold()
                if repository_url
                else None
            )
        except (ToolingError, OSError, ValueError):
            pass
        try:
            from azurpilot.tooling.git import GitClient

            return GitClient(root).remote_identity("origin").casefold()
        except (ToolingError, OSError, ValueError):
            return None

    def _save_state(
        self,
        root: Path,
        *,
        iterations: int,
        head: str,
        terminal: bool,
        base_sha: str,
        repository_identity: str,
        attempt: int,
        operation_id: str,
        started_at: str,
        provider_state: str,
        active: bool,
        complete_received: bool,
        last_event_type: str,
        findings_count: int = 0,
        findings_digest: str | None = None,
        findings: Iterable[CodeRabbitFinding] | None = None,
        rate_limit_hint: str | None = None,
        cycle_id: str | None = None,
        cycle_started_at: str | None = None,
        reviewed_head: str | None = None,
        rate_limited_at: str | None = None,
        retry_not_before: str | None = None,
        retry_source: str = "unknown",
        cycle_status: str | None = None,
        previous_cycles: list[dict[str, object]] | None = None,
        provider_quota: dict[str, object] | None = None,
        recovery: dict[str, object] | None = None,
    ) -> None:
        current = self._load_review_state(root)
        selected_cycle_id = cycle_id or str(current.get("current_cycle_id") or "not-started")
        if selected_cycle_id == "not-started":
            selected_cycle_id = "coderabbit-cycle-" + secrets.token_hex(8)
        selected_started_at = cycle_started_at or current.get("cycle_started_at")
        if selected_started_at is None:
            selected_started_at = started_at
        selected_reviewed_head = reviewed_head
        if selected_reviewed_head is None:
            selected_reviewed_head = current.get("reviewed_head")
        quota = provider_quota or current.get("provider_quota")
        if not isinstance(quota, dict):
            quota = {
                "state": "unknown",
                "rate_limited_at": None,
                "retry_not_before": None,
                "retry_source": "unknown",
            }
        if provider_state in {_RATE_LIMIT_WAITING, _RATE_LIMIT_RETRY_ALLOWED, "rate_limited"}:
            quota = {
                "state": _RATE_LIMIT_WAITING,
                "rate_limited_at": rate_limited_at,
                "retry_not_before": retry_not_before,
                "retry_source": retry_source if retry_source in _RETRY_SOURCES else "unknown",
            }
        if active:
            resolved_cycle_status = "reviewing"
        elif provider_state in {_RATE_LIMIT_WAITING, "rate_limited"}:
            resolved_cycle_status = _RATE_LIMIT_WAITING
        elif provider_state == _RATE_LIMIT_RETRY_ALLOWED:
            resolved_cycle_status = _RATE_LIMIT_RETRY_ALLOWED
        elif complete_received and terminal and findings_count == 0:
            resolved_cycle_status = "zero_findings"
        elif complete_received and iterations >= MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE:
            resolved_cycle_status = "budget_exhausted"
        else:
            resolved_cycle_status = cycle_status or str(provider_state or "fresh")
        source_findings = (
            findings
            if findings is not None
            else []
            if not complete_received and findings_count == 0
            else current.get("findings", [])
        )
        normalized_findings = [
            item.model_dump(mode="json") if isinstance(item, CodeRabbitFinding) else item
            for item in source_findings
        ]
        if len(normalized_findings) > _MAX_RETAINED_FINDINGS:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Список findings CodeRabbit превышает bounded предел.",
            )
        payload = {
            "schema_version": REVIEW_STATE_SCHEMA_VERSION,
            "current_cycle_id": selected_cycle_id[:80],
            "cycle_started_at": str(selected_started_at)[:40],
            "cycle_status": resolved_cycle_status[:80],
            "substantive_iterations": iterations,
            # Совместимый алиас; см. _default_review_state().
            "iterations": iterations,
            "last_head": head,
            "terminal": terminal,
            "active": active,
            "operation_id": operation_id[:80] if operation_id else None,
            "started_at": started_at[:40] if started_at else None,
            "attempt": attempt,
            "repository_identity": repository_identity[:512] if repository_identity else None,
            "base_sha": base_sha,
            "reviewed_head": selected_reviewed_head,
            "provider_state": provider_state[:80],
            "rate_limited_at": rate_limited_at,
            "retry_not_before": retry_not_before,
            "retry_source": retry_source if retry_source in _RETRY_SOURCES else "unknown",
            "complete_received": complete_received,
            "last_event_type": last_event_type[:80],
            "findings_count": findings_count,
            "findings_digest": findings_digest,
            **({"findings": normalized_findings} if findings is not None else {}),
            "rate_limit_hint": rate_limit_hint[:240] if rate_limit_hint else None,
            "previous_cycles": list(previous_cycles or current.get("previous_cycles", []))[-MAX_RETAINED_REVIEW_CYCLES:],
            "provider_quota": quota,
            "recovery": recovery if recovery is not None else current.get("recovery"),
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self._validate_review_state(payload)
        self._write_state(root, payload)

    def start_cycle(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        base_sha: str | None = None,
    ) -> AdapterOutcome:
        """Явно закрыть предыдущий cycle и создать новый без запуска review."""

        settings = self._settings(config)
        review_state = self._load_review_state(root)
        classification = self._review_state_classification(review_state)
        if classification == "rate_limited":
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_RATE_LIMITED",
                    state=IntegrationState.RATE_LIMITED,
                    diagnostics=self._review_state_diagnostics(review_state),
                )
            )
        if classification in {
            "active",
            "incomplete_known_failure",
            "incomplete_unknown",
        }:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_CYCLE_START_FORBIDDEN",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=self._review_state_diagnostics(review_state),
                )
            )
        if base_sha is not None and not _SHA_RE.fullmatch(base_sha):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "CodeRabbit cycle base должен быть exact SHA.",
            )
        stored_base = review_state.get("base_sha")
        if base_sha is not None and isinstance(stored_base, str) and stored_base and stored_base != base_sha:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_BASE_MISMATCH",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=self._review_state_diagnostics(review_state),
                )
            )
        expected_repository = self._expected_repository(root, settings)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        previous = list(review_state.get("previous_cycles", []))
        has_current_cycle = (
            review_state.get("current_cycle_id") != "not-started"
            or bool(review_state.get("operation_id"))
            or int(review_state.get("substantive_iterations", 0)) > 0
            or bool(review_state.get("provider_state"))
        )
        if has_current_cycle:
            previous.append(
                _state_summary(
                    review_state,
                    finished_at=now,
                    terminal_reason="operator_started_new_fix_cycle",
                )
            )
        quota = review_state.get("provider_quota")
        if not isinstance(quota, dict):
            quota = {
                "state": "unknown",
                "rate_limited_at": None,
                "retry_not_before": None,
                "retry_source": "unknown",
            }
        current_head = _git_head(root)
        new_state = _default_review_state()
        new_state.update(
            {
                "current_cycle_id": "coderabbit-cycle-" + secrets.token_hex(8),
                "cycle_started_at": now,
                "cycle_status": "fresh",
                "repository_identity": review_state.get("repository_identity") or expected_repository,
                "base_sha": base_sha or stored_base,
                "last_head": current_head,
                "last_event_type": "cycle_start",
                "previous_cycles": previous[-MAX_RETAINED_REVIEW_CYCLES:],
                # Provider quota is deliberately carried independently from
                # the fresh logical cycle budget.
                "provider_quota": quota,
                "updated_at": now,
            }
        )
        self._validate_review_state(new_state)
        self._write_state(root, new_state)
        diagnostics = self._review_state_diagnostics(new_state) + (
            f"previous_cycles_retained={len(new_state['previous_cycles'])}",
            "review_not_started=true",
        )
        record = build_record(
            self.name,
            IntegrationState.READY,
            "CODERABBIT_REVIEW_CYCLE_STARTED",
            "Новый CodeRabbit review cycle создан; provider review не запускался.",
            build_evidence(
                config=settings,
                credential=CredentialRef(),
                configured=True,
                reachable=False,
                authenticated=None,
                diagnostics=diagnostics,
            ),
        )
        return AdapterOutcome(record, coderabbit_cycle=self._cycle_summary(new_state))

    async def probe(self, root: Path, config: IntegrationConfig) -> AdapterOutcome:
        settings = self._settings(config)
        try:
            review_state = self._load_review_state(root)
        except ToolingError:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_STATE_UNAVAILABLE",
                    state=IntegrationState.UNKNOWN,
                )
            )
        review_classification = self._review_state_classification(review_state)
        if review_classification in {
            "active",
            "incomplete_known_failure",
            "incomplete_unknown",
            "rate_limited",
        }:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_RECOVERY_REQUIRED"
                    if review_classification != "rate_limited"
                    else "CODERABBIT_RATE_LIMITED",
                    state=IntegrationState.RATE_LIMITED
                    if review_classification == "rate_limited"
                    else IntegrationState.INCOMPATIBLE,
                    diagnostics=self._review_state_diagnostics(review_state),
                )
            )
        runtime, error_code = self._configured_runtime(root, settings)
        if runtime is None:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    error_code or "CODERABBIT_RUNTIME_UNAVAILABLE",
                    state=self._state_for_code(
                        error_code or "CODERABBIT_RUNTIME_UNAVAILABLE",
                        IntegrationState.UNAVAILABLE,
                    ),
                )
            )
        command = runtime.coderabbit_command
        expected_head = _git_head(root)
        if expected_head is None:
            return AdapterOutcome(self._record_from_error(settings, "CODERABBIT_LOCAL_HEAD_UNAVAILABLE"))
        ok, reason = self._verify_clone(runtime, expected_head, None)
        if not ok:
            return AdapterOutcome(self._record_from_error(settings, reason, state=IntegrationState.INCOMPATIBLE))
        runtime_state, reason, diagnostics = self._runtime_preflight(runtime, command)
        if runtime_state is not IntegrationState.READY:
            return AdapterOutcome(
                self._record_from_error(
                    settings, reason, state=runtime_state, diagnostics=diagnostics
                )
            )
        return AdapterOutcome(
            build_record(
                self.name,
                IntegrationState.READY,
                reason,
                "CodeRabbit CLI, review syntax и agent authentication подтверждены.",
                build_evidence(
                    config=settings,
                    credential=CredentialRef(auth_verified=True),
                    configured=True,
                    reachable=True,
                    authenticated=True,
                    diagnostics=diagnostics,
                ),
            )
        )

    def review(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        base_sha: str,
        head_sha: str,
        progress_callback: Callable[[CodeRabbitProgress], None] | None = None,
    ) -> AdapterOutcome:
        if not _SHA_RE.fullmatch(base_sha) or not _SHA_RE.fullmatch(head_sha):
            raise ToolingError(ResultCode.TOOLING_INVALID_INVOCATION, "CodeRabbit base/head должны быть exact SHA.")
        settings = self._settings(config)
        review_state = self._load_review_state(root)
        iterations = int(review_state.get("substantive_iterations", review_state.get("iterations", 0)))
        terminal = bool(review_state.get("terminal", False))
        initial_cycle_id = str(review_state.get("current_cycle_id") or "not-started")
        initial_attempt = int(review_state.get("attempt", 0)) + 1
        _emit_progress(
            progress_callback,
            phase="preflight",
            cycle_id=initial_cycle_id,
            attempt=initial_attempt,
            substantive_iterations=iterations,
            provider_state=str(review_state.get("provider_state") or "not_observed"),
            message="Проверяю cycle state, exact head и dedicated review clone.",
        )
        review_classification = self._review_state_classification(review_state)
        if review_classification in {
            "active",
            "incomplete_known_failure",
            "incomplete_unknown",
            "rate_limited",
        }:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_RECOVERY_REQUIRED"
                    if review_classification != "rate_limited"
                    else "CODERABBIT_RATE_LIMITED",
                    state=IntegrationState.RATE_LIMITED
                    if review_classification == "rate_limited"
                    else IntegrationState.INCOMPATIBLE,
                    diagnostics=self._review_state_diagnostics(review_state),
                )
            )
        if not review_iteration_allowed(iterations, terminal=terminal):
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_ITERATION_BUDGET_EXHAUSTED",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=self._review_state_diagnostics(review_state),
                )
            )
        stored_base = review_state.get("base_sha")
        if isinstance(stored_base, str) and stored_base and stored_base != base_sha:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_BASE_MISMATCH",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=self._review_state_diagnostics(review_state),
                )
            )
        runtime, error_code = self._configured_runtime(root, settings)
        if runtime is None:
            return AdapterOutcome(self._record_from_error(settings, error_code or "CODERABBIT_RUNTIME_UNAVAILABLE"))
        command = runtime.coderabbit_command
        expected_repository = self._expected_repository(root, settings)
        if expected_repository is None:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REPOSITORY_NOT_CONFIGURED",
                    state=IntegrationState.NOT_CONFIGURED,
                )
            )
        ok, reason = self._prepare_clone(
            runtime,
            root,
            expected_head=head_sha,
            expected_repository=expected_repository,
        )
        if not ok:
            return AdapterOutcome(self._record_from_error(settings, reason, state=IntegrationState.INCOMPATIBLE))
        _emit_progress(
            progress_callback,
            phase="clone_ready",
            cycle_id=initial_cycle_id,
            attempt=initial_attempt,
            substantive_iterations=iterations,
            provider_state="preflight",
            message="Exact committed head подтверждён в dedicated review clone.",
        )
        runtime_state, reason, runtime_diagnostics = self._runtime_preflight(runtime, command)
        if runtime_state is not IntegrationState.READY:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    reason,
                    state=runtime_state,
                    diagnostics=runtime_diagnostics,
                )
            )
        _emit_progress(
            progress_callback,
            phase="provider_preflight",
            cycle_id=initial_cycle_id,
            attempt=initial_attempt,
            substantive_iterations=iterations,
            provider_state="ready",
            message="CLI, review syntax и agent authentication подтверждены.",
        )
        operation_id = f"coderabbit-{secrets.token_hex(8)}"
        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        attempt = int(review_state.get("attempt", 0)) + 1
        if attempt > _MAX_REVIEW_ATTEMPTS:
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_ATTEMPT_LIMIT_EXHAUSTED",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=self._review_state_diagnostics(review_state),
                )
            )
        cycle_id = str(review_state.get("current_cycle_id") or "not-started")
        if cycle_id == "not-started":
            cycle_id = "coderabbit-cycle-" + secrets.token_hex(8)
        cycle_started_at = review_state.get("cycle_started_at") or started_at
        provider_quota = review_state.get("provider_quota")
        self._save_state(
            root,
            iterations=iterations,
            head=head_sha,
            terminal=False,
            base_sha=base_sha,
            repository_identity=expected_repository,
            attempt=attempt,
            operation_id=operation_id,
            started_at=started_at,
            provider_state="reviewing",
            active=True,
            complete_received=False,
            last_event_type="review_start",
            cycle_id=cycle_id,
            cycle_started_at=str(cycle_started_at),
            reviewed_head=review_state.get("reviewed_head")
            if isinstance(review_state.get("reviewed_head"), str)
            else None,
            provider_quota=provider_quota if isinstance(provider_quota, dict) else None,
        )
        provider_started = time.monotonic()
        _emit_progress(
            progress_callback,
            phase="provider_started",
            cycle_id=cycle_id,
            attempt=attempt,
            substantive_iterations=iterations,
            provider_state="reviewing",
            message="Provider review запущен; ожидается complete или rate limit.",
            started_monotonic=provider_started,
        )
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        if progress_callback is not None:
            def heartbeat() -> None:
                while not heartbeat_stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
                    _emit_progress(
                        progress_callback,
                        phase="provider_running",
                        cycle_id=cycle_id,
                        attempt=attempt,
                        substantive_iterations=iterations,
                        provider_state="reviewing",
                        message="Review всё ещё выполняется; новый запрос к провайдеру не отправляется.",
                        started_monotonic=provider_started,
                    )

            heartbeat_thread = threading.Thread(
                target=heartbeat,
                name="coderabbit-review-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
        try:
            result = runtime.command(
                command,
                "review",
                "--agent",
                "--committed",
                "--base-commit",
                base_sha,
                timeout=20 * 60,
            )
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1.0)
        _emit_progress(
            progress_callback,
            phase="provider_finished",
            cycle_id=cycle_id,
            attempt=attempt,
            substantive_iterations=iterations,
            provider_state="command_finished",
            message="Provider command завершил работу; разбираю bounded result.",
            started_monotonic=provider_started,
        )
        if result.timed_out:
            self._save_state(
                root,
                iterations=iterations,
                head=head_sha,
                terminal=False,
                base_sha=base_sha,
                repository_identity=expected_repository,
                attempt=attempt,
                operation_id=operation_id,
                started_at=started_at,
                provider_state="timeout",
                active=False,
                complete_received=False,
                last_event_type="timeout",
                cycle_id=cycle_id,
                cycle_started_at=str(cycle_started_at),
                reviewed_head=review_state.get("reviewed_head")
                if isinstance(review_state.get("reviewed_head"), str)
                else None,
            )
            _emit_progress(
                progress_callback,
                phase="timeout",
                cycle_id=cycle_id,
                attempt=attempt,
                substantive_iterations=iterations,
                provider_state="timeout",
                message="Review завершился по timeout; substantive iteration не расходуется.",
                started_monotonic=provider_started,
            )
            return AdapterOutcome(self._record_from_error(settings, "CODERABBIT_REVIEW_TIMEOUT"))
        if result.stdout_truncated or result.stderr_truncated:
            self._save_state(
                root,
                iterations=iterations,
                head=head_sha,
                terminal=False,
                base_sha=base_sha,
                repository_identity=expected_repository,
                attempt=attempt,
                operation_id=operation_id,
                started_at=started_at,
                provider_state="stream_error",
                active=False,
                complete_received=False,
                last_event_type="output_truncated",
                cycle_id=cycle_id,
                cycle_started_at=str(cycle_started_at),
                reviewed_head=review_state.get("reviewed_head")
                if isinstance(review_state.get("reviewed_head"), str)
                else None,
            )
            _emit_progress(
                progress_callback,
                phase="output_truncated",
                cycle_id=cycle_id,
                attempt=attempt,
                substantive_iterations=iterations,
                provider_state="stream_error",
                message="Provider output усечён; substantive iteration не расходуется.",
                started_monotonic=provider_started,
            )
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_OUTPUT_TRUNCATED",
                    state=IntegrationState.UNKNOWN,
                )
            )
        if result.returncode != 0:
            combined = (result.stdout + "\n" + result.stderr).casefold()
            rate_limited = any(marker in combined for marker in ("rate limit", "429", "too many requests"))
            failure_code = "CODERABBIT_RATE_LIMITED" if rate_limited else "CODERABBIT_REVIEW_FAILED"
            rate_limited_at = datetime.now(UTC).isoformat(timespec="seconds") if rate_limited else None
            retry_not_before, retry_source = (None, "unknown")
            self._save_state(
                root,
                iterations=iterations,
                head=head_sha,
                terminal=False,
                base_sha=base_sha,
                repository_identity=expected_repository,
                attempt=attempt,
                operation_id=operation_id,
                started_at=started_at,
                provider_state=_RATE_LIMIT_WAITING if rate_limited else "failed",
                active=False,
                complete_received=False,
                last_event_type="error",
                rate_limit_hint="provider_rate_limit_observed" if rate_limited else None,
                cycle_id=cycle_id,
                cycle_started_at=str(cycle_started_at),
                reviewed_head=review_state.get("reviewed_head")
                if isinstance(review_state.get("reviewed_head"), str)
                else None,
                rate_limited_at=rate_limited_at,
                retry_not_before=retry_not_before,
                retry_source=retry_source,
            )
            _emit_progress(
                progress_callback,
                phase="rate_limited" if rate_limited else "provider_failed",
                cycle_id=cycle_id,
                attempt=attempt,
                substantive_iterations=iterations,
                provider_state=_RATE_LIMIT_WAITING if rate_limited else "failed",
                message=(
                    "Provider сообщил rate limit; cycle сохранён без расхода iteration."
                    if rate_limited
                    else "Provider review завершился ошибкой; cycle сохранён для диагностики."
                ),
                started_monotonic=provider_started,
            )
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    failure_code,
                    state=IntegrationState.RATE_LIMITED if rate_limited else IntegrationState.UNAVAILABLE,
                )
            )
        try:
            parsed = parse_agent_ndjson(result.stdout.splitlines())
        except CodeRabbitStreamError as error:
            rate_limited_at = datetime.now(UTC).isoformat(timespec="seconds") if error.rate_limited else None
            retry_not_before, retry_source = (
                (error.retry_not_before, error.retry_source)
                if error.rate_limited and error.retry_not_before
                else (None, "unknown")
            )
            self._save_state(
                root,
                iterations=iterations,
                head=head_sha,
                terminal=False,
                base_sha=base_sha,
                repository_identity=expected_repository,
                attempt=attempt,
                operation_id=operation_id,
                started_at=started_at,
                provider_state=_RATE_LIMIT_WAITING if error.rate_limited else "stream_error",
                active=False,
                complete_received=False,
                last_event_type="error",
                rate_limit_hint="provider_rate_limit_observed" if error.rate_limited else None,
                cycle_id=cycle_id,
                cycle_started_at=str(cycle_started_at),
                reviewed_head=review_state.get("reviewed_head")
                if isinstance(review_state.get("reviewed_head"), str)
                else None,
                rate_limited_at=rate_limited_at,
                retry_not_before=retry_not_before,
                retry_source=retry_source,
            )
            _emit_progress(
                progress_callback,
                phase="rate_limited" if error.rate_limited else "parse_failed",
                cycle_id=cycle_id,
                attempt=attempt,
                substantive_iterations=iterations,
                provider_state=_RATE_LIMIT_WAITING if error.rate_limited else "stream_error",
                message=(
                    "Provider сообщил rate limit; cycle сохранён без расхода iteration."
                    if error.rate_limited
                    else "Provider result не прошёл bounded parser; iteration не расходуется."
                ),
                started_monotonic=provider_started,
            )
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    error.code,
                    state=IntegrationState.RATE_LIMITED
                    if error.rate_limited
                    else IntegrationState.UNKNOWN,
                )
            )
        parsed = _enrich_provider_findings(runtime, command, parsed)
        if not parsed.complete:
            self._save_state(
                root,
                iterations=iterations,
                head=head_sha,
                terminal=False,
                base_sha=base_sha,
                repository_identity=expected_repository,
                attempt=attempt,
                operation_id=operation_id,
                started_at=started_at,
                provider_state="stream_error",
                active=False,
                complete_received=False,
                last_event_type="complete_missing",
                cycle_id=cycle_id,
                cycle_started_at=str(cycle_started_at),
                reviewed_head=review_state.get("reviewed_head")
                if isinstance(review_state.get("reviewed_head"), str)
                else None,
            )
            return AdapterOutcome(
                self._record_from_error(
                    settings,
                    "CODERABBIT_STREAM_TRUNCATED",
                    state=IntegrationState.UNKNOWN,
                )
            )
        findings_digest = _normalized_findings_digest(parsed.findings)
        self._save_state(
            root,
            iterations=iterations + 1,
            head=head_sha,
            terminal=not parsed.findings,
            base_sha=base_sha,
            repository_identity=expected_repository,
            attempt=attempt,
            operation_id=operation_id,
            started_at=started_at,
            provider_state="complete",
            active=False,
            complete_received=True,
            last_event_type="complete",
            findings_count=len(parsed.findings),
            findings_digest=findings_digest,
            findings=parsed.findings,
            cycle_id=cycle_id,
            cycle_started_at=str(cycle_started_at),
            reviewed_head=head_sha,
            provider_quota={
                "state": "available",
                "rate_limited_at": None,
                "retry_not_before": None,
                "retry_source": "unknown",
            },
        )
        completed_state = self._load_review_state(root)
        completed_diagnostics = _bounded_diagnostics(
            self._review_state_diagnostics(completed_state),
            (
                f"base_sha={base_sha}",
                f"reviewed_head={head_sha}",
                f"operation_id={operation_id}",
                "untrusted_provider_text_not_executed",
            ),
            runtime_diagnostics,
        )
        record = build_record(
            self.name,
            IntegrationState.READY,
            "CODERABBIT_REVIEW_COMPLETE",
            f"CodeRabbit review завершён; findings: {len(parsed.findings)}.",
            build_evidence(
                config=settings,
                credential=CredentialRef(auth_verified=True),
                configured=True,
                reachable=True,
                authenticated=True,
                diagnostics=completed_diagnostics,
            ),
        )
        findings = tuple(
            IntegrationFinding(
                kind="coderabbit",
                identifier=(f.title or f.path)[:240],
                path=f.path,
                line=f.line,
                line_end=f.line_end,
                title=f.title,
                severity=f.severity.value,
                message=f.impact[:400],
                reviewed_head=head_sha,
                base_sha=base_sha,
                disposition=f.disposition.value,
                resolution=f.resolution[:400],
            )
            for f in parsed.findings
        )
        _emit_progress(
            progress_callback,
            phase="complete",
            cycle_id=cycle_id,
            attempt=attempt,
            substantive_iterations=iterations + 1,
            provider_state="complete",
            message=f"Review завершён; получено замечаний: {len(findings)}.",
            started_monotonic=provider_started,
        )
        return AdapterOutcome(record, findings, self._cycle_summary(completed_state))


def _git_head(root: Path) -> str | None:
    try:
        return GitClient(root).head().casefold()
    except (OSError, ToolingError, ValueError):
        return None


__all__ = [
    "CODERABBIT_STATE_SCHEMA",
    "MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE",
    "REVIEW_STATE_SCHEMA_VERSION",
    "CodeRabbitAdapter",
    "CodeRabbitProgress",
    "CodeRabbitStreamError",
    "ParsedCodeRabbitReview",
    "WslDistribution",
    "WslReviewEnvironment",
    "WslSelection",
    "parse_agent_ndjson",
    "parse_provider_findings_output",
    "parse_wsl_verbose",
    "review_iteration_allowed",
    "select_wsl_distribution",
]
