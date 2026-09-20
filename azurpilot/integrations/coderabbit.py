"""Bounded native CodeRabbit agent boundary for the canonical checkout.

The adapter deliberately owns the complete provider lifecycle: discovery,
candidate preflight, exact process identity, bounded parsing, state, and the
postcondition check.  It never creates a second checkout and never delegates
provider liveness to a name-only process search.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from azurpilot.tooling.contracts import (
    CodeRabbitFinding,
    FindingDisposition,
    FindingSeverity,
    ResultCode,
)
from azurpilot.tooling.coordination import RepositoryCoordinator
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import (
    ScopedPath,
    StateLayout,
    bounded_read_text,
    path_has_link,
    path_identity,
)
from azurpilot.tooling.git import GitClient
from azurpilot.tooling.process import (
    ProcessController,
    ProcessIdentity,
    ProcessResult,
    ProcessSpec,
    RunningProcess,
    StructuredProcessRunner,
)

from .adapters import (
    AdapterOutcome,
    IntegrationAdapter,
    build_evidence,
    build_record,
)
from .config import IntegrationConfig
from .contracts import (
    CYCLE_ID_PATTERN,
    MAX_RETAINED_REVIEW_CYCLES,
    MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE,
    TASK_ID_PATTERN,
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
_CYCLE_ID_RE = re.compile(CYCLE_ID_PATTERN)
_TASK_ID_RE = re.compile(TASK_ID_PATTERN)
_MAX_REVIEW_BYTES = 4 * 1024 * 1024
_MAX_REVIEW_LINES = 512
_MAX_STATE_BYTES = 32 * 1024
_MAX_REVIEW_ATTEMPTS = 128
_MAX_RETAINED_FINDINGS = 128
_RATE_LIMIT_MAX_SECONDS = 7 * 24 * 60 * 60
_HEARTBEAT_INTERVAL_SECONDS = 15.0
_REVIEW_TIMEOUT_SECONDS = 20 * 60
_STATE_FILE_NAME = "coderabbit-review.json"
_REVIEW_SCHEMA_VERSION = 3
REVIEW_STATE_SCHEMA_VERSION = _REVIEW_SCHEMA_VERSION
CODERABBIT_STATE_SCHEMA = REVIEW_STATE_SCHEMA_VERSION
_RATE_LIMIT_WAITING = "rate_limited_waiting"
_RATE_LIMIT_RETRY_ALLOWED = "rate_limited_retry_allowed"
_RETRY_SOURCES = frozenset({"provider", "unknown"})
_DEFAULT_FINDING_IMPACT = "CodeRabbit finding требует независимой проверки."
_DEFAULT_FINDING_RESOLUTION = "Не применено автоматически; требуется независимая проверка."
_PROVIDER_FINDING_HEADER_RE = re.compile(
    r"^\s*(critical|major|minor|trivial|info)\s+\[[^\]]{1,160}\]\s*$",
    re.IGNORECASE,
)
_PROVIDER_FINDING_LOCATION_RE = re.compile(
    r"^\s*→\s+(.+?):([1-9][0-9]*)(?:-([1-9][0-9]*))?\s*$"
)
_PROVIDER_FINDING_SEPARATOR_RE = re.compile(r"^\s*[─-]{8,}\s*$")
_PROVIDER_WRAPPER_SUFFIXES = frozenset({".cmd", ".bat", ".ps1"})
_PROVIDER_NAME = "coderabbit.exe"


class CodeRabbitStreamError(ValueError):
    """Ошибка bounded NDJSON потока без сохранения provider payload."""

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
    """Bounded operator event emitted by the adapter heartbeat."""

    phase: str
    cycle_id: str
    attempt: int
    substantive_iterations: int
    provider_state: str
    message: str
    elapsed_seconds: int = 0


@dataclass(frozen=True, slots=True)
class CandidateFingerprint:
    """Exact candidate identity captured immediately before provider start."""

    root_identity: str
    repository_identity: str
    base_sha: str
    head_sha: str
    status_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "root_identity": self.root_identity,
            "repository_identity": self.repository_identity,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "status_digest": self.status_digest,
        }


@dataclass(frozen=True, slots=True)
class CanonicalCheckout:
    """Минимальная identity clean canonical checkout для status/doctor."""

    root_identity: str
    repository_identity: str
    head_sha: str
    status_digest: str


@dataclass(frozen=True, slots=True)
class ProviderCheck:
    state: IntegrationState
    reason_code: str
    message: str
    diagnostics: tuple[str, ...] = ()
    provider: NativeCodeRabbit | None = None
    configured: bool = False
    authenticated: bool | None = None


@dataclass(slots=True)
class NativeCodeRabbit:
    """Host-native executable plus the bounded readiness evidence."""

    executable: Path
    version: str
    review_help: str
    root: Path
    runner: StructuredProcessRunner

    def run(
        self,
        *args: str,
        timeout_seconds: float = 30.0,
        max_output_bytes: int = 128 * 1024,
    ) -> ProcessResult:
        return self.runner.run(
            ProcessSpec(
                executable=self.executable,
                argv=tuple(args),
                cwd=self.root,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
                env={"NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"},
            )
        )

    def start_review(self, base_sha: str) -> RunningProcess:
        return self.runner.start(
            ProcessSpec(
                executable=self.executable,
                argv=(
                    "review",
                    "--agent",
                    "--committed",
                    "--base-commit",
                    base_sha,
                ),
                cwd=self.root,
                timeout_seconds=_REVIEW_TIMEOUT_SECONDS,
                max_output_bytes=_MAX_REVIEW_BYTES,
                env={"NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"},
                capture_output=True,
            )
        )

    @property
    def display_name(self) -> str:
        return self.executable.name[:128]


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
    current = now or datetime.now(UTC)
    containers: list[Mapping[str, object]] = [event]
    for key in ("metadata", "details", "rate_limit"):
        nested = event.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    for container in containers:
        for key in ("retry_not_before", "retry_at", "retryAt", "reset_at", "resetAt"):
            value = container.get(key)
            if not isinstance(value, str) or len(value) > 128:
                continue
            try:
                parsed = datetime.fromisoformat(value.strip())
            except ValueError:
                continue
            if parsed.tzinfo is None:
                continue
            parsed = parsed.astimezone(UTC)
            if parsed <= current + timedelta(seconds=_RATE_LIMIT_MAX_SECONDS):
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
                return (
                    (current + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
                    "provider",
                )
    return None, "unknown"


def _finding_path(payload: Mapping[str, object]) -> str:
    candidates: list[object] = [
        payload.get("path"),
        payload.get("fileName"),
        payload.get("file"),
    ]
    location = payload.get("location")
    if isinstance(location, Mapping):
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
    payload = raw if isinstance(raw, Mapping) else {}
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
    location = payload.get("location")
    line: int | None = None
    line_end: int | None = None
    if isinstance(location, Mapping):
        raw_line = location.get("line") or location.get("start_line")
        raw_end = location.get("end_line") or location.get("line_end")
        if isinstance(raw_line, int) and not isinstance(raw_line, bool) and raw_line >= 1:
            line = raw_line
        if isinstance(raw_end, int) and not isinstance(raw_end, bool) and raw_end >= 1:
            line_end = raw_end
    for key, target in (("line", "start"), ("line_end", "end")):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            if target == "start":
                line = value
            else:
                line_end = value
    return CodeRabbitFinding(
        severity=_severity(payload.get("severity") or payload.get("priority")),
        path=path,
        title=_bounded_string(title, "", 160) or None,
        line=line,
        line_end=line_end,
        impact=impact,
        disposition=_disposition(payload.get("disposition") or payload.get("classification")),
        resolution=resolution,
    )


def _finding_event_payload(event: Mapping[str, object]) -> object:
    nested = event.get("finding")
    if not isinstance(nested, Mapping):
        return nested if nested is not None else event
    payload = {key: value for key, value in event.items() if key != "finding"}
    payload.update(nested)
    return payload


def _complete_findings(
    event: Mapping[str, object], findings: list[CodeRabbitFinding]
) -> tuple[CodeRabbitFinding, ...]:
    nested = event.get("findings")
    if nested is None:
        return ()
    if isinstance(nested, bool):
        raise CodeRabbitStreamError("CODERABBIT_FINDINGS_INVALID")
    if isinstance(nested, int):
        if nested > _MAX_RETAINED_FINDINGS or nested < 0 or nested != len(findings):
            raise CodeRabbitStreamError("CODERABBIT_FINDINGS_COUNT_MISMATCH")
        return ()
    if isinstance(nested, Mapping):
        count = nested.get("count")
        items = nested.get("items", nested.get("findings"))
        if items is None and isinstance(count, int) and not isinstance(count, bool):
            if count > _MAX_RETAINED_FINDINGS or count < 0 or count != len(findings):
                raise CodeRabbitStreamError("CODERABBIT_FINDINGS_COUNT_MISMATCH")
            return ()
        nested = items
    if not isinstance(nested, list):
        raise CodeRabbitStreamError("CODERABBIT_FINDINGS_INVALID")
    if len(findings) + len(nested) > _MAX_RETAINED_FINDINGS:
        raise CodeRabbitStreamError("CODERABBIT_FINDINGS_TOO_LARGE")
    return tuple(_parse_finding(item) for item in nested)


def parse_agent_ndjson(lines: Iterable[str]) -> ParsedCodeRabbitReview:
    """Разобрать только bounded structured agent events."""

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
            if len(findings) > _MAX_RETAINED_FINDINGS:
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
            rate_limited = any(marker in message for marker in ("rate", "429", "too many"))
            retry_not_before, retry_source = (
                _parse_provider_retry_metadata(event)
                if rate_limited
                else (None, "unknown")
            )
            raise CodeRabbitStreamError(
                "CODERABBIT_RATE_LIMITED" if rate_limited else "CODERABBIT_AGENT_ERROR",
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
    return ParsedCodeRabbitReview(
        tuple(findings), complete=True, unknown_events=tuple(unknown[:16])
    )


def parse_provider_findings_output(output: str) -> tuple[CodeRabbitFinding, ...]:
    """Разобрать bounded human findings output без сохранения raw текста."""

    if not isinstance(output, str) or not output.strip():
        return ()
    if len(output.encode("utf-8", errors="replace")) > _MAX_REVIEW_BYTES:
        return ()
    findings: list[CodeRabbitFinding] = []
    current: dict[str, object] | None = None

    def flush() -> None:
        if current is None or len(findings) >= _MAX_RETAINED_FINDINGS:
            return
        raw_path = current.get("path")
        raw_lines = current.get("lines")
        if not isinstance(raw_path, str) or not isinstance(raw_lines, list):
            return
        try:
            normalized_path = _finding_path({"path": raw_path.strip().replace("\\", "/")})
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
        suggestion_index = next(
            (
                index
                for index, line in enumerate(cleaned)
                if "предлагаемое исправление" in line.casefold()
            ),
            None,
        )
        impact_lines = cleaned if suggestion_index is None else cleaned[:suggestion_index]
        resolution_lines = cleaned[suggestion_index + 1 :] if suggestion_index is not None else []
        findings.append(
            CodeRabbitFinding(
                severity=_severity(current.get("severity")),
                path=normalized_path,
                title=_bounded_string(current.get("title"), "", 160) or None,
                line=current.get("line") if isinstance(current.get("line"), int) else None,
                line_end=current.get("line_end")
                if isinstance(current.get("line_end"), int)
                else None,
                impact=_bounded_string(" ".join(impact_lines), _DEFAULT_FINDING_IMPACT, 1200),
                disposition=FindingDisposition.INSUFFICIENT_EVIDENCE,
                resolution=_bounded_string(
                    " ".join(resolution_lines), _DEFAULT_FINDING_RESOLUTION, 1200
                ),
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
    return not terminal and 0 <= iterations < MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE


def _default_review_state() -> dict[str, object]:
    return {
        "schema_version": _REVIEW_SCHEMA_VERSION,
        "current_cycle_id": "not-started",
        "cycle_started_at": None,
        "cycle_status": "fresh",
        "logical_task_id": None,
        "substantive_iterations": 0,
        "iterations": 0,
        "terminal": False,
        "repository_identity": None,
        "root_identity": None,
        "base_sha": None,
        "last_head": None,
        "reviewed_head": None,
        "provider_state": None,
        "provider_version": None,
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
        "findings": [],
        "candidate_fingerprint": None,
        "provider_identity": None,
        "phase": "idle",
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
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    ).encode("utf-8")
    return "coderabbit-cycle-" + hashlib.sha256(encoded).hexdigest()[:16]


def _state_summary(
    state: Mapping[str, object], *, finished_at: str, terminal_reason: str
) -> dict[str, object]:
    return {
        "cycle_id": str(state.get("current_cycle_id") or "not-started")[:80],
        "task_id": state.get("logical_task_id"),
        "started_at": state.get("cycle_started_at"),
        "finished_at": finished_at[:40],
        "base_sha": state.get("base_sha"),
        "substantive_iterations": int(state.get("substantive_iterations", 0)),
        "last_reviewed_head": state.get("reviewed_head"),
        "last_findings_count": int(state.get("findings_count", 0)),
        "terminal_reason": terminal_reason[:80],
        "provider_state": str(state.get("provider_state") or "unknown")[:80],
    }


def _bounded_diagnostics(*groups: Iterable[str], limit: int = 16) -> tuple[str, ...]:
    result: list[str] = []
    for group in groups:
        for item in group:
            value = str(item)[:240]
            if value not in result:
                result.append(value)
                if len(result) >= limit:
                    return tuple(result)
    return tuple(result)


def _retry_time_has_arrived(
    retry_not_before: object, *, now: datetime | None = None
) -> bool:
    if not isinstance(retry_not_before, str) or not retry_not_before:
        return False
    try:
        parsed = datetime.fromisoformat(retry_not_before.strip())
    except ValueError:
        return False
    return parsed.tzinfo is not None and (now or datetime.now(UTC)) >= parsed.astimezone(UTC)


def _serialize_identity(identity: ProcessIdentity) -> dict[str, object]:
    return {
        "pid": identity.pid,
        "start_time": identity.start_time,
        "executable": str(identity.executable),
        "argv": list(identity.argv[:32]),
        "cwd": str(identity.cwd),
        "process_group": identity.process_group,
    }


def _deserialize_identity(value: object) -> ProcessIdentity | None:
    if not isinstance(value, Mapping):
        return None
    pid = value.get("pid")
    start_time = value.get("start_time")
    executable = value.get("executable")
    argv = value.get("argv")
    cwd = value.get("cwd")
    process_group = value.get("process_group")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(start_time, (int, float))
        or isinstance(start_time, bool)
        or not isinstance(executable, str)
        or not executable
        or not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) for item in argv)
        or len(argv) > 32
        or not isinstance(cwd, str)
        or not cwd
        or (process_group is not None and not isinstance(process_group, int))
    ):
        return None
    return ProcessIdentity(
        pid=pid,
        start_time=float(start_time),
        executable=Path(executable),
        argv=tuple(argv),
        cwd=Path(cwd),
        process_group=process_group,
    )


def _provider_error_state(code: str) -> IntegrationState:
    if code.endswith(("NOT_CONFIGURED", "NOT_FOUND")):
        return IntegrationState.NOT_CONFIGURED
    if code.endswith(("INCOMPATIBLE", "WRAPPER_REJECTED")):
        return IntegrationState.INCOMPATIBLE
    if code.endswith("AUTH_NOT_READY"):
        return IntegrationState.UNAUTHENTICATED
    return IntegrationState.UNAVAILABLE


class CodeRabbitAdapter(IntegrationAdapter):
    """Direct native provider lifecycle bound to the canonical checkout."""

    name = IntegrationName.CODERABBIT

    def __init__(self, *, runner: StructuredProcessRunner | None = None) -> None:
        self.runner = runner or StructuredProcessRunner()

    @staticmethod
    def _state_layout(root: Path) -> StateLayout:
        return StateLayout.for_repository(root)

    @staticmethod
    def _state_path(root: Path) -> Path:
        return CodeRabbitAdapter._state_layout(root).path(_STATE_FILE_NAME)

    @staticmethod
    def _safe_task_id(value: object) -> str | None:
        return value if isinstance(value, str) and _TASK_ID_RE.fullmatch(value) else None

    @staticmethod
    def _safe_sha(value: object) -> str | None:
        return value if isinstance(value, str) and _SHA_RE.fullmatch(value) else None

    @staticmethod
    def _normalise_state(payload: Mapping[str, object], *, migrated: bool) -> dict[str, object]:
        state = _default_review_state()
        for key in state:
            if key in payload:
                state[key] = payload[key]
        state["schema_version"] = _REVIEW_SCHEMA_VERSION
        state["current_cycle_id"] = (
            payload.get("current_cycle_id")
            if isinstance(payload.get("current_cycle_id"), str)
            and _CYCLE_ID_RE.fullmatch(str(payload.get("current_cycle_id")))
            else (_legacy_cycle_id(payload) if migrated else "not-started")
        )
        state["logical_task_id"] = CodeRabbitAdapter._safe_task_id(payload.get("logical_task_id"))
        iterations = payload.get("substantive_iterations", payload.get("iterations", 0))
        state["substantive_iterations"] = (
            max(0, min(int(iterations), MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE))
            if isinstance(iterations, int) and not isinstance(iterations, bool)
            else 0
        )
        state["iterations"] = state["substantive_iterations"]
        state["base_sha"] = CodeRabbitAdapter._safe_sha(payload.get("base_sha"))
        state["last_head"] = CodeRabbitAdapter._safe_sha(payload.get("last_head"))
        state["reviewed_head"] = CodeRabbitAdapter._safe_sha(payload.get("reviewed_head"))
        state["repository_identity"] = (
            str(payload.get("repository_identity"))[:256]
            if isinstance(payload.get("repository_identity"), str)
            else None
        )
        state["root_identity"] = (
            str(payload.get("root_identity"))[:64]
            if isinstance(payload.get("root_identity"), str)
            else None
        )
        state["cycle_status"] = _bounded_string(payload.get("cycle_status"), "fresh", 80)
        state["provider_state"] = (
            _bounded_string(payload.get("provider_state"), "unknown", 80)
            if payload.get("provider_state") is not None
            else None
        )
        state["provider_version"] = (
            _bounded_string(payload.get("provider_version"), "", 80) or None
        )
        state["terminal"] = bool(payload.get("terminal", False))
        state["attempt"] = (
            max(0, min(int(payload.get("attempt", 0)), _MAX_REVIEW_ATTEMPTS))
            if isinstance(payload.get("attempt", 0), int)
            else 0
        )
        state["findings"] = []
        raw_findings = payload.get("findings")
        if isinstance(raw_findings, list):
            for raw in raw_findings[:_MAX_RETAINED_FINDINGS]:
                try:
                    state["findings"].append(CodeRabbitFinding.model_validate(raw).model_dump(mode="json"))
                except Exception:  # noqa: BLE001, S112 - corrupted finding is discarded.
                    continue
        state["findings_count"] = len(state["findings"])
        state["findings_digest"] = (
            str(payload.get("findings_digest"))[:64]
            if isinstance(payload.get("findings_digest"), str)
            else None
        )
        state["previous_cycles"] = []
        raw_previous = payload.get("previous_cycles")
        if isinstance(raw_previous, list):
            state["previous_cycles"] = [
                item for item in raw_previous[-MAX_RETAINED_REVIEW_CYCLES:]
                if isinstance(item, dict)
            ]
        quota = payload.get("provider_quota")
        if isinstance(quota, Mapping):
            state["provider_quota"] = {
                "state": _bounded_string(quota.get("state"), "unknown", 80),
                "rate_limited_at": quota.get("rate_limited_at")
                if isinstance(quota.get("rate_limited_at"), str)
                else None,
                "retry_not_before": quota.get("retry_not_before")
                if isinstance(quota.get("retry_not_before"), str)
                else None,
                "retry_source": quota.get("retry_source")
                if quota.get("retry_source") in _RETRY_SOURCES
                else "unknown",
            }
        # Старый provider payload не является доказательством живого native
        # процесса.  Только текущая schema с корректной identity сохраняет active.
        if not migrated and payload.get("schema_version") == _REVIEW_SCHEMA_VERSION:
            state["active"] = bool(payload.get("active", False))
            state["provider_identity"] = payload.get("provider_identity")
            state["candidate_fingerprint"] = payload.get("candidate_fingerprint")
        else:
            state["active"] = False
            state["provider_identity"] = None
            state["candidate_fingerprint"] = None
            if payload.get("active"):
                state["provider_state"] = "legacy_state_migrated"
                state["cycle_status"] = "recovery_required"
        return state

    @classmethod
    def _load_review_state(cls, root: Path) -> dict[str, object]:
        path = cls._state_path(root)
        if path_has_link(path):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "CodeRabbit state содержит symlink или reparse point.",
            )
        if not path.exists():
            return _default_review_state()
        try:
            raw = bounded_read_text(path, max_bytes=_MAX_STATE_BYTES)
            payload = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "CodeRabbit state повреждён или имеет неизвестный формат.",
            ) from exc
        if not isinstance(payload, dict):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "CodeRabbit state должен быть JSON object.",
            )
        schema = payload.get("schema_version", 1)
        if not isinstance(schema, int) or schema not in {1, 2, _REVIEW_SCHEMA_VERSION}:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "CodeRabbit state имеет неподдерживаемую schema.",
            )
        return cls._normalise_state(payload, migrated=schema != _REVIEW_SCHEMA_VERSION)

    @classmethod
    def _save_review_state(cls, root: Path, state: Mapping[str, object]) -> None:
        layout = cls._state_layout(root)
        layout.ensure()
        payload = dict(_default_review_state())
        payload.update(state)
        payload["schema_version"] = _REVIEW_SCHEMA_VERSION
        payload["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        ScopedPath(layout.repository_directory).atomic_write_text(
            _STATE_FILE_NAME,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    @staticmethod
    def _cycle_summary(state: Mapping[str, object]) -> CodeRabbitCycleSummary:
        cycle_id = state.get("current_cycle_id")
        if not isinstance(cycle_id, str) or not _CYCLE_ID_RE.fullmatch(cycle_id):
            cycle_id = "not-started"
        iterations = state.get("substantive_iterations", 0)
        iterations = max(0, min(int(iterations), MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE)) if isinstance(iterations, int) else 0
        previous = state.get("previous_cycles")
        last_head = state.get("reviewed_head")
        return CodeRabbitCycleSummary(
            cycle_id=cycle_id,
            task_id=state.get("logical_task_id") if isinstance(state.get("logical_task_id"), str) else None,
            cycle_status=_bounded_string(state.get("cycle_status"), "fresh", 80),
            substantive_iterations=iterations,
            provider_state=_bounded_string(state.get("provider_state"), "unknown", 80),
            rate_limited_at=state.get("rate_limited_at") if isinstance(state.get("rate_limited_at"), str) else None,
            retry_not_before=state.get("retry_not_before") if isinstance(state.get("retry_not_before"), str) else None,
            retry_source=state.get("retry_source") if state.get("retry_source") in _RETRY_SOURCES else "unknown",
            last_reviewed_head=last_head if isinstance(last_head, str) and _SHA_RE.fullmatch(last_head) else None,
            previous_cycles_retained=len(previous) if isinstance(previous, list) else 0,
            findings_count=max(0, min(int(state.get("findings_count", 0)), _MAX_RETAINED_FINDINGS))
            if isinstance(state.get("findings_count", 0), int)
            else 0,
            terminal=bool(state.get("terminal", False)),
            active=bool(state.get("active", False)),
        )

    @staticmethod
    def _evidence_config(settings: Mapping[str, object], provider: NativeCodeRabbit | None = None) -> dict[str, object]:
        result = dict(settings)
        result["route"] = "direct_native_agent"
        result["transport"] = "native_process"
        if provider is not None:
            result["command"] = provider.display_name
        elif not isinstance(result.get("command"), str):
            result["command"] = _PROVIDER_NAME
        result.pop("executable", None)
        return result

    def _record_from_error(
        self,
        settings: Mapping[str, object],
        code: str,
        *,
        state: IntegrationState | None = None,
        diagnostics: tuple[str, ...] = (),
        message: str | None = None,
        provider: NativeCodeRabbit | None = None,
        configured: bool | None = None,
        authenticated: bool | None = None,
    ) -> IntegrationRecord:
        selected_state = state or _provider_error_state(code)
        return build_record(
            self.name,
            selected_state,
            code,
            message or "Прямой native CodeRabbit provider не подтверждён.",
            build_evidence(
                config=self._evidence_config(settings, provider),
                credential=CredentialRef(),
                configured=(configured if configured is not None else selected_state is not IntegrationState.NOT_CONFIGURED),
                reachable=selected_state in {IntegrationState.READY, IntegrationState.RATE_LIMITED},
                authenticated=authenticated,
                diagnostics=diagnostics,
            ),
        )

    @staticmethod
    def _candidate_reason(error: ToolingError) -> tuple[str, str]:
        mapping = {
            ResultCode.TOOLING_REPOSITORY_INVALID: "CODERABBIT_CANONICAL_ROOT_INVALID",
            ResultCode.TOOLING_GIT_FAILED: "CODERABBIT_GIT_PRECHECK_FAILED",
            ResultCode.TOOLING_PRECONDITION_FAILED: "CODERABBIT_CANDIDATE_PRECONDITION_FAILED",
            ResultCode.TOOLING_VERIFICATION_UNKNOWN: "CODERABBIT_CANDIDATE_VERIFICATION_UNKNOWN",
        }
        return mapping.get(error.code, "CODERABBIT_CANDIDATE_PRECHECK_FAILED"), error.message

    @staticmethod
    def _canonical_checkout_preflight(
        root: Path, settings: Mapping[str, object]
    ) -> tuple[CanonicalCheckout | None, str | None, str | None]:
        try:
            canonical_root = root.resolve(strict=False)
            git = GitClient(canonical_root)
            git_root = Path(git.text("rev-parse", "--show-toplevel")).resolve(strict=False)
            if os.path.normcase(str(git_root)) != os.path.normcase(str(canonical_root)):
                return None, "CODERABBIT_CANONICAL_ROOT_MISMATCH", "Provider cwd не является canonical checkout."
            repository_identity = git.remote_identity("origin")
            expected_repository = settings.get("repository")
            if isinstance(expected_repository, str) and expected_repository.strip() and repository_identity != expected_repository.strip():
                return None, "CODERABBIT_REPOSITORY_IDENTITY_MISMATCH", "Git repository identity не совпадает с ожидаемой."
            head_sha = git.head()
            if not _SHA_RE.fullmatch(head_sha):
                return None, "CODERABBIT_HEAD_INVALID", "Текущий HEAD не является exact commit SHA."
            status = git.status_z()
            status_digest = hashlib.sha256(status.encode("utf-8")).hexdigest()
            if status:
                return None, "CODERABBIT_CANDIDATE_DIRTY", "Canonical checkout должен иметь clean index и worktree."
            return (
                CanonicalCheckout(
                    root_identity=path_identity(canonical_root),
                    repository_identity=repository_identity,
                    head_sha=head_sha,
                    status_digest=status_digest,
                ),
                None,
                None,
            )
        except ToolingError as error:
            code, message = CodeRabbitAdapter._candidate_reason(error)
            return None, code, message
        except (OSError, ValueError) as error:
            return None, "CODERABBIT_CANDIDATE_VERIFICATION_UNKNOWN", type(error).__name__

    @staticmethod
    def _candidate_preflight(
        root: Path,
        *,
        base_sha: str,
        head_sha: str,
        settings: Mapping[str, object],
    ) -> tuple[CandidateFingerprint | None, str | None, str | None]:
        if not _SHA_RE.fullmatch(base_sha) or not _SHA_RE.fullmatch(head_sha):
            return None, "CODERABBIT_SHA_INVALID", "base/head должны быть exact SHA."
        checkout, code, detail = CodeRabbitAdapter._canonical_checkout_preflight(root, settings)
        if checkout is None:
            return None, code, detail
        if checkout.head_sha != head_sha:
            return None, "CODERABBIT_CANDIDATE_HEAD_MISMATCH", "HEAD изменился до запуска provider."
        try:
            git = GitClient(root.resolve(strict=False))
            if not git.object_exists(f"{base_sha}^{{commit}}") or not git.object_exists(f"{head_sha}^{{commit}}"):
                return None, "CODERABBIT_COMMIT_NOT_FOUND", "base/head commit не подтверждены Git."
            if not git.is_ancestor(base_sha, head_sha):
                return None, "CODERABBIT_BASE_NOT_ANCESTOR", "base SHA не является ancestor exact HEAD."
        except ToolingError as error:
            reason, message = CodeRabbitAdapter._candidate_reason(error)
            return None, reason, message
        except (OSError, ValueError) as error:
            return None, "CODERABBIT_CANDIDATE_VERIFICATION_UNKNOWN", type(error).__name__
        return (
            CandidateFingerprint(
                root_identity=checkout.root_identity,
                repository_identity=checkout.repository_identity,
                base_sha=base_sha,
                head_sha=head_sha,
                status_digest=checkout.status_digest,
            ),
            None,
            None,
        )

    @staticmethod
    def _provider_path(settings: Mapping[str, object]) -> tuple[Path | None, str | None, bool]:
        configured = settings.get("executable")
        explicitly_configured = isinstance(configured, str) and bool(configured.strip())
        raw = configured.strip() if explicitly_configured else _PROVIDER_NAME
        candidate: Path | None
        raw_path = Path(raw)
        if raw_path.is_absolute() or raw_path.parent != Path("."):
            candidate = raw_path
        else:
            found = shutil.which(raw)
            candidate = Path(found) if found else None
        if candidate is None:
            return None, (
                "CODERABBIT_EXECUTABLE_NOT_FOUND"
                if explicitly_configured
                else "CODERABBIT_NATIVE_EXECUTABLE_UNAVAILABLE"
            ), explicitly_configured
        try:
            if path_has_link(candidate):
                return None, "CODERABBIT_EXECUTABLE_UNAVAILABLE", explicitly_configured
            candidate = candidate.resolve(strict=False)
        except OSError:
            return None, "CODERABBIT_EXECUTABLE_UNAVAILABLE", explicitly_configured
        if candidate.suffix.casefold() in _PROVIDER_WRAPPER_SUFFIXES:
            return None, "CODERABBIT_EXECUTABLE_WRAPPER_REJECTED", explicitly_configured
        if candidate.name.casefold() != _PROVIDER_NAME:
            return None, "CODERABBIT_EXECUTABLE_NOT_NATIVE", explicitly_configured
        if path_has_link(candidate) or not candidate.is_file():
            return None, "CODERABBIT_EXECUTABLE_UNAVAILABLE", explicitly_configured
        return candidate, None, explicitly_configured

    def _discover_provider(self, root: Path, settings: Mapping[str, object]) -> ProviderCheck:
        if os.name != "nt":
            return ProviderCheck(
                IntegrationState.INCOMPATIBLE,
                "CODERABBIT_NATIVE_WINDOWS_REQUIRED",
                "Native CodeRabbit provider доступен только на Windows host.",
                configured=False,
            )
        executable, path_error, configured = self._provider_path(settings)
        if executable is None:
            code = path_error or "CODERABBIT_EXECUTABLE_UNAVAILABLE"
            state = _provider_error_state(code)
            return ProviderCheck(
                state,
                code,
                "Native CodeRabbit executable не подтверждён.",
                configured=configured,
            )
        try:
            version_result = self.runner.run(
                ProcessSpec(
                    executable=executable,
                    argv=("--version",),
                    cwd=root,
                    timeout_seconds=30,
                    max_output_bytes=16 * 1024,
                    env={"NO_COLOR": "1"},
                )
            )
            version_match = _VERSION_OUTPUT_RE.search(version_result.stdout + "\n" + version_result.stderr)
            if version_result.timed_out or version_result.returncode != 0 or version_match is None:
                return ProviderCheck(
                    IntegrationState.INCOMPATIBLE,
                    "CODERABBIT_VERSION_INVALID",
                    "Версия native CodeRabbit не подтверждена.",
                    configured=True,
                )
            version = version_match.group(0).lstrip("v")[:80]
            help_result = self.runner.run(
                ProcessSpec(
                    executable=executable,
                    argv=("review", "--help"),
                    cwd=root,
                    timeout_seconds=30,
                    max_output_bytes=64 * 1024,
                    env={"NO_COLOR": "1"},
                )
            )
            help_text = help_result.stdout + "\n" + help_result.stderr
            if help_result.timed_out or help_result.returncode != 0 or not all(
                marker in help_text for marker in ("--agent", "--committed", "--base-commit")
            ):
                return ProviderCheck(
                    IntegrationState.INCOMPATIBLE,
                    "CODERABBIT_REVIEW_SYNTAX_UNSUPPORTED",
                    "Native CodeRabbit не подтверждает требуемый review/agent syntax.",
                    configured=True,
                )
            auth_help = self.runner.run(
                ProcessSpec(
                    executable=executable,
                    argv=("auth", "--help"),
                    cwd=root,
                    timeout_seconds=30,
                    max_output_bytes=32 * 1024,
                    env={"NO_COLOR": "1"},
                )
            )
            auth_help_text = auth_help.stdout + "\n" + auth_help.stderr
            if auth_help.timed_out or auth_help.returncode != 0 or "status" not in auth_help_text:
                return ProviderCheck(
                    IntegrationState.INCOMPATIBLE,
                    "CODERABBIT_AUTH_SYNTAX_UNSUPPORTED",
                    "Native CodeRabbit не подтверждает auth status readiness.",
                    configured=True,
                )
            auth_result = self.runner.run(
                ProcessSpec(
                    executable=executable,
                    argv=("auth", "status"),
                    cwd=root,
                    timeout_seconds=30,
                    max_output_bytes=32 * 1024,
                    env={"NO_COLOR": "1"},
                )
            )
            if auth_result.timed_out or auth_result.returncode != 0:
                return ProviderCheck(
                    IntegrationState.UNAUTHENTICATED,
                    "CODERABBIT_AUTH_NOT_READY",
                    "Native CodeRabbit executable найден, но auth readiness не подтверждён.",
                    configured=True,
                    authenticated=False,
                )
            doctor_result = self.runner.run(
                ProcessSpec(
                    executable=executable,
                    argv=("doctor",),
                    cwd=root,
                    timeout_seconds=45,
                    max_output_bytes=64 * 1024,
                    env={"NO_COLOR": "1"},
                )
            )
            if doctor_result.timed_out or doctor_result.returncode != 0:
                return ProviderCheck(
                    IntegrationState.UNAVAILABLE,
                    "CODERABBIT_READINESS_FAILED",
                    "Native CodeRabbit doctor не подтвердил readiness.",
                    configured=True,
                    authenticated=True,
                )
            provider = NativeCodeRabbit(executable, version, help_text[:64 * 1024], root, self.runner)
            return ProviderCheck(
                IntegrationState.READY,
                "CODERABBIT_NATIVE_READY",
                "Native CodeRabbit executable и agent readiness подтверждены.",
                diagnostics=(
                    "platform=windows-native",
                    f"provider_version={version}",
                    "review_syntax=agent-committed-base-commit",
                    "auth=ready",
                    "readiness=doctor_passed",
                    "transport=native_process",
                ),
                provider=provider,
                configured=True,
                authenticated=True,
            )
        except (OSError, ValueError, ToolingError) as error:
            return ProviderCheck(
                IntegrationState.UNAVAILABLE,
                "CODERABBIT_NATIVE_DISCOVERY_FAILED",
                "Native CodeRabbit discovery завершился без подтверждения.",
                diagnostics=(type(error).__name__,),
                configured=True,
            )

    @staticmethod
    def _state_active_identity(state: Mapping[str, object]) -> tuple[ProcessIdentity | None, str | None]:
        if not state.get("active"):
            return None, None
        identity = _deserialize_identity(state.get("provider_identity"))
        if identity is None:
            return None, "CODERABBIT_ACTIVE_STATE_INVALID"
        return identity, None

    def _active_record(
        self,
        settings: Mapping[str, object],
        state: Mapping[str, object],
        *,
        provider: NativeCodeRabbit | None = None,
    ) -> IntegrationRecord | None:
        identity, error = self._state_active_identity(state)
        if error:
            return self._record_from_error(
                settings,
                error,
                state=IntegrationState.UNKNOWN,
                provider=provider,
                message="Active CodeRabbit state не содержит проверяемую process identity.",
            )
        if identity is None:
            return None
        liveness = ProcessController.inspect_state(identity)
        if liveness == "alive":
            return self._record_from_error(
                settings,
                "CODERABBIT_REVIEW_STILL_ALIVE",
                state=IntegrationState.DEGRADED,
                provider=provider,
                message="CodeRabbit review уже выполняется; duplicate provider call запрещён.",
                diagnostics=("liveness=exact_alive",),
            )
        if liveness == "unknown":
            return self._record_from_error(
                settings,
                "CODERABBIT_REVIEW_LIVENESS_UNKNOWN",
                state=IntegrationState.UNKNOWN,
                provider=provider,
                message="Liveness ранее зарегистрированного CodeRabbit процесса неизвестна.",
            )
        return self._record_from_error(
            settings,
            "CODERABBIT_REVIEW_RECOVERY_REQUIRED",
            state=IntegrationState.DEGRADED,
            provider=provider,
            message="Предыдущий CodeRabbit процесс отсутствует; сначала выполните explicit recovery.",
            diagnostics=("liveness=exact_absent",),
        )

    def status(self, root: Path, config: IntegrationConfig) -> IntegrationRecord:
        settings = config.provider("coderabbit")
        state = self._load_review_state(root)
        check = self._discover_provider(root, settings)
        active = self._active_record(settings, state, provider=check.provider)
        if active is not None:
            return active
        if check.state is not IntegrationState.READY or check.provider is None:
            return self._record_from_error(
                settings,
                check.reason_code,
                state=check.state,
                diagnostics=check.diagnostics,
                message=check.message,
                configured=check.configured,
                authenticated=check.authenticated,
            )
        base_sha = state.get("base_sha")
        head_sha = state.get("last_head") or state.get("reviewed_head")
        diagnostics = check.diagnostics
        if isinstance(base_sha, str) and _SHA_RE.fullmatch(base_sha):
            if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha):
                canonical, code, detail = self._canonical_checkout_preflight(root, settings)
                if canonical is None:
                    return self._record_from_error(
                        settings,
                        code or "CODERABBIT_CANDIDATE_PRECHECK_FAILED",
                        state=IntegrationState.INCOMPATIBLE,
                        diagnostics=_bounded_diagnostics(diagnostics, (detail or "",)),
                        provider=check.provider,
                        message="Native CodeRabbit provider готов, но canonical candidate не готов.",
                        authenticated=True,
                    )
                head_sha = canonical.head_sha
            fingerprint, code, detail = self._candidate_preflight(
                root, base_sha=base_sha, head_sha=head_sha, settings=settings
            )
            if fingerprint is None:
                return self._record_from_error(
                    settings,
                    code or "CODERABBIT_CANDIDATE_PRECHECK_FAILED",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=_bounded_diagnostics(diagnostics, (detail or "",)),
                    provider=check.provider,
                    message="Native CodeRabbit provider готов, но exact candidate не готов.",
                    authenticated=True,
                )
        elif isinstance(head_sha, str) and _SHA_RE.fullmatch(head_sha):
            fingerprint, code, detail = self._candidate_preflight(
                root, base_sha=head_sha, head_sha=head_sha, settings=settings
            )
            if fingerprint is None:
                return self._record_from_error(
                    settings,
                    code or "CODERABBIT_CANDIDATE_PRECHECK_FAILED",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=_bounded_diagnostics(diagnostics, (detail or "",)),
                    provider=check.provider,
                    message="Native CodeRabbit provider готов, но canonical candidate не готов.",
                    authenticated=True,
                )
        else:
            canonical, code, detail = self._canonical_checkout_preflight(root, settings)
            if canonical is None:
                return self._record_from_error(
                    settings,
                    code or "CODERABBIT_CANDIDATE_PRECHECK_FAILED",
                    state=IntegrationState.INCOMPATIBLE,
                    diagnostics=_bounded_diagnostics(diagnostics, (detail or "",)),
                    provider=check.provider,
                    message="Native CodeRabbit provider готов, но canonical candidate не готов.",
                    authenticated=True,
                )
        cycle_state = state.get("cycle_status")
        if cycle_state in {_RATE_LIMIT_WAITING, _RATE_LIMIT_RETRY_ALLOWED}:
            return self._record_from_error(
                settings,
                "CODERABBIT_RATE_LIMITED",
                state=IntegrationState.RATE_LIMITED,
                diagnostics=diagnostics,
                provider=check.provider,
                message="CodeRabbit cycle ограничен provider rate limit.",
                authenticated=True,
            )
        return self._record_from_error(
            settings,
            "CODERABBIT_NATIVE_READY",
            state=IntegrationState.READY,
            diagnostics=_bounded_diagnostics(
                diagnostics,
                ("canonical_checkout=ready", "candidate=clean", "provider_cwd=canonical_checkout"),
            ),
            provider=check.provider,
            message=check.message,
            configured=True,
            authenticated=True,
        )

    async def probe(self, root: Path, config: IntegrationConfig) -> AdapterOutcome:
        record = self.status(root, config)
        state = self._load_review_state(root)
        findings = self._stored_findings(state, state.get("base_sha"), state.get("reviewed_head"))
        return AdapterOutcome(record, findings, self._cycle_summary(state))

    @staticmethod
    def _stored_findings(
        state: Mapping[str, object], base_sha: object, head_sha: object
    ) -> tuple[IntegrationFinding, ...]:
        raw_findings = state.get("findings")
        if not isinstance(raw_findings, list):
            return ()
        result: list[IntegrationFinding] = []
        for index, raw in enumerate(raw_findings[:_MAX_RETAINED_FINDINGS]):
            try:
                finding = CodeRabbitFinding.model_validate(raw)
            except Exception:  # noqa: BLE001, S112 - state is bounded and fail-closed.
                continue
            result.append(
                IntegrationFinding(
                    kind="coderabbit",
                    identifier=f"coderabbit-{index + 1}-{_normalized_findings_digest((finding,))[:16]}",
                    path=finding.path,
                    line=finding.line,
                    line_end=finding.line_end,
                    title=finding.title,
                    severity=finding.severity.value,
                    message=finding.impact,
                    fingerprint=_normalized_findings_digest((finding,)),
                    reviewed_head=head_sha if isinstance(head_sha, str) and _SHA_RE.fullmatch(head_sha) else None,
                    base_sha=base_sha if isinstance(base_sha, str) and _SHA_RE.fullmatch(base_sha) else None,
                    fix_head=finding.fix_head,
                    disposition=finding.disposition.value,
                    resolution=finding.resolution,
                )
            )
        return tuple(result)

    def findings(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        base_sha: str,
        head_sha: str,
    ) -> AdapterOutcome:
        settings = config.provider("coderabbit")
        state = self._load_review_state(root)
        if (
            state.get("base_sha") != base_sha
            or state.get("reviewed_head") != head_sha
            or not isinstance(state.get("reviewed_head"), str)
        ):
            record = self._record_from_error(
                settings,
                "CODERABBIT_FINDINGS_NOT_AVAILABLE",
                state=IntegrationState.NOT_CONFIGURED,
                message="Для указанного exact base/head нет сохранённых CodeRabbit findings.",
            )
            return AdapterOutcome(record, (), self._cycle_summary(state))
        findings = self._stored_findings(state, base_sha, head_sha)
        record = self._record_from_error(
            settings,
            "CODERABBIT_FINDINGS_AVAILABLE",
            state=IntegrationState.READY,
            message="Сохранённые CodeRabbit findings относятся к exact candidate.",
            configured=True,
        )
        return AdapterOutcome(record, findings, self._cycle_summary(state))

    def recover_interrupted_review(
        self, root: Path, config: IntegrationConfig
    ) -> AdapterOutcome:
        settings = config.provider("coderabbit")
        coordinator = RepositoryCoordinator.for_root(root)
        lock = coordinator.lock("coderabbit-review")
        if not lock.acquire(0):
            record = self._record_from_error(
                settings,
                "CODERABBIT_REVIEW_IN_PROGRESS",
                state=IntegrationState.DEGRADED,
                message="CodeRabbit lifecycle lock занят другой операцией.",
            )
            return AdapterOutcome(record, (), self.cycle_summary(root))
        try:
            state = self._load_review_state(root)
            identity, invalid = self._state_active_identity(state)
            if invalid:
                record = self._record_from_error(
                    settings,
                    invalid,
                    state=IntegrationState.UNKNOWN,
                    message="Recovery не может доказать exact process identity.",
                )
                return AdapterOutcome(record, (), self._cycle_summary(state))
            if identity is None:
                record = self._record_from_error(
                    settings,
                    "CODERABBIT_NO_INTERRUPTED_REVIEW",
                    state=IntegrationState.READY,
                    message="Незавершённая native CodeRabbit операция не обнаружена.",
                    configured=True,
                )
                return AdapterOutcome(record, (), self._cycle_summary(state))
            liveness = ProcessController.inspect_state(identity)
            if liveness == "alive":
                record = self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_STILL_ALIVE",
                    state=IntegrationState.DEGRADED,
                    message="Exact CodeRabbit process всё ещё выполняется; recovery не изменял state.",
                )
                return AdapterOutcome(record, (), self._cycle_summary(state))
            if liveness == "unknown":
                record = self._record_from_error(
                    settings,
                    "CODERABBIT_REVIEW_LIVENESS_UNKNOWN",
                    state=IntegrationState.UNKNOWN,
                    message="Recovery остановлен: liveness exact CodeRabbit процесса неизвестна.",
                )
                return AdapterOutcome(record, (), self._cycle_summary(state))
            now = datetime.now(UTC).isoformat(timespec="seconds")
            previous = list(state.get("previous_cycles") or [])
            previous.append(_state_summary(state, finished_at=now, terminal_reason="external_interruption_recovered"))
            state.update(
                {
                    "active": False,
                    "provider_identity": None,
                    "candidate_fingerprint": None,
                    "provider_state": "interrupted_recovered",
                    "cycle_status": "recovered",
                    "phase": "recovery",
                    "recovery": {
                        "reason": "external_interruption",
                        "operation_id": str(state.get("operation_id") or "")[:80],
                        "verified_at": now,
                    },
                    "previous_cycles": previous[-MAX_RETAINED_REVIEW_CYCLES:],
                }
            )
            self._save_review_state(root, state)
            record = self._record_from_error(
                settings,
                "CODERABBIT_REVIEW_RECOVERED",
                state=IntegrationState.READY,
                message="Отсутствующий native CodeRabbit процесс доказан; state восстановлен без duplicate call.",
                configured=True,
            )
            return AdapterOutcome(record, self._stored_findings(state, state.get("base_sha"), state.get("reviewed_head")), self._cycle_summary(state))
        finally:
            lock.release()

    def cycle_summary(self, root: Path) -> CodeRabbitCycleSummary:
        return self._cycle_summary(self._load_review_state(root))

    def start_cycle(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        base_sha: str | None = None,
        task_id: str | None = None,
    ) -> AdapterOutcome:
        settings = config.provider("coderabbit")
        if task_id is not None and _TASK_ID_RE.fullmatch(task_id) is None:
            raise ToolingError(ResultCode.TOOLING_INVALID_INVOCATION, "task_id имеет неверный формат.")
        if base_sha is not None and _SHA_RE.fullmatch(base_sha) is None:
            raise ToolingError(ResultCode.TOOLING_INVALID_INVOCATION, "base SHA должен быть exact commit SHA.")
        coordinator = RepositoryCoordinator.for_root(root)
        lock = coordinator.lock("coderabbit-review")
        if not lock.acquire(0):
            record = self._record_from_error(settings, "CODERABBIT_REVIEW_IN_PROGRESS", state=IntegrationState.DEGRADED, message="CodeRabbit lifecycle lock занят другой операцией.")
            return AdapterOutcome(record, (), self.cycle_summary(root))
        try:
            current = self._load_review_state(root)
            active = self._active_record(settings, current)
            if active is not None and current.get("active"):
                return AdapterOutcome(active, (), self._cycle_summary(current))
            current_task = current.get("logical_task_id")
            if (
                current.get("current_cycle_id") != "not-started"
                and task_id is not None
                and current_task == task_id
                and (base_sha is None or current.get("base_sha") == base_sha)
            ):
                record = self._record_from_error(settings, "CODERABBIT_CYCLE_ALREADY_BOUND", state=IntegrationState.READY, message="CodeRabbit cycle уже привязан к этой logical task; budget сохранён.", configured=True)
                return AdapterOutcome(record, (), self._cycle_summary(current))
            previous = list(current.get("previous_cycles") or [])
            if current.get("current_cycle_id") != "not-started":
                previous.append(_state_summary(current, finished_at=datetime.now(UTC).isoformat(timespec="seconds"), terminal_reason="operator_started_new_fix_cycle"))
            new_state = _default_review_state()
            new_state.update(
                {
                    "current_cycle_id": "coderabbit-cycle-" + secrets.token_hex(8),
                    "cycle_started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "cycle_status": "fresh",
                    "logical_task_id": task_id,
                    "base_sha": base_sha,
                    "provider_state": "cycle_started",
                    "previous_cycles": previous[-MAX_RETAINED_REVIEW_CYCLES:],
                    "last_event_type": "cycle_start",
                }
            )
            self._save_review_state(root, new_state)
            record = self._record_from_error(settings, "CODERABBIT_CYCLE_STARTED", state=IntegrationState.READY, message="Новый CodeRabbit review cycle создан; provider review не запускался.", configured=True)
            return AdapterOutcome(record, (), self._cycle_summary(new_state))
        finally:
            lock.release()

    def _prepare_cycle(
        self,
        root: Path,
        state: dict[str, object],
        *,
        base_sha: str,
        task_id: str | None,
        settings: Mapping[str, object],
    ) -> tuple[dict[str, object] | None, IntegrationRecord | None]:
        if task_id is not None and _TASK_ID_RE.fullmatch(task_id) is None:
            raise ToolingError(ResultCode.TOOLING_INVALID_INVOCATION, "task_id имеет неверный формат.")
        current_task = state.get("logical_task_id")
        if isinstance(current_task, str) and task_id is not None and current_task != task_id:
            return None, self._record_from_error(
                settings,
                "CODERABBIT_LOGICAL_TASK_MISMATCH",
                state=IntegrationState.INCOMPATIBLE,
                message="Текущий CodeRabbit cycle принадлежит другой logical task; требуется explicit cycle start.",
            )
        if state.get("current_cycle_id") == "not-started":
            state.update(
                {
                    "current_cycle_id": "coderabbit-cycle-" + secrets.token_hex(8),
                    "cycle_started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "cycle_status": "fresh",
                    "logical_task_id": task_id,
                    "base_sha": base_sha,
                    "provider_state": "cycle_started",
                }
            )
        elif state.get("base_sha") not in {None, base_sha}:
            return None, self._record_from_error(
                settings,
                "CODERABBIT_BASE_SHA_MISMATCH",
                state=IntegrationState.INCOMPATIBLE,
                message="Текущий CodeRabbit cycle привязан к другому base SHA.",
            )
        elif current_task is None and task_id is not None:
            state["logical_task_id"] = task_id
        return state, None

    def _finish_failure(
        self,
        root: Path,
        state: dict[str, object],
        *,
        provider_state: str,
        cycle_status: str,
        reason_code: str,
        message: str,
        integration_state: IntegrationState = IntegrationState.UNAVAILABLE,
        rate_limited_at: str | None = None,
        retry_not_before: str | None = None,
        retry_source: str = "unknown",
        diagnostics: tuple[str, ...] = (),
        settings: Mapping[str, object],
        provider: NativeCodeRabbit | None,
    ) -> AdapterOutcome:
        state.update(
            {
                "active": False,
                "provider_identity": None,
                "candidate_fingerprint": None,
                "provider_state": provider_state,
                "cycle_status": cycle_status,
                "rate_limited_at": rate_limited_at,
                "retry_not_before": retry_not_before,
                "retry_source": retry_source if retry_source in _RETRY_SOURCES else "unknown",
                "phase": "terminal" if cycle_status in {"budget_exhausted", "zero_findings"} else "failed",
                "last_event_type": "error",
            }
        )
        state["provider_quota"] = {
            "state": provider_state,
            "rate_limited_at": rate_limited_at,
            "retry_not_before": retry_not_before,
            "retry_source": retry_source if retry_source in _RETRY_SOURCES else "unknown",
        }
        self._save_review_state(root, state)
        record = self._record_from_error(
            settings,
            reason_code,
            state=integration_state,
            diagnostics=diagnostics,
            message=message,
            provider=provider,
            configured=True,
            authenticated=True,
        )
        return AdapterOutcome(record, (), self._cycle_summary(state))

    def review(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        base_sha: str,
        head_sha: str,
        task_id: str | None = None,
        progress_callback: Callable[[CodeRabbitProgress], None] | None = None,
    ) -> AdapterOutcome:
        settings = config.provider("coderabbit")
        if not _SHA_RE.fullmatch(base_sha) or not _SHA_RE.fullmatch(head_sha):
            raise ToolingError(ResultCode.TOOLING_INVALID_INVOCATION, "base/head должны быть exact SHA.")
        coordinator = RepositoryCoordinator.for_root(root)
        lock = coordinator.lock("coderabbit-review")
        if not lock.acquire(0):
            record = self._record_from_error(settings, "CODERABBIT_REVIEW_IN_PROGRESS", state=IntegrationState.DEGRADED, message="CodeRabbit lifecycle lock занят другой операцией.")
            return AdapterOutcome(record, (), self.cycle_summary(root))
        try:
            state = self._load_review_state(root)
            active = self._active_record(settings, state)
            if active is not None and state.get("active"):
                return AdapterOutcome(active, (), self._cycle_summary(state))
            prepared, cycle_error = self._prepare_cycle(
                root,
                state,
                base_sha=base_sha,
                task_id=task_id,
                settings=settings,
            )
            if cycle_error is not None or prepared is None:
                return AdapterOutcome(cycle_error or self._record_from_error(settings, "CODERABBIT_CYCLE_PREPARATION_FAILED", state=IntegrationState.UNKNOWN), (), self._cycle_summary(state))
            state = prepared
            iterations = state.get("substantive_iterations", 0)
            terminal = bool(state.get("terminal", False))
            if not isinstance(iterations, int) or not review_iteration_allowed(iterations, terminal=terminal):
                state["cycle_status"] = "budget_exhausted"
                state["terminal"] = True
                return self._finish_failure(
                    root,
                    state,
                    provider_state="budget_exhausted",
                    cycle_status="budget_exhausted",
                    reason_code="CODERABBIT_REVIEW_BUDGET_EXHAUSTED",
                    message="CodeRabbit substantive review budget исчерпан для текущего cycle.",
                    integration_state=IntegrationState.DEGRADED,
                    settings=settings,
                    provider=None,
                )
            retry_not_before = state.get("retry_not_before")
            if state.get("cycle_status") == _RATE_LIMIT_WAITING and not _retry_time_has_arrived(retry_not_before):
                return self._finish_failure(
                    root,
                    state,
                    provider_state="rate_limited",
                    cycle_status=_RATE_LIMIT_WAITING,
                    reason_code="CODERABBIT_RATE_LIMITED",
                    message="CodeRabbit cycle ожидает разрешённый retry window; blind retry запрещён.",
                    integration_state=IntegrationState.RATE_LIMITED,
                    rate_limited_at=state.get("rate_limited_at") if isinstance(state.get("rate_limited_at"), str) else None,
                    retry_not_before=retry_not_before if isinstance(retry_not_before, str) else None,
                    retry_source=state.get("retry_source") if state.get("retry_source") in _RETRY_SOURCES else "unknown",
                    settings=settings,
                    provider=None,
                )
            check = self._discover_provider(root, settings)
            if check.state is not IntegrationState.READY or check.provider is None:
                record = self._record_from_error(settings, check.reason_code, state=check.state, diagnostics=check.diagnostics, message=check.message, configured=check.configured, authenticated=check.authenticated)
                return AdapterOutcome(record, (), self._cycle_summary(state))
            provider = check.provider
            fingerprint, candidate_code, candidate_detail = self._candidate_preflight(root, base_sha=base_sha, head_sha=head_sha, settings=settings)
            if fingerprint is None:
                record = self._record_from_error(settings, candidate_code or "CODERABBIT_CANDIDATE_PRECHECK_FAILED", state=IntegrationState.INCOMPATIBLE, diagnostics=(candidate_detail or "",), provider=provider, message="Native provider готов, но exact candidate preflight не пройден.", configured=True, authenticated=True)
                return AdapterOutcome(record, (), self._cycle_summary(state))
            cycle_id = str(state.get("current_cycle_id") or "not-started")
            operation_id = "coderabbit-" + secrets.token_hex(8)
            attempt = int(state.get("attempt", 0)) + 1 if isinstance(state.get("attempt", 0), int) else 1
            started_at = datetime.now(UTC).isoformat(timespec="seconds")
            _emit_progress(progress_callback, phase="preflight", cycle_id=cycle_id, attempt=attempt, substantive_iterations=iterations, provider_state="starting", message="Exact candidate подтверждён; native provider запускается.")
            try:
                running = provider.start_review(base_sha)
            except (OSError, ValueError, ToolingError) as error:
                return self._finish_failure(root, state, provider_state="start_failed", cycle_status="provider_error", reason_code="CODERABBIT_PROVIDER_START_FAILED", message="Native CodeRabbit process не удалось безопасно запустить.", diagnostics=(type(error).__name__,), settings=settings, provider=provider)
            state.update(
                {
                    "repository_identity": fingerprint.repository_identity,
                    "root_identity": fingerprint.root_identity,
                    "base_sha": base_sha,
                    "last_head": head_sha,
                    "provider_version": provider.version,
                    "provider_state": "running",
                    "active": True,
                    "operation_id": operation_id,
                    "started_at": started_at,
                    "attempt": min(attempt, _MAX_REVIEW_ATTEMPTS),
                    "candidate_fingerprint": fingerprint.as_dict(),
                    "provider_identity": _serialize_identity(running.identity),
                    "phase": "provider",
                    "last_event_type": "provider_started",
                }
            )
            try:
                self._save_review_state(root, state)
            except Exception:
                ProcessController.terminate(running.identity)
                raise
            started_monotonic = time.monotonic()
            stop_heartbeat = threading.Event()

            def heartbeat() -> None:
                while not stop_heartbeat.wait(_HEARTBEAT_INTERVAL_SECONDS):
                    _emit_progress(progress_callback, phase="heartbeat", cycle_id=cycle_id, attempt=attempt, substantive_iterations=iterations, provider_state="running", message="Native CodeRabbit review всё ещё выполняется.", started_monotonic=started_monotonic)

            heartbeat_thread = threading.Thread(target=heartbeat, name="coderabbit-review-heartbeat", daemon=True)
            heartbeat_thread.start()
            try:
                result = running.collect(timeout_seconds=_REVIEW_TIMEOUT_SECONDS)
            finally:
                stop_heartbeat.set()
                heartbeat_thread.join(timeout=2.0)
            post_fingerprint, post_code, post_detail = self._candidate_preflight(root, base_sha=base_sha, head_sha=head_sha, settings=settings)
            candidate_unchanged = post_fingerprint is not None and post_fingerprint == fingerprint
            if not candidate_unchanged:
                state.update({"active": False, "provider_identity": None, "candidate_fingerprint": None, "provider_state": "candidate_changed", "cycle_status": "candidate_mismatch", "phase": "failed", "last_event_type": "postcondition_mismatch"})
                self._save_review_state(root, state)
                return AdapterOutcome(
                    self._record_from_error(settings, "CODERABBIT_CANDIDATE_CHANGED", state=IntegrationState.UNKNOWN, diagnostics=(post_code or "CODERABBIT_CANDIDATE_CHANGED", post_detail or ""), provider=provider, message="Canonical candidate изменился во время provider review; результат не authoritative.", configured=True, authenticated=True),
                    (),
                    self._cycle_summary(state),
                )
            if result.timed_out:
                return self._finish_failure(root, state, provider_state="timeout", cycle_status="provider_error", reason_code="CODERABBIT_REVIEW_TIMEOUT", message="Native CodeRabbit review превысил bounded timeout.", settings=settings, provider=provider)
            if result.stdout_truncated or result.stderr_truncated:
                return self._finish_failure(root, state, provider_state="stream_truncated", cycle_status="provider_error", reason_code="CODERABBIT_STREAM_TOO_LARGE", message="Native CodeRabbit stream превысил bounded limit.", settings=settings, provider=provider)
            try:
                parsed = parse_agent_ndjson(result.stdout.splitlines())
            except CodeRabbitStreamError as error:
                if error.rate_limited:
                    now = datetime.now(UTC).isoformat(timespec="seconds")
                    state["rate_limited_at"] = now
                    return self._finish_failure(root, state, provider_state="rate_limited", cycle_status=_RATE_LIMIT_WAITING, reason_code=error.code, message="Provider сообщил rate limit; cycle сохранён без расхода iteration.", integration_state=IntegrationState.RATE_LIMITED, rate_limited_at=now, retry_not_before=error.retry_not_before, retry_source=error.retry_source, diagnostics=("retry_source=" + error.retry_source,), settings=settings, provider=provider)
                return self._finish_failure(root, state, provider_state="stream_error", cycle_status="provider_error", reason_code=error.code, message="Native CodeRabbit stream не прошёл bounded parser.", settings=settings, provider=provider)
            if result.returncode != 0:
                return self._finish_failure(root, state, provider_state="provider_error", cycle_status="provider_error", reason_code="CODERABBIT_PROVIDER_FAILED", message="Native CodeRabbit завершился с ошибкой; iteration не засчитана.", settings=settings, provider=provider)
            findings = tuple(parsed.findings[:_MAX_RETAINED_FINDINGS])
            state.update(
                {
                    "active": False,
                    "provider_identity": None,
                    "candidate_fingerprint": None,
                    "provider_state": "complete",
                    "complete_received": parsed.complete,
                    "last_event_type": "complete",
                    "reviewed_head": head_sha,
                    "substantive_iterations": min(iterations + 1, MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE),
                    "iterations": min(iterations + 1, MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE),
                    "findings": [finding.model_dump(mode="json") for finding in findings],
                    "findings_count": len(findings),
                    "findings_digest": _normalized_findings_digest(findings),
                    "terminal": not findings,
                    "cycle_status": "zero_findings" if not findings else "findings_pending",
                    "phase": "terminal" if not findings else "complete",
                    "provider_quota": {"state": "available", "rate_limited_at": None, "retry_not_before": None, "retry_source": "unknown"},
                }
            )
            self._save_review_state(root, state)
            _emit_progress(progress_callback, phase="complete", cycle_id=cycle_id, attempt=attempt, substantive_iterations=iterations + 1, provider_state="complete", message="Native CodeRabbit review завершён; exact postcondition подтверждён.", started_monotonic=started_monotonic)
            integration_findings = self._stored_findings(state, base_sha, head_sha)
            record = self._record_from_error(settings, "CODERABBIT_REVIEW_COMPLETE", state=IntegrationState.READY, diagnostics=(f"reviewed_head={head_sha}", f"findings={len(findings)}", "candidate_postcondition=matched"), provider=provider, message="Native CodeRabbit review завершён на canonical checkout.", configured=True, authenticated=True)
            return AdapterOutcome(record, integration_findings, self._cycle_summary(state))
        finally:
            lock.release()


__all__ = [
    "CODERABBIT_STATE_SCHEMA",
    "REVIEW_STATE_SCHEMA_VERSION",
    "CodeRabbitAdapter",
    "CodeRabbitProgress",
    "CodeRabbitStreamError",
    "ParsedCodeRabbitReview",
    "parse_agent_ndjson",
    "parse_provider_findings_output",
    "review_iteration_allowed",
]
