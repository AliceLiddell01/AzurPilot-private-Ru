"""Ограниченная host-native граница CodeRabbit для canonical checkout.

Адаптер владеет полным lifecycle провайдера: discovery, candidate preflight,
exact process identity, bounded parsing, state и postcondition check. Он не
создаёт второй checkout и не определяет liveness по одному имени процесса.
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
from typing import Literal

from azurpilot.tooling.config import load_deploy_settings
from azurpilot.tooling.contracts import (
    CodeRabbitDeferredBacklog,
    CodeRabbitDeferredFinding,
    CodeRabbitDeferredOccurrence,
    CodeRabbitFinding,
    CodeRabbitTriageManifest,
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
from azurpilot.tooling.git import GitClient, canonical_remote_identity
from azurpilot.tooling.process import (
    ProcessController,
    ProcessIdentity,
    ProcessResult,
    ProcessSpec,
    ProcessStartError,
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
_MAX_STATE_BYTES = _MAX_REVIEW_BYTES
_MAX_TRIAGE_MANIFEST_BYTES = 128 * 1024
_MAX_BACKLOG_BYTES = 2 * 1024 * 1024
_MAX_REVIEW_ATTEMPTS = 128
_MAX_RETAINED_FINDINGS = 128
_MAX_BACKLOG_OCCURRENCES = 8
_RATE_LIMIT_MAX_SECONDS = 7 * 24 * 60 * 60
_HEARTBEAT_INTERVAL_SECONDS = 15.0
_REVIEW_TIMEOUT_SECONDS = 20 * 60
_STATE_FILE_NAME = "coderabbit-review.json"
_REVIEW_SCHEMA_VERSION = 6
_BACKLOG_FILE_NAME = ".codex/local/coderabbit-deferred-findings.json"
REVIEW_STATE_SCHEMA_VERSION = _REVIEW_SCHEMA_VERSION
CODERABBIT_STATE_SCHEMA = REVIEW_STATE_SCHEMA_VERSION
_RATE_LIMIT_WAITING = "rate_limited_waiting"
_RATE_LIMIT_RETRY_ALLOWED = "rate_limited_retry_allowed"
_RETRY_SOURCES = frozenset({"provider", "unknown"})
_BACKLOG_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|credential(?:s)?|bearer|basic|token|password|secret|cookie|authorization)\s*[:=]\s*[^\s,;)]*"
)
_BACKLOG_PERSONAL_PATH_RE = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/]|/)(?:users|home|private|var/folders)[^\s,;)]*"
)
_PROVIDER_FINDING_HEADER_RE = re.compile(
    r"^\s*(critical|major|minor|trivial|info)\s+\[[^\]]{1,160}\]\s*$",
    re.IGNORECASE,
)
_PROVIDER_FINDING_LOCATION_RE = re.compile(
    r"^\s*→\s+(.+?):([1-9][0-9]*)(?:-([1-9][0-9]*))?\s*$"
)
_PROVIDER_FINDING_SEPARATOR_RE = re.compile(r"^\s*[─-]{8,}\s*$")
_PROVIDER_WRAPPER_SUFFIXES = frozenset({".cmd", ".bat", ".ps1"})
_WINDOWS_PROVIDER_NAME = "coderabbit.exe"
_POSIX_PROVIDER_NAME = "coderabbit"
_RESERVATION_STATES = frozenset({"idle", "pre_spawn", "retryable", "owned", "unknown"})


def host_platform(host_os: str | None = None) -> Literal["windows", "posix", "unsupported"]:
    """Определить host platform без изменения process-wide platform state."""

    value = os.name if host_os is None else host_os
    if value in {"nt", "windows"}:
        return "windows"
    if value in {"posix", "posix-host", "linux", "darwin", "macos"}:
        return "posix"
    return "unsupported"


def provider_name(host_os: str | None = None) -> str:
    platform = host_platform(host_os)
    if platform == "windows":
        return _WINDOWS_PROVIDER_NAME
    return _POSIX_PROVIDER_NAME


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
    """Ограниченное операторское событие heartbeat адаптера."""

    phase: str
    cycle_id: str
    attempt: int
    substantive_iterations: int
    provider_state: str
    message: str
    elapsed_seconds: int = 0


@dataclass(frozen=True, slots=True)
class CandidateFingerprint:
    """Кандидат с точной идентичностью, снятый перед запуском provider."""

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
    """Исполняемый файл host-native с ограниченными доказательствами готовности."""

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

    def validate_repository_config(self) -> ProcessResult:
        """Проверить root `.coderabbit.yaml` официальным CLI command.

        Repository configuration автоматически обнаруживается CodeRabbit.
        Эта команда проверяет только schema; мы намеренно не передаём путь как
        `--config` в review, потому что этот flag добавляет AI-instruction
        surface, а не выбирает repository configuration.
        """

        return self.run(
            "config",
            "validate",
            ".coderabbit.yaml",
            timeout_seconds=45.0,
            max_output_bytes=64 * 1024,
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


def _redact_backlog_text(value: object, default: str, limit: int) -> str:
    """Сохранить только bounded claim без очевидных secret/path payloads."""

    text = _bounded_string(value, default, limit)
    if not text:
        return text
    text = _BACKLOG_SECRET_RE.sub("<redacted>", text)
    return _BACKLOG_PERSONAL_PATH_RE.sub("<path-redacted>", text)[:limit]


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
            if current <= parsed <= current + timedelta(
                seconds=_RATE_LIMIT_MAX_SECONDS
            ):
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
    text = str(value).casefold().strip() if value is not None else ""
    mapped = {
        "critical": FindingSeverity.CRITICAL,
        "blocker": FindingSeverity.CRITICAL,
        "high": FindingSeverity.MAJOR,
        "major": FindingSeverity.MAJOR,
        "warning": FindingSeverity.MINOR,
        "medium": FindingSeverity.MINOR,
        "minor": FindingSeverity.MINOR,
        "low": FindingSeverity.TRIVIAL,
        "trivial": FindingSeverity.TRIVIAL,
        "info": FindingSeverity.INFO,
    }.get(text)
    if mapped is None:
        raise CodeRabbitStreamError("CODERABBIT_FINDING_SEVERITY_INVALID")
    return mapped


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


def _finding_suggestions(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Сохранить bounded suggestions как untrusted text без исполнения."""

    raw = payload.get("suggestions")
    values = raw if isinstance(raw, list | tuple) else [raw]
    result: list[str] = []
    for item in values:
        if len(result) >= 16:
            break
        if isinstance(item, str):
            text = item
        elif isinstance(item, Mapping):
            text = _finding_text(item, "text", "suggestion", "comment", "description", "code")
        else:
            continue
        if isinstance(text, str) and text.strip():
            result.append(_bounded_string(text, "", 1200))
    return tuple(result)


def _parse_finding(raw: object) -> CodeRabbitFinding:
    if not isinstance(raw, Mapping):
        raise CodeRabbitStreamError("CODERABBIT_FINDING_INVALID")
    payload = raw
    path = _finding_path(payload)
    comment = _finding_text(
        payload,
        "comment",
        "impact",
        "message",
        "issue",
        "body",
        "explanation",
        "rationale",
        "problem",
        "details",
        "description",
        "text",
    )
    codegen_instructions = _finding_text(
        payload, "codegenInstructions", "codegen_instructions"
    )
    legacy_fix = _finding_text(
        payload,
        "resolution",
        "recommendation",
        "suggested_fix",
        "suggestion",
        "fix",
        "proposed_fix",
        "action",
    )
    suggestions = _finding_suggestions(payload)
    # Official agent payloads могут содержать только codegenInstructions. В
    # такой форме это actionable provider context и основной fix context;
    # comment является documented fallback. Поэтому payload только с
    # path/severity — protocol defect, а не synthetic substantive finding.
    impact_source = comment or codegen_instructions
    if not isinstance(impact_source, str) or not impact_source.strip():
        raise CodeRabbitStreamError("CODERABBIT_FINDING_INCOMPLETE")
    if isinstance(codegen_instructions, str) and codegen_instructions.strip():
        resolution_source = codegen_instructions
    elif isinstance(legacy_fix, str) and legacy_fix.strip():
        resolution_source = legacy_fix
    else:
        resolution_source = comment
    if not isinstance(resolution_source, str) or not resolution_source.strip():
        raise CodeRabbitStreamError("CODERABBIT_FINDING_INCOMPLETE")
    impact = _bounded_string(impact_source, "", 1200)
    resolution = _bounded_string(resolution_source, "", 4000)
    if not impact or not resolution:
        raise CodeRabbitStreamError("CODERABBIT_FINDING_INCOMPLETE")
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
        resolution=resolution,
        codegen_instructions=(
            _bounded_string(codegen_instructions, "", 4000)
            if isinstance(codegen_instructions, str) and codegen_instructions.strip()
            else None
        ),
        suggestions=suggestions,
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
        impact = _bounded_string(" ".join(impact_lines), "", 1200)
        resolution = _bounded_string(
            " ".join(resolution_lines) or impact,
            "",
            4000,
        )
        if not impact or not resolution:
            return
        findings.append(
            CodeRabbitFinding(
                severity=_severity(current.get("severity")),
                path=normalized_path,
                title=_bounded_string(current.get("title"), "", 160) or None,
                line=current.get("line") if isinstance(current.get("line"), int) else None,
                line_end=current.get("line_end")
                if isinstance(current.get("line_end"), int)
                else None,
                impact=impact,
                resolution=resolution,
                codegen_instructions=resolution,
                suggestions=tuple(resolution_lines[:16]),
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


def load_coderabbit_triage_manifest(path: str | os.PathLike[str]) -> CodeRabbitTriageManifest:
    """Прочитать bounded manifest только из абсолютного обычного файла."""

    manifest_path = Path(path).expanduser()
    try:
        if not manifest_path.is_absolute() or path_has_link(manifest_path):
            raise ToolingError(
                ResultCode.TOOLING_MANIFEST_INVALID,
                "CodeRabbit triage manifest должен быть абсолютным обычным файлом.",
            )
        document = json.loads(
            bounded_read_text(manifest_path, max_bytes=_MAX_TRIAGE_MANIFEST_BYTES)
        )
        return CodeRabbitTriageManifest.model_validate(document)
    except ToolingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ToolingError(
            ResultCode.TOOLING_MANIFEST_INVALID,
            "Не удалось разобрать закрытый CodeRabbit triage manifest.",
        ) from exc


def _finding_is_triaged(
    finding: CodeRabbitFinding, *, reviewed_head: object
) -> bool:
    return (
        finding.triage is not None
        and finding.disposition is not None
        and finding.disposition is finding.triage.disposition
        and isinstance(reviewed_head, str)
        and finding.triage.reviewed_head == reviewed_head
    )


def _triaged_findings_count(
    findings: Iterable[CodeRabbitFinding], *, reviewed_head: object
) -> int:
    return sum(
        _finding_is_triaged(finding, reviewed_head=reviewed_head)
        for finding in findings
    )


def _finding_requires_fix(finding: CodeRabbitFinding) -> bool:
    return finding.disposition in {
        FindingDisposition.CONFIRMED,
        FindingDisposition.PARTIALLY_CONFIRMED,
    }


def _provider_finding_payload(finding: CodeRabbitFinding) -> dict[str, object]:
    """Получить identity provider finding без mutable triage metadata."""

    return finding.model_dump(
        mode="json", exclude={"disposition", "triage", "fix_head"}
    )


def _normalized_findings_digest(findings: Iterable[CodeRabbitFinding]) -> str:
    return hashlib.sha256(
        json.dumps(
            [_provider_finding_payload(finding) for finding in findings],
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
        "reviewed_branch": None,
        "provider_state": None,
        "provider_version": None,
        "reservation_state": "idle",
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
        "historical_non_authoritative_findings": [],
        "triage_complete": False,
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
        "historical_non_authoritative_count": len(
            state.get("historical_non_authoritative_findings", [])
            if isinstance(state.get("historical_non_authoritative_findings"), list)
            else ()
        ),
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
    """Жизненный цикл прямого host-native provider в canonical checkout."""

    name = IntegrationName.CODERABBIT

    def __init__(
        self,
        *,
        runner: StructuredProcessRunner | None = None,
        host_os: str | None = None,
    ) -> None:
        self.runner = runner or StructuredProcessRunner()
        self.host_os = host_os

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
        state["reviewed_branch"] = (
            _bounded_string(payload.get("reviewed_branch"), "", 120) or None
        )
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
        reservation_state = payload.get("reservation_state")
        if reservation_state not in _RESERVATION_STATES:
            reservation_state = "unknown" if payload.get("active") else "idle"
        state["reservation_state"] = reservation_state
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
        state["historical_non_authoritative_findings"] = []
        legacy_non_authoritative = False
        raw_historical = payload.get("historical_non_authoritative_findings")
        if isinstance(raw_historical, list):
            state["historical_non_authoritative_findings"] = [
                item
                for item in raw_historical[:_MAX_RETAINED_FINDINGS]
                if isinstance(item, dict)
            ]
            legacy_non_authoritative = bool(state["historical_non_authoritative_findings"])
        raw_findings = payload.get("findings")
        if isinstance(raw_findings, list):
            for raw in raw_findings[:_MAX_RETAINED_FINDINGS]:
                candidate = dict(raw) if isinstance(raw, Mapping) else {}
                legacy_disposition = candidate.get("disposition") == "insufficient evidence"
                legacy_triage = candidate.get("triage")
                if isinstance(legacy_triage, Mapping) and legacy_triage.get("disposition") == "insufficient evidence":
                    legacy_disposition = True
                if (
                    isinstance(legacy_triage, Mapping)
                    and legacy_triage.get("disposition") == FindingDisposition.FALSE_POSITIVE.value
                    and legacy_triage.get("conflict_kind") == "task_prompt_conflict"
                ):
                    # До появления deferred/task_scope такой записью ошибочно
                    # называли scope mismatch. Лишаем её authority и оставляем
                    # provider finding для нового typed triage.
                    legacy_disposition = True
                if migrated and isinstance(legacy_triage, Mapping) and not {
                    "decision_reason",
                    "change_summary",
                }.issubset(legacy_triage):
                    legacy_disposition = True
                if legacy_disposition:
                    # Сохраняем legacy provider evidence отдельно, но лишаем его
                    # verified authority до нового exact-head triage.
                    state["historical_non_authoritative_findings"].append(candidate)
                    legacy_non_authoritative = True
                    candidate["disposition"] = None
                    candidate["triage"] = None
                    candidate["fix_head"] = None
                try:
                    # Старые state сохраняли provider/default disposition без
                    # индивидуального evidence. Это не verified triage: очистить
                    # classification и заставить lifecycle пройти через triage.
                    if not isinstance(candidate.get("triage"), Mapping):
                        candidate["disposition"] = None
                        candidate["triage"] = None
                        candidate["fix_head"] = None
                    state["findings"].append(
                        CodeRabbitFinding.model_validate(candidate).model_dump(mode="json")
                    )
                except Exception as exc:
                    if legacy_disposition:
                        # Incomplete historical provider output не должен
                        # превращать весь state в corrupted или становиться
                        # terminal evidence. Сохраняем bounded raw record.
                        continue
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "CodeRabbit state содержит повреждённый finding.",
                    ) from exc
        state["findings_count"] = len(state["findings"])
        parsed_findings = tuple(
            CodeRabbitFinding.model_validate(raw) for raw in state["findings"]
        )
        reviewed_head = state.get("reviewed_head")
        triaged_count = _triaged_findings_count(
            parsed_findings, reviewed_head=reviewed_head
        )
        state["triage_complete"] = (
            bool(parsed_findings)
            and triaged_count == len(parsed_findings)
            and not legacy_non_authoritative
        )
        if not parsed_findings and not legacy_non_authoritative:
            state["triage_complete"] = True
        elif legacy_non_authoritative or not state["triage_complete"]:
            state["terminal"] = False
            state["cycle_status"] = (
                "triage_required" if parsed_findings else "legacy_non_authoritative"
            )
            state["phase"] = "triage"
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
            if state["active"]:
                if _deserialize_identity(state["provider_identity"]) is not None:
                    state["reservation_state"] = "owned"
                elif state["reservation_state"] == "idle":
                    state["reservation_state"] = "unknown"
            elif state["reservation_state"] == "owned":
                state["reservation_state"] = "idle"
        else:
            state["active"] = False
            state["provider_identity"] = None
            state["candidate_fingerprint"] = None
            state["reservation_state"] = "idle"
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
        if not isinstance(schema, int) or schema not in {
            1,
            2,
            3,
            4,
            5,
            _REVIEW_SCHEMA_VERSION,
        }:
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
        raw_findings = state.get("findings")
        raw_items = raw_findings[:_MAX_RETAINED_FINDINGS] if isinstance(raw_findings, list) else ()
        findings = tuple(
            CodeRabbitFinding.model_validate(raw)
            for raw in raw_items
            if isinstance(raw, Mapping)
        )
        triaged_count = _triaged_findings_count(findings, reviewed_head=last_head)
        findings_count = len(findings)
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
            findings_count=findings_count,
            triaged_findings_count=triaged_count,
            triage_required=(
                findings_count > triaged_count
                or bool(state.get("historical_non_authoritative_findings"))
            ),
            historical_non_authoritative_count=(
                len(state.get("historical_non_authoritative_findings", []))
                if isinstance(state.get("historical_non_authoritative_findings"), list)
                else 0
            ),
            terminal=bool(state.get("terminal", False)),
            active=bool(state.get("active", False)),
        )

    def _evidence_config(
        self,
        settings: Mapping[str, object],
        provider: NativeCodeRabbit | None = None,
    ) -> dict[str, object]:
        result = dict(settings)
        result["route"] = "direct_native_agent"
        result["transport"] = "native_process"
        if provider is not None:
            result["command"] = provider.display_name
        elif not isinstance(result.get("command"), str):
            result["command"] = provider_name(self.host_os)
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
            message or "Прямой host-native provider CodeRabbit не подтверждён.",
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
    def _current_branch(root: Path) -> str | None:
        try:
            branch = GitClient(root).branch()
        except (OSError, ToolingError):
            return None
        return branch[:120] or None

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
            project_settings = load_deploy_settings(canonical_root)
            expected_repository = project_settings.repository_url
            if not expected_repository:
                return (
                    None,
                    "CODERABBIT_CANONICAL_REPOSITORY_UNCONFIGURED",
                    "Project-owned canonical repository identity не настроена.",
                )
            expected_identity = canonical_remote_identity(expected_repository)
            if repository_identity != expected_identity:
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
    def _provider_path(
        settings: Mapping[str, object],
        *,
        host_os: str | None = None,
    ) -> tuple[Path | None, str | None, bool]:
        configured = settings.get("executable")
        explicitly_configured = isinstance(configured, str) and bool(configured.strip())
        raw = configured.strip() if explicitly_configured else provider_name(host_os)
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
        platform = host_platform(host_os)
        path_entry_symlink = (
            not explicitly_configured
            and platform == "posix"
            and candidate.is_symlink()
        )
        try:
            if path_has_link(candidate) and not path_entry_symlink:
                return None, "CODERABBIT_EXECUTABLE_UNAVAILABLE", explicitly_configured
            candidate = candidate.resolve(strict=True)
        except OSError:
            return None, "CODERABBIT_EXECUTABLE_UNAVAILABLE", explicitly_configured
        if candidate.suffix.casefold() in _PROVIDER_WRAPPER_SUFFIXES:
            return None, "CODERABBIT_EXECUTABLE_WRAPPER_REJECTED", explicitly_configured
        if candidate.name.casefold() != provider_name(host_os):
            return None, "CODERABBIT_EXECUTABLE_NOT_NATIVE", explicitly_configured
        if path_has_link(candidate) or not candidate.is_file():
            return None, "CODERABBIT_EXECUTABLE_UNAVAILABLE", explicitly_configured
        if platform == "posix" and not os.access(candidate, os.X_OK):
            return None, "CODERABBIT_EXECUTABLE_UNAVAILABLE", explicitly_configured
        try:
            with candidate.open("rb") as stream:
                if stream.read(2) == b"#!":
                    return None, "CODERABBIT_EXECUTABLE_WRAPPER_REJECTED", explicitly_configured
        except OSError:
            return None, "CODERABBIT_EXECUTABLE_UNAVAILABLE", explicitly_configured
        return candidate, None, explicitly_configured

    def _discover_provider(
        self,
        root: Path,
        settings: Mapping[str, object],
        full_checks: bool = True,
    ) -> ProviderCheck:
        platform = host_platform(self.host_os)
        if platform == "unsupported":
            return ProviderCheck(
                IntegrationState.INCOMPATIBLE,
                "CODERABBIT_NATIVE_HOST_UNSUPPORTED",
                "Текущая host OS не поддерживает host-native provider CodeRabbit.",
                configured=False,
            )
        executable, path_error, configured = self._provider_path(
            settings, host_os=self.host_os
        )
        if executable is None:
            code = path_error or "CODERABBIT_EXECUTABLE_UNAVAILABLE"
            state = _provider_error_state(code)
            return ProviderCheck(
                state,
                code,
                "Исполняемый файл CodeRabbit не подтверждён.",
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
                    "Версия CodeRabbit не подтверждена.",
                    configured=True,
                )
            version = version_match.group(0).lstrip("v")[:80]
            if not full_checks:
                provider = NativeCodeRabbit(executable, version, "", root, self.runner)
                return ProviderCheck(
                    IntegrationState.READY,
                    "CODERABBIT_NATIVE_READY",
                    "Исполняемый файл CodeRabbit и его версия подтверждены.",
                    diagnostics=(
                        f"platform={platform}-native",
                        f"provider_version={version}",
                        "liveness=executable_version",
                        "transport=native_process",
                    ),
                    provider=provider,
                    configured=True,
                )
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
                    "CodeRabbit не подтверждает требуемый синтаксис review/agent.",
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
                    "CodeRabbit не подтверждает готовность auth status.",
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
                    "Исполняемый файл CodeRabbit найден, но готовность auth не подтверждена.",
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
                    "Проверка CodeRabbit doctor не подтвердила готовность.",
                    configured=True,
                    authenticated=True,
                )
            provider = NativeCodeRabbit(executable, version, help_text[:64 * 1024], root, self.runner)
            config_path = root / ".coderabbit.yaml"
            if not config_path.is_file() or path_has_link(config_path):
                return ProviderCheck(
                    IntegrationState.INCOMPATIBLE,
                    "CODERABBIT_CONFIG_MISSING",
                    "Repository `.coderabbit.yaml` не подтверждён в canonical checkout.",
                    diagnostics=("repository_config=.coderabbit.yaml",),
                    provider=provider,
                    configured=True,
                    authenticated=True,
                )
            config_result = provider.validate_repository_config()
            if config_result.timed_out or config_result.returncode != 0:
                return ProviderCheck(
                    IntegrationState.INCOMPATIBLE,
                    "CODERABBIT_CONFIG_INVALID",
                    "Repository `.coderabbit.yaml` не прошёл official schema validation.",
                    diagnostics=(
                        "repository_config=.coderabbit.yaml",
                        "config_validation=failed",
                    ),
                    provider=provider,
                    configured=True,
                    authenticated=True,
                )
            return ProviderCheck(
                IntegrationState.READY,
                "CODERABBIT_NATIVE_READY",
                "Исполняемый файл CodeRabbit и готовность agent подтверждены.",
                diagnostics=(
                    f"platform={platform}-native",
                    f"provider_version={version}",
                    "review_syntax=agent-committed-base-commit",
                    "auth=ready",
                    "readiness=doctor_passed",
                    "repository_config=.coderabbit.yaml",
                    "config_validation=passed",
                    "effective_config_provenance=not_observable_through_native_surface",
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
                "Поиск CodeRabbit завершился без подтверждения.",
                diagnostics=(type(error).__name__,),
                configured=True,
            )

    @staticmethod
    def _start_failure_evidence(
        error: BaseException,
    ) -> tuple[str, ProcessIdentity | None, str]:
        if isinstance(error, ProcessStartError):
            identity = error.identity if isinstance(error.identity, ProcessIdentity) else None
            return error.spawn_state, identity, error.cleanup_state
        if isinstance(error, (OSError, ValueError)):
            return "not_spawned", None, "absent"
        return "unknown", None, "unknown"

    @staticmethod
    def _state_active_identity(state: Mapping[str, object]) -> tuple[ProcessIdentity | None, str | None]:
        if not state.get("active"):
            return None, None
        identity = _deserialize_identity(state.get("provider_identity"))
        if identity is None:
            if state.get("reservation_state") in {"pre_spawn", "unknown"}:
                return None, "CODERABBIT_PROVIDER_START_UNKNOWN"
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
            message = (
                "Доказательства запуска и владения CodeRabbit неизвестны; повторный вызов provider запрещён."
                if error == "CODERABBIT_PROVIDER_START_UNKNOWN"
                else "Активное состояние CodeRabbit не содержит проверяемую идентичность процесса."
            )
            return self._record_from_error(
                settings,
                error,
                state=IntegrationState.UNKNOWN,
                provider=provider,
                message=message,
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
        check = self._discover_provider(root, settings, False)
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
                        message="Host-native provider CodeRabbit готов, но канонический кандидат не подтверждён.",
                        authenticated=check.authenticated,
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
                    message="Host-native provider CodeRabbit готов, но точный кандидат не подтверждён.",
                    authenticated=check.authenticated,
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
                    message="Host-native provider CodeRabbit готов, но канонический кандидат не подтверждён.",
                    authenticated=check.authenticated,
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
                    message="Host-native provider CodeRabbit готов, но канонический кандидат не подтверждён.",
                    authenticated=check.authenticated,
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
                authenticated=check.authenticated,
            )
        if state.get("findings") and not state.get("triage_complete", False):
            return self._record_from_error(
                settings,
                "CODERABBIT_TRIAGE_REQUIRED",
                state=IntegrationState.DEGRADED,
                diagnostics=diagnostics,
                provider=check.provider,
                message="CodeRabbit provider findings сохранены; требуется индивидуальный triage каждого finding.",
                configured=True,
                authenticated=check.authenticated,
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
            authenticated=check.authenticated,
        )

    def validate_config(self, root: Path, config: IntegrationConfig) -> AdapterOutcome:
        """Вернуть типизированное evidence отдельной проверки repository config.

        `_discover_provider` уже выполняет `config validate` через native
        executable. Этот leaf не запускает review и не изменяет state/checkout.
        """

        settings = config.provider("coderabbit")
        check = self._discover_provider(root, settings)
        if check.state is IntegrationState.READY:
            record = self._record_from_error(
                settings,
                "CODERABBIT_CONFIG_VALID",
                state=IntegrationState.READY,
                diagnostics=check.diagnostics,
                message=(
                    "Repository `.coderabbit.yaml` прошёл official schema validation; "
                    "effective global override provenance native surface не раскрывает."
                ),
                provider=check.provider,
                configured=True,
                authenticated=check.authenticated,
            )
        else:
            record = self._record_from_error(
                settings,
                check.reason_code,
                state=check.state,
                diagnostics=check.diagnostics,
                message=check.message,
                provider=check.provider,
                configured=check.configured,
                authenticated=check.authenticated,
            )
        return AdapterOutcome(record, (), self._cycle_summary(self._load_review_state(root)))

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
                    disposition=(finding.disposition.value if finding.disposition is not None else None),
                    resolution=finding.resolution,
                    codegen_instructions=finding.codegen_instructions,
                    suggestions=finding.suggestions,
                    decision_reason=(
                        finding.triage.decision_reason
                        if finding.triage is not None
                        else None
                    ),
                    change_summary=(
                        finding.triage.change_summary
                        if finding.triage is not None
                        else None
                    ),
                    conflict_kind=(
                        finding.triage.conflict_kind.value
                        if finding.triage is not None
                        and finding.triage.conflict_kind is not None
                        else None
                    ),
                    deferral_reason=(
                        finding.triage.deferral_reason.value
                        if finding.triage is not None
                        and finding.triage.deferral_reason is not None
                        else None
                    ),
                    authoritative_source=(
                        finding.triage.authoritative_source
                        if finding.triage is not None
                        else None
                    ),
                )
            )
        return tuple(result)

    @staticmethod
    def _backlog_path(root: Path) -> Path:
        return root.resolve(strict=False) / _BACKLOG_FILE_NAME

    @staticmethod
    def _backlog_repository_identity(root: Path, state: Mapping[str, object]) -> str:
        stored = state.get("repository_identity")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()[:256]
        return GitClient(root).remote_identity("origin")

    @classmethod
    def _load_deferred_backlog(
        cls, root: Path, *, repository_identity: str
    ) -> CodeRabbitDeferredBacklog:
        path = cls._backlog_path(root)
        if path_has_link(path):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "CodeRabbit deferred backlog содержит symlink или reparse point.",
            )
        if not path.exists():
            return CodeRabbitDeferredBacklog(
                repository_identity=repository_identity,
                updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
                findings=(),
            )
        if not path.is_file():
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "CodeRabbit deferred backlog не является обычным файлом.",
            )
        try:
            backlog = CodeRabbitDeferredBacklog.model_validate_json(
                bounded_read_text(path, max_bytes=_MAX_BACKLOG_BYTES)
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "CodeRabbit deferred backlog повреждён или имеет неизвестную схему.",
            ) from exc
        if backlog.repository_identity != repository_identity:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "CodeRabbit deferred backlog относится к другой repository identity.",
            )
        return backlog

    @classmethod
    def _save_deferred_backlog(
        cls, root: Path, backlog: CodeRabbitDeferredBacklog
    ) -> None:
        payload = backlog.model_dump_json(indent=2, exclude_none=True)
        if len(payload.encode("utf-8")) > _MAX_BACKLOG_BYTES:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "CodeRabbit deferred backlog превышает bounded размер.",
            )
        ScopedPath(root).atomic_write_text(_BACKLOG_FILE_NAME, payload)

    @staticmethod
    def _backlog_record(
        code: str, message: str, *, state: IntegrationState = IntegrationState.READY
    ) -> IntegrationRecord:
        return build_record(
            IntegrationName.CODERABBIT,
            state,
            code,
            message,
            build_evidence(
                config={"route": "repository_local", "transport": "filesystem"},
                credential=CredentialRef(),
                configured=True,
                reachable=True,
                diagnostics=("source=.codex/local/coderabbit-deferred-findings.json",),
            ),
        )

    @staticmethod
    def _review_branch(root: Path, state: Mapping[str, object]) -> str:
        stored = state.get("reviewed_branch")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()[:120]
        return GitClient(root).branch()[:120]

    @staticmethod
    def _deferred_entry_fingerprint(finding: CodeRabbitFinding) -> str:
        return _normalized_findings_digest((finding,))

    @classmethod
    def _upsert_deferred_backlog(
        cls,
        root: Path,
        state: Mapping[str, object],
        findings: Iterable[CodeRabbitFinding],
    ) -> CodeRabbitDeferredBacklog:
        deferred = tuple(
            finding
            for finding in findings
            if finding.disposition is FindingDisposition.DEFERRED
            and finding.triage is not None
            and finding.triage.deferral_reason is not None
        )
        repository_identity = cls._backlog_repository_identity(root, state)
        backlog = cls._load_deferred_backlog(
            root, repository_identity=repository_identity
        )
        if not deferred:
            return backlog
        branch = cls._review_branch(root, state)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        current_head = state.get("reviewed_head")
        current_base = state.get("base_sha")
        if not isinstance(current_head, str) or not _SHA_RE.fullmatch(current_head):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Deferred backlog требует exact reviewed HEAD.",
            )
        if not isinstance(current_base, str) or not _SHA_RE.fullmatch(current_base):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Deferred backlog требует exact base SHA.",
            )
        cycle_id = state.get("current_cycle_id")
        cycle_id = cycle_id if isinstance(cycle_id, str) and cycle_id else "not-started"
        task_id = state.get("logical_task_id")
        task_id = task_id if isinstance(task_id, str) and task_id else None
        provider_version = state.get("provider_version")
        provider_version = (
            provider_version if isinstance(provider_version, str) and provider_version else None
        )
        entries = {entry.fingerprint: entry for entry in backlog.findings}
        for finding in deferred:
            triage = finding.triage
            if triage is None or triage.deferral_reason is None:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Deferred finding не содержит полного typed triage evidence.",
                )
            fingerprint = cls._deferred_entry_fingerprint(finding)
            previous = entries.get(fingerprint)
            seen = bool(
                previous
                and any(
                    occurrence.reviewed_head == current_head
                    and occurrence.branch == branch
                    for occurrence in previous.occurrence_history
                )
            )
            history = list(previous.occurrence_history if previous else ())
            if not seen:
                history.append(
                    CodeRabbitDeferredOccurrence(
                        timestamp=now,
                        branch=branch,
                        reviewed_head=current_head,
                    )
                )
                history = history[-_MAX_BACKLOG_OCCURRENCES:]
            occurrence_count = (previous.occurrence_count if previous else 0) + (
                0 if seen else 1
            )
            entries[fingerprint] = CodeRabbitDeferredFinding(
                backlog_id=(
                    previous.backlog_id
                    if previous
                    else f"coderabbit-deferred-{fingerprint[:16]}"
                ),
                fingerprint=fingerprint,
                status="open",
                repository_identity=repository_identity,
                first_seen_at=previous.first_seen_at if previous else now,
                last_seen_at=now,
                reviewed_branch=branch,
                base_sha=current_base,
                reviewed_head=current_head,
                cycle_id=cycle_id,
                logical_task_id=task_id,
                provider_version=provider_version,
                path=finding.path,
                line=finding.line,
                line_end=finding.line_end,
                severity=finding.severity,
                title=_redact_backlog_text(finding.title, "", 160) or None,
                impact=_redact_backlog_text(finding.impact, "", 1200),
                resolution=_redact_backlog_text(finding.resolution, "", 4000),
                codegen_instructions=(
                    _redact_backlog_text(finding.codegen_instructions, "", 4000)
                    if finding.codegen_instructions
                    else None
                ),
                suggestions=tuple(
                    _redact_backlog_text(item, "", 1200)
                    for item in finding.suggestions
                ),
                deferral_reason=triage.deferral_reason,
                authoritative_source=_redact_backlog_text(
                    triage.authoritative_source, "task prompt", 1200
                ),
                decision_reason=_redact_backlog_text(
                    triage.decision_reason, "deferred by task scope", 2000
                ),
                occurrence_count=occurrence_count,
                occurrence_history=tuple(history),
            )
        if len(entries) > 128:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Число CodeRabbit deferred findings превышает bounded предел.",
            )
        updated = CodeRabbitDeferredBacklog(
            repository_identity=repository_identity,
            updated_at=now,
            findings=tuple(sorted(entries.values(), key=lambda item: item.backlog_id)),
        )
        cls._save_deferred_backlog(root, updated)
        return updated

    def backlog(
        self, root: Path, config: IntegrationConfig
    ) -> AdapterOutcome:
        del config
        state = self._load_review_state(root)
        repository_identity = self._backlog_repository_identity(root, state)
        backlog = self._load_deferred_backlog(
            root, repository_identity=repository_identity
        )
        return AdapterOutcome(
            self._backlog_record(
                "CODERABBIT_BACKLOG_READ",
                "Repository-local deferred CodeRabbit backlog прочитан.",
            ),
            (),
            self._cycle_summary(state),
            backlog,
        )

    def resolve_deferred_finding(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        backlog_id: str,
        fix_head: str,
        resolution_summary: str,
    ) -> AdapterOutcome:
        if not re.fullmatch(r"^coderabbit-deferred-[0-9a-f]{16,64}$", backlog_id):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "backlog id имеет неверный формат.",
            )
        if not _SHA_RE.fullmatch(fix_head):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "fix head должен быть exact commit SHA.",
            )
        if len(resolution_summary.strip()) < 12 or len(resolution_summary) > 2000:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "resolution summary должен быть bounded и содержательным.",
            )
        git = GitClient(root)
        if git.head() != fix_head or git.status_z():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Для закрытия deferred finding нужен clean exact fix HEAD текущего checkout.",
            )
        settings = config.provider("coderabbit")
        coordinator = RepositoryCoordinator.for_root(root)
        lock = coordinator.lock("coderabbit-review")
        try:
            acquired = lock.acquire(0)
        except (OSError, ToolingError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "CodeRabbit lifecycle lock недоступен для безопасного backlog resolve.",
            ) from exc
        if not acquired:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "CodeRabbit lifecycle lock занят другой операцией.",
            )
        try:
            state = self._load_review_state(root)
            repository_identity = self._backlog_repository_identity(root, state)
            backlog = self._load_deferred_backlog(
                root, repository_identity=repository_identity
            )
            selected = next(
                (entry for entry in backlog.findings if entry.backlog_id == backlog_id),
                None,
            )
            if selected is None:
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Указанный deferred finding отсутствует в repository-local backlog.",
                )
            if selected.status == "resolved":
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "Указанный deferred finding уже закрыт.",
                )
            resolved = selected.model_copy(
                update={
                    "status": "resolved",
                    "resolved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "fix_head": fix_head,
                    "resolution_summary": _redact_backlog_text(
                        resolution_summary, "resolved", 2000
                    ),
                }
            )
            resolved_entries = tuple(
                resolved if entry.backlog_id == backlog_id else entry
                for entry in backlog.findings
            )
            updated = CodeRabbitDeferredBacklog(
                repository_identity=repository_identity,
                updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
                findings=resolved_entries,
            )
            self._save_deferred_backlog(root, updated)
            return AdapterOutcome(
                self._backlog_record(
                    "CODERABBIT_BACKLOG_RESOLVED",
                    "Deferred CodeRabbit finding закрыт по exact fix HEAD.",
                ),
                (),
                self._cycle_summary(state),
                updated,
            )
        finally:
            lock.release()

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
        if findings and not state.get("triage_complete", False):
            record = self._record_from_error(
                settings,
                "CODERABBIT_TRIAGE_REQUIRED",
                state=IntegrationState.DEGRADED,
                message="Сохранённые findings CodeRabbit ещё не прошли индивидуальную проверку.",
                configured=True,
                authenticated=True,
            )
            return AdapterOutcome(record, findings, self._cycle_summary(state))
        record = self._record_from_error(
            settings,
            "CODERABBIT_FINDINGS_AVAILABLE",
            state=IntegrationState.READY,
                    message="Сохранённые findings CodeRabbit относятся к точному кандидату.",
            configured=True,
        )
        return AdapterOutcome(record, findings, self._cycle_summary(state))

    def triage(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        manifest_path: str | os.PathLike[str],
    ) -> AdapterOutcome:
        """Зафиксировать только индивидуально доказанный triage exact findings."""

        settings = config.provider("coderabbit")
        manifest = load_coderabbit_triage_manifest(manifest_path)
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
            return self._triage_unlocked(root, config, manifest=manifest)
        finally:
            lock.release()

    def _triage_unlocked(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        manifest: CodeRabbitTriageManifest,
    ) -> AdapterOutcome:
        settings = config.provider("coderabbit")
        state = self._load_review_state(root)
        if (
            state.get("base_sha") != manifest.base_sha
            or state.get("reviewed_head") != manifest.reviewed_head
        ):
            record = self._record_from_error(
                settings,
                "CODERABBIT_TRIAGE_HEAD_MISMATCH",
                state=IntegrationState.INCOMPATIBLE,
                message="Triage manifest не относится к последнему exact CodeRabbit review head.",
            )
            return AdapterOutcome(record, (), self._cycle_summary(state))

        raw_findings = state.get("findings")
        raw_items = raw_findings if isinstance(raw_findings, list) else ()
        findings = tuple(
            CodeRabbitFinding.model_validate(raw)
            for raw in raw_items
            if isinstance(raw, Mapping)
        )
        expected_indices = set(range(1, len(findings) + 1))
        entries = {entry.index: entry for entry in manifest.findings}
        if not findings or set(entries) != expected_indices:
            record = self._record_from_error(
                settings,
                "CODERABBIT_TRIAGE_INCOMPLETE",
                state=IntegrationState.DEGRADED,
                message="Для каждого finding provider требуется ровно одна evidence-запись проверки.",
                configured=True,
                authenticated=True,
            )
            return AdapterOutcome(record, self._stored_findings(state, manifest.base_sha, manifest.reviewed_head), self._cycle_summary(state))

        updated: list[CodeRabbitFinding] = []
        for index, finding in enumerate(findings, start=1):
            entry = entries[index]
            if entry.triage.reviewed_head != manifest.reviewed_head:
                record = self._record_from_error(
                    settings,
                    "CODERABBIT_TRIAGE_HEAD_MISMATCH",
                    state=IntegrationState.INCOMPATIBLE,
                    message="Каждая evidence-запись проверки должна ссылаться на точный reviewed head.",
                    configured=True,
                    authenticated=True,
                )
                return AdapterOutcome(record, self._stored_findings(state, manifest.base_sha, manifest.reviewed_head), self._cycle_summary(state))
            updated.append(
                finding.model_copy(
                    update={
                        "disposition": entry.triage.disposition,
                        "triage": entry.triage,
                        "fix_head": entry.fix_head,
                    }
                )
            )

        requires_fix = any(_finding_requires_fix(finding) for finding in updated)
        deferred_count = sum(
            finding.disposition is FindingDisposition.DEFERRED for finding in updated
        )
        iterations = state.get("substantive_iterations", 0)
        iterations = iterations if isinstance(iterations, int) else 0
        budget_exhausted = iterations >= MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE
        if budget_exhausted:
            next_cycle_status = "budget_exhausted"
            terminal = True
        elif requires_fix:
            next_cycle_status = "fixes_required"
            terminal = False
        elif deferred_count:
            next_cycle_status = "complete_with_deferred_findings"
            terminal = True
        else:
            next_cycle_status = "complete_for_current_task"
            terminal = True
        deferred_backlog = (
            self._upsert_deferred_backlog(root, state, updated)
            if deferred_count
            else None
        )
        state.update(
            {
                "findings": [finding.model_dump(mode="json") for finding in updated],
                # После индивидуального typed triage legacy-копии этого же
                # exact-head набора больше не являются неподтверждёнными.
                "historical_non_authoritative_findings": [],
                "triage_complete": True,
                "terminal": terminal,
                "cycle_status": next_cycle_status,
                "phase": "terminal" if terminal else "complete",
                "last_event_type": "triage_complete",
            }
        )
        self._save_review_state(root, state)
        if budget_exhausted:
            reason_code = "CODERABBIT_TRIAGE_COMPLETE_BUDGET_EXHAUSTED"
            integration_state = IntegrationState.DEGRADED
            message = (
                "Каждый finding проверен на последнем разрешённом review; "
                "post-fix provider confirmation отсутствует, 4/3 запрещён."
            )
        elif requires_fix:
            reason_code = "CODERABBIT_TRIAGE_COMPLETE_FIXES_REQUIRED"
            integration_state = IntegrationState.DEGRADED
            message = "Каждый finding проверен; подтверждённые findings требуют исправления и нового exact head."
        elif deferred_count:
            reason_code = "CODERABBIT_TRIAGE_COMPLETE_WITH_DEFERRED"
            integration_state = IntegrationState.READY
            message = (
                "Каждый finding проверен; применимых к текущей task findings нет, "
                "отложенные findings сохранены в repository-local backlog."
            )
        else:
            reason_code = "CODERABBIT_TRIAGE_COMPLETE_FOR_TASK"
            integration_state = IntegrationState.READY
            message = (
                "Каждый finding проверен и отклонён как false positive; "
                "текущая logical task завершена без нового exact head."
            )
        record = self._record_from_error(
            settings,
            reason_code,
            state=integration_state,
            diagnostics=(
                f"reviewed_head={manifest.reviewed_head}",
                f"findings={len(updated)}",
                "individual_triage=complete",
            ),
            message=message,
            configured=True,
            authenticated=True,
        )
        return AdapterOutcome(
            record,
            self._stored_findings(state, manifest.base_sha, manifest.reviewed_head),
            self._cycle_summary(state),
            deferred_backlog,
        )

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
                message = (
                    "Восстановление остановлено: после сбоя запуска не получена "
                    "точная идентичность процесса; повторный вызов запрещён."
                    if invalid == "CODERABBIT_PROVIDER_START_UNKNOWN"
                    else "Восстановление не может доказать точную идентичность процесса."
                )
                record = self._record_from_error(
                    settings,
                    invalid,
                    state=IntegrationState.UNKNOWN,
                    message=message,
                )
                return AdapterOutcome(record, (), self._cycle_summary(state))
            if identity is None:
                record = self._record_from_error(
                    settings,
                    "CODERABBIT_NO_INTERRUPTED_REVIEW",
                    state=IntegrationState.READY,
                    message="Незавершённая операция CodeRabbit не обнаружена.",
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
                    "reservation_state": "idle",
                    "provider_identity": None,
                    "candidate_fingerprint": None,
                    "provider_state": "interrupted_recovered",
                    "cycle_status": "recovered",
                    "phase": "idle",
                    "recovery": {
                        "reason": "external_interruption",
                        "status": "completed",
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
                message="Отсутствие процесса CodeRabbit доказано; state восстановлен без повторного вызова.",
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
        preserve_ownership: bool = False,
        ownership_liveness: str | None = None,
        settings: Mapping[str, object],
        provider: NativeCodeRabbit | None,
    ) -> AdapterOutcome:
        if preserve_ownership:
            state.update(
                {
                    "active": True,
                    "reservation_state": "owned",
                    "provider_state": provider_state,
                    "cycle_status": "recovery_required",
                    "rate_limited_at": rate_limited_at,
                    "retry_not_before": retry_not_before,
                    "retry_source": retry_source if retry_source in _RETRY_SOURCES else "unknown",
                    "phase": "recovery",
                    "recovery": {
                        "reason": "provider_timeout",
                        "status": "required",
                        "liveness": ownership_liveness or "unknown",
                        "verified_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                    "last_event_type": "ownership_preserved",
                }
            )
        else:
            state.update(
                {
                    "active": False,
                    "reservation_state": "idle",
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

    def _handle_provider_state_save_failure(
        self,
        root: Path,
        state: dict[str, object],
        running: RunningProcess,
        error: Exception,
        *,
        settings: Mapping[str, object],
        provider: NativeCodeRabbit,
    ) -> AdapterOutcome:
        """Сохранить восстановимое владение после сбоя постоянного сохранения identity."""

        diagnostics = [type(error).__name__]
        try:
            ProcessController.terminate(running.identity)
        except Exception as termination_error:  # noqa: BLE001 - recovery evidence.
            diagnostics.append(type(termination_error).__name__)
        try:
            liveness = ProcessController.inspect_state(running.identity)
        except Exception as liveness_error:  # noqa: BLE001 - fail closed.
            liveness = "unknown"
            diagnostics.append(type(liveness_error).__name__)
        diagnostics.append(f"liveness={liveness}")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if liveness == "absent":
            state.update(
                {
                    "active": False,
                    "reservation_state": "retryable",
                    "provider_identity": None,
                    "candidate_fingerprint": None,
                    "provider_state": "provider_state_save_failed_absent",
                    "cycle_status": "provider_error",
                    "phase": "failed",
                    "recovery": {
                        "reason": "provider_state_save_failed",
                        "status": "completed",
                        "liveness": "absent",
                        "next_action": "retry_review",
                        "verified_at": now,
                    },
                    "last_event_type": "provider_state_save_recovered",
                }
            )
            reason_code = "CODERABBIT_PROVIDER_STATE_SAVE_FAILED_RECOVERED"
            integration_state = IntegrationState.UNAVAILABLE
            message = (
                "Сбой сохранения identity восстановлен: отсутствие provider доказано, "
                "текущий cycle допускает безопасный повторный запуск."
            )
        else:
            state.update(
                {
                    "active": True,
                    "reservation_state": "owned",
                    "provider_identity": _serialize_identity(running.identity),
                    "provider_state": f"provider_state_save_failed_{liveness}",
                    "cycle_status": "recovery_required",
                    "phase": "recovery",
                    "recovery": {
                        "reason": "provider_state_save_failed",
                        "status": "required",
                        "liveness": liveness,
                        "next_action": "recover_interrupted_review",
                        "verified_at": now,
                    },
                    "last_event_type": "provider_state_save_recovery_required",
                }
            )
            reason_code = "CODERABBIT_PROVIDER_STATE_SAVE_RECOVERY_REQUIRED"
            integration_state = (
                IntegrationState.DEGRADED
                if liveness == "alive"
                else IntegrationState.UNKNOWN
            )
            message = (
                "Сбой сохранения identity не позволил доказать отсутствие provider; "
                "владение сохранено, повторный вызов запрещён."
            )
        try:
            self._save_review_state(root, state)
        except Exception as recovery_error:
            raise error from recovery_error
        return AdapterOutcome(
            self._record_from_error(
                settings,
                reason_code,
                state=integration_state,
                diagnostics=tuple(diagnostics),
                message=message,
                provider=provider,
                configured=True,
                authenticated=True,
            ),
            (),
            self._cycle_summary(state),
        )

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
                state,
                base_sha=base_sha,
                task_id=task_id,
                settings=settings,
            )
            if cycle_error is not None or prepared is None:
                return AdapterOutcome(cycle_error or self._record_from_error(settings, "CODERABBIT_CYCLE_PREPARATION_FAILED", state=IntegrationState.UNKNOWN), (), self._cycle_summary(state))
            state = prepared
            if state.get("historical_non_authoritative_findings"):
                return AdapterOutcome(
                    self._record_from_error(
                        settings,
                        "CODERABBIT_NEW_CYCLE_REQUIRED",
                        state=IntegrationState.INCOMPATIBLE,
                        message=(
                            "Legacy CodeRabbit findings сохранены только как historical "
                            "evidence; начните новый logical cycle перед review."
                        ),
                        configured=True,
                        authenticated=True,
                    ),
                    self._stored_findings(state, state.get("base_sha"), state.get("reviewed_head")),
                    self._cycle_summary(state),
                )
            if state.get("findings") and not state.get("triage_complete", False):
                return AdapterOutcome(
                    self._record_from_error(
                        settings,
                        "CODERABBIT_TRIAGE_REQUIRED",
                        state=IntegrationState.DEGRADED,
                        message="Перед следующим CodeRabbit review требуется индивидуально проверить каждый provider finding.",
                        configured=True,
                        authenticated=True,
                    ),
                    self._stored_findings(state, state.get("base_sha"), state.get("reviewed_head")),
                    self._cycle_summary(state),
                )
            if state.get("terminal") and state.get("cycle_status") in {
                "zero_findings",
                "complete_with_deferred_findings",
                "complete_for_current_task",
            }:
                deferred_backlog = None
                if state.get("cycle_status") == "complete_with_deferred_findings":
                    repository_identity = self._backlog_repository_identity(root, state)
                    deferred_backlog = self._load_deferred_backlog(
                        root, repository_identity=repository_identity
                    )
                if state.get("cycle_status") == "complete_with_deferred_findings":
                    reason_code = "CODERABBIT_REVIEW_COMPLETE_WITH_DEFERRED"
                    message = (
                        "Текущая logical task завершена; deferred findings сохранены "
                        "в repository-local backlog, новый exact head не требуется."
                    )
                else:
                    reason_code = "CODERABBIT_REVIEW_COMPLETE"
                    message = "Текущая logical task завершена без нового CodeRabbit review."
                return AdapterOutcome(
                    self._record_from_error(
                        settings,
                        reason_code,
                        state=IntegrationState.READY,
                        message=message,
                        configured=True,
                        authenticated=True,
                    ),
                    self._stored_findings(
                        state, state.get("base_sha"), state.get("reviewed_head")
                    ),
                    self._cycle_summary(state),
                    deferred_backlog,
                )
            if state.get("cycle_status") in {"fixes_required", "review_required"} and state.get("reviewed_head") == head_sha:
                same_head_reason = (
                    "CODERABBIT_FIX_REQUIRED"
                    if state.get("cycle_status") == "fixes_required"
                    else "CODERABBIT_NEXT_REVIEW_REQUIRED"
                )
                same_head_message = (
                    "Подтверждённые CodeRabbit findings требуют исправления и нового exact head."
                    if same_head_reason == "CODERABBIT_FIX_REQUIRED"
                    else "После triage findings требуется следующая substantive review на новом exact head."
                )
                return AdapterOutcome(
                    self._record_from_error(
                        settings,
                        same_head_reason,
                        state=IntegrationState.INCOMPATIBLE,
                        message=same_head_message,
                        configured=True,
                        authenticated=True,
                    ),
                    self._stored_findings(state, state.get("base_sha"), state.get("reviewed_head")),
                    self._cycle_summary(state),
                )
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
            has_retry_window = isinstance(retry_not_before, str) and bool(
                retry_not_before.strip()
            )
            if (
                state.get("cycle_status") == _RATE_LIMIT_WAITING
                and has_retry_window
                and not _retry_time_has_arrived(retry_not_before)
            ):
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
            if state.get("cycle_status") == _RATE_LIMIT_WAITING and not has_retry_window:
                state.update(
                    {
                        "cycle_status": _RATE_LIMIT_RETRY_ALLOWED,
                        "retry_not_before": None,
                    }
                )
                self._save_review_state(root, state)
            check = self._discover_provider(root, settings)
            if check.state is not IntegrationState.READY or check.provider is None:
                record = self._record_from_error(settings, check.reason_code, state=check.state, diagnostics=check.diagnostics, message=check.message, configured=check.configured, authenticated=check.authenticated)
                return AdapterOutcome(record, (), self._cycle_summary(state))
            provider = check.provider
            fingerprint, candidate_code, candidate_detail = self._candidate_preflight(root, base_sha=base_sha, head_sha=head_sha, settings=settings)
            if fingerprint is None:
                record = self._record_from_error(settings, candidate_code or "CODERABBIT_CANDIDATE_PRECHECK_FAILED", state=IntegrationState.INCOMPATIBLE, diagnostics=(candidate_detail or "",), provider=provider, message="Host-native provider готов, но предварительная проверка точного кандидата не пройдена.", configured=True, authenticated=True)
                return AdapterOutcome(record, (), self._cycle_summary(state))
            cycle_id = str(state.get("current_cycle_id") or "not-started")
            operation_id = "coderabbit-" + secrets.token_hex(8)
            attempt = int(state.get("attempt", 0)) + 1 if isinstance(state.get("attempt", 0), int) else 1
            started_at = datetime.now(UTC).isoformat(timespec="seconds")
            reviewed_branch = self._current_branch(root) or state.get("reviewed_branch")
            state.update(
                {
                    "repository_identity": fingerprint.repository_identity,
                    "root_identity": fingerprint.root_identity,
                    "base_sha": base_sha,
                    "last_head": head_sha,
                    "reviewed_branch": reviewed_branch,
                    "provider_version": provider.version,
                    "provider_state": "starting",
                    "active": True,
                    "reservation_state": "pre_spawn",
                    "operation_id": operation_id,
                    "started_at": started_at,
                    "attempt": min(attempt, _MAX_REVIEW_ATTEMPTS),
                    "candidate_fingerprint": fingerprint.as_dict(),
                    "provider_identity": None,
                    "phase": "starting",
                    "last_event_type": "reservation_created",
                }
            )
            # Reservation должна быть durable до любого spawn transition.
            self._save_review_state(root, state)
            _emit_progress(progress_callback, phase="preflight", cycle_id=cycle_id, attempt=attempt, substantive_iterations=iterations, provider_state="starting", message="Точный кандидат подтверждён; запускается host-native provider.")
            try:
                running = provider.start_review(base_sha)
            except (OSError, ValueError, ToolingError) as error:
                spawn_state, identity, cleanup_state = self._start_failure_evidence(error)
                retryable = spawn_state in {"not_spawned", "absent_after_cleanup"}
                state.update(
                    {
                        "active": not retryable,
                        "reservation_state": "retryable" if retryable else "unknown",
                        "provider_identity": (
                            _serialize_identity(identity) if identity is not None else None
                        ),
                        "provider_state": (
                            "start_failed_not_spawned" if retryable else "start_unknown"
                        ),
                        "cycle_status": "provider_error" if retryable else "recovery_required",
                        "phase": "failed" if retryable else "recovery",
                        "recovery": {
                            "reason": "provider_start",
                            "status": "completed" if retryable else "required",
                            "liveness": "absent" if retryable else "unknown",
                            "next_action": "retry_review" if retryable else "recover_interrupted_review",
                            "cleanup_state": cleanup_state,
                            "verified_at": datetime.now(UTC).isoformat(timespec="seconds"),
                        },
                        "last_event_type": (
                            "provider_start_failed_retryable"
                            if retryable
                            else "provider_start_unknown"
                        ),
                    }
                )
                self._save_review_state(root, state)
                record = self._record_from_error(
                    settings,
                    "CODERABBIT_PROVIDER_START_FAILED_RETRYABLE"
                    if retryable
                    else "CODERABBIT_PROVIDER_START_UNKNOWN",
                    state=IntegrationState.UNAVAILABLE
                    if retryable
                    else IntegrationState.UNKNOWN,
                    diagnostics=(
                        type(error).__name__,
                        f"spawn_state={spawn_state}",
                        f"cleanup_state={cleanup_state}",
                    ),
                    message=(
                        "Запуск CodeRabbit provider доказанно не состоялся; reservation очищена, "
                        "повторный запуск разрешён в том же cycle."
                        if retryable
                        else "Не удалось доказать отсутствие CodeRabbit provider; "
                        "владение сохранено, повторный вызов запрещён."
                    ),
                    provider=provider,
                    configured=True,
                    authenticated=True,
                )
                return AdapterOutcome(record, (), self._cycle_summary(state))
            state.update(
                {
                    "repository_identity": fingerprint.repository_identity,
                    "root_identity": fingerprint.root_identity,
                    "base_sha": base_sha,
                    "last_head": head_sha,
                    "provider_version": provider.version,
                    "provider_state": "running",
                    "active": True,
                    "reservation_state": "owned",
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
            except Exception as error:  # noqa: BLE001 - durable recovery must classify liveness.
                return self._handle_provider_state_save_failure(
                    root,
                    state,
                    running,
                    error,
                    settings=settings,
                    provider=provider,
                )
            started_monotonic = time.monotonic()
            stop_heartbeat = threading.Event()

            def heartbeat() -> None:
                while not stop_heartbeat.wait(_HEARTBEAT_INTERVAL_SECONDS):
                    _emit_progress(progress_callback, phase="heartbeat", cycle_id=cycle_id, attempt=attempt, substantive_iterations=iterations, provider_state="running", message="Проверка CodeRabbit всё ещё выполняется.", started_monotonic=started_monotonic)

            heartbeat_thread = threading.Thread(target=heartbeat, name="coderabbit-review-heartbeat", daemon=True)
            heartbeat_thread.start()
            try:
                result = running.collect(timeout_seconds=_REVIEW_TIMEOUT_SECONDS)
            finally:
                stop_heartbeat.set()
                heartbeat_thread.join(timeout=2.0)
            if result.timed_out and result.termination_state != "absent":
                liveness = result.termination_state or "unknown"
                return self._finish_failure(
                    root,
                    state,
                    provider_state=f"timeout_{liveness}",
                    cycle_status="recovery_required",
                    reason_code="CODERABBIT_REVIEW_TIMEOUT_RECOVERY_REQUIRED",
                    message="Timeout CodeRabbit не подтвердил отсутствие процесса provider; владение сохранено до восстановления.",
                    integration_state=IntegrationState.UNKNOWN
                    if liveness == "unknown"
                    else IntegrationState.DEGRADED,
                    diagnostics=(f"liveness={liveness}", "ownership=preserved"),
                    preserve_ownership=True,
                    ownership_liveness=liveness,
                    settings=settings,
                    provider=provider,
                )
            post_fingerprint, post_code, post_detail = self._candidate_preflight(root, base_sha=base_sha, head_sha=head_sha, settings=settings)
            candidate_unchanged = post_fingerprint is not None and post_fingerprint == fingerprint
            if not candidate_unchanged:
                state.update({"active": False, "reservation_state": "idle", "provider_identity": None, "candidate_fingerprint": None, "provider_state": "candidate_changed", "cycle_status": "candidate_mismatch", "phase": "failed", "last_event_type": "postcondition_mismatch"})
                self._save_review_state(root, state)
                return AdapterOutcome(
                    self._record_from_error(settings, "CODERABBIT_CANDIDATE_CHANGED", state=IntegrationState.UNKNOWN, diagnostics=(post_code or "CODERABBIT_CANDIDATE_CHANGED", post_detail or ""), provider=provider, message="Канонический кандидат изменился во время проверки provider; результат не является authoritative.", configured=True, authenticated=True),
                    (),
                    self._cycle_summary(state),
                )
            if result.timed_out:
                return self._finish_failure(root, state, provider_state="timeout", cycle_status="provider_error", reason_code="CODERABBIT_REVIEW_TIMEOUT", message="Проверка CodeRabbit превысила ограниченный timeout.", settings=settings, provider=provider)
            if result.stdout_truncated or result.stderr_truncated:
                return self._finish_failure(root, state, provider_state="stream_truncated", cycle_status="provider_error", reason_code="CODERABBIT_STREAM_TOO_LARGE", message="Поток CodeRabbit превысил ограниченный размер.", settings=settings, provider=provider)
            try:
                parsed = parse_agent_ndjson(result.stdout.splitlines())
            except CodeRabbitStreamError as error:
                if error.rate_limited:
                    now = datetime.now(UTC).isoformat(timespec="seconds")
                    state["rate_limited_at"] = now
                    retry_not_before = (
                        error.retry_not_before
                        if isinstance(error.retry_not_before, str)
                        and error.retry_not_before.strip()
                        else None
                    )
                    cycle_status = (
                        _RATE_LIMIT_WAITING
                        if retry_not_before is not None
                        else _RATE_LIMIT_RETRY_ALLOWED
                    )
                    return self._finish_failure(root, state, provider_state="rate_limited", cycle_status=cycle_status, reason_code=error.code, message="Provider сообщил rate limit; cycle сохранён без расхода iteration.", integration_state=IntegrationState.RATE_LIMITED, rate_limited_at=now, retry_not_before=retry_not_before, retry_source=error.retry_source, diagnostics=("retry_source=" + error.retry_source,), settings=settings, provider=provider)
                return self._finish_failure(root, state, provider_state="stream_error", cycle_status="provider_error", reason_code=error.code, message="Поток CodeRabbit не прошёл ограниченный разбор.", settings=settings, provider=provider)
            if result.returncode != 0:
                return self._finish_failure(root, state, provider_state="provider_error", cycle_status="provider_error", reason_code="CODERABBIT_PROVIDER_FAILED", message="CodeRabbit завершился с ошибкой; итерация не засчитана.", settings=settings, provider=provider)
            findings = tuple(parsed.findings[:_MAX_RETAINED_FINDINGS])
            state.update(
                {
                    "active": False,
                    "reservation_state": "idle",
                    "provider_identity": None,
                    "candidate_fingerprint": None,
                    "provider_state": "complete",
                    "complete_received": parsed.complete,
                    "last_event_type": "complete",
                    "reviewed_head": head_sha,
                    "reviewed_branch": reviewed_branch,
                    "substantive_iterations": min(iterations + 1, MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE),
                    "iterations": min(iterations + 1, MAX_SUBSTANTIVE_REVIEWS_PER_CYCLE),
                    "findings": [finding.model_dump(mode="json") for finding in findings],
                    "findings_count": len(findings),
                    "findings_digest": _normalized_findings_digest(findings),
                    "triage_complete": not findings,
                    "terminal": not findings,
                    "cycle_status": "zero_findings" if not findings else "triage_required",
                    "phase": "terminal" if not findings else "complete",
                    "provider_quota": {"state": "available", "rate_limited_at": None, "retry_not_before": None, "retry_source": "unknown"},
                }
            )
            self._save_review_state(root, state)
            _emit_progress(progress_callback, phase="complete", cycle_id=cycle_id, attempt=attempt, substantive_iterations=iterations + 1, provider_state="complete", message=("Проверка CodeRabbit завершена; точное postcondition подтверждено." if not findings else "Проверка CodeRabbit завершена; требуется индивидуальная проверка findings."), started_monotonic=started_monotonic)
            integration_findings = self._stored_findings(state, base_sha, head_sha)
            if findings:
                record = self._record_from_error(
                    settings,
                    "CODERABBIT_TRIAGE_REQUIRED",
                    state=IntegrationState.DEGRADED,
                    diagnostics=(
                        f"reviewed_head={head_sha}",
                        f"findings={len(findings)}",
                        "candidate_postcondition=matched",
                        "provider_complete=authoritative",
                    ),
                    provider=provider,
                    message="Проверка CodeRabbit завершена; workflow нельзя завершить без индивидуальной проверки findings.",
                    configured=True,
                    authenticated=True,
                )
            else:
                record = self._record_from_error(settings, "CODERABBIT_REVIEW_COMPLETE", state=IntegrationState.READY, diagnostics=(f"reviewed_head={head_sha}", f"findings={len(findings)}", "candidate_postcondition=matched"), provider=provider, message="Проверка CodeRabbit завершена в canonical checkout.", configured=True, authenticated=True)
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
    "host_platform",
    "load_coderabbit_triage_manifest",
    "parse_agent_ndjson",
    "parse_provider_findings_output",
    "provider_name",
    "review_iteration_allowed",
]
