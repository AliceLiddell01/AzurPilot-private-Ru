"""Прямой bounded CodeRabbit agent через явно выбранный WSL2 checkout."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
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
MAX_REVIEW_ITERATIONS = 3
_STATE_FILE_NAME = "coderabbit-review.json"


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

    def __init__(self, code: str, *, rate_limited: bool = False) -> None:
        self.code = code
        self.rate_limited = rate_limited
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ParsedCodeRabbitReview:
    findings: tuple[CodeRabbitFinding, ...]
    complete: bool
    unknown_events: tuple[str, ...] = ()


def _bounded_string(value: object, default: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return " ".join(value.split())[:limit]


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


def _parse_finding(raw: object) -> CodeRabbitFinding:
    payload = raw if isinstance(raw, dict) else {}
    path = _finding_path(payload)
    impact = _bounded_string(
        payload.get("impact")
        or payload.get("message")
        or payload.get("comment")
        or payload.get("title")
        or payload.get("description"),
        "CodeRabbit finding требует независимой проверки.",
        1200,
    )
    disposition = _disposition(
        payload.get("disposition") or payload.get("classification")
    )
    resolution = _bounded_string(
        payload.get("resolution") or payload.get("recommendation"),
        "Не применено автоматически; требуется независимая проверка.",
        1200,
    )
    return CodeRabbitFinding(
        severity=_severity(payload.get("severity") or payload.get("priority")),
        path=path,
        impact=impact,
        disposition=disposition,
        resolution=resolution,
    )


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
            findings.append(_parse_finding(event.get("finding", event)))
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
            raise CodeRabbitStreamError(
                "CODERABBIT_RATE_LIMITED"
                if any(marker in message for marker in ("rate", "429", "too many"))
                else "CODERABBIT_AGENT_ERROR",
                rate_limited=any(
                    marker in message for marker in ("rate", "429", "too many")
                ),
            )
        elif isinstance(kind, str) and _NAME_RE.fullmatch(kind):
            unknown.append(kind)
        else:
            unknown.append("unknown")
    if not complete:
        raise CodeRabbitStreamError("CODERABBIT_STREAM_TRUNCATED")
    return ParsedCodeRabbitReview(tuple(findings), complete=True, unknown_events=tuple(unknown[:16]))


def review_iteration_allowed(iterations: int, *, terminal: bool = False) -> bool:
    """Проверить bounded iteration budget до запуска provider review."""

    return not terminal and 0 <= iterations < MAX_REVIEW_ITERATIONS


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
        ok, reason = CodeRabbitAdapter()._verify_clone_identity(
            runtime, expected_repository
        )
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
        runtime.coderabbit_executable = candidate_path
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
        ok, reason = CodeRabbitAdapter()._verify_clone_state(
            runtime, expected_repository
        )
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

        if not review_state.get("operation_id") and not review_state.get("provider_state"):
            return "fresh"
        if review_state.get("active") is True:
            return "active"
        if review_state.get("complete_received") is True:
            return "complete_terminal" if review_state.get("terminal") is True else "complete_non_terminal"
        provider_state = review_state.get("provider_state")
        if provider_state == "rate_limited":
            return "rate_limited"
        if provider_state in {"failed", "timeout", "stream_error"}:
            return "incomplete_known_failure"
        return "incomplete_unknown"

    @staticmethod
    def _review_state_diagnostics(review_state: dict[str, object]) -> tuple[str, ...]:
        return (
            "review_state=" + CodeRabbitAdapter._review_state_classification(review_state),
            f"substantive_iterations={int(review_state.get('iterations', 0))}/{MAX_REVIEW_ITERATIONS}",
        )

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
        diagnostics = inventory_diagnostics + runtime_diagnostics + self._review_state_diagnostics(review_state)
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

    def _verify_clone_identity(
        self, runtime: _WslRuntime, expected_repository: str | None
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

    def _verify_clone_state(
        self,
        runtime: _WslRuntime,
        expected_repository: str | None,
        expected_head: str | None = None,
    ) -> tuple[bool, str]:
        identity_ok, identity_reason = self._verify_clone_identity(runtime, expected_repository)
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
            return {
                "schema_version": 1,
                "iterations": 0,
                "terminal": False,
                "active": False,
                "operation_id": None,
                "started_at": None,
                "complete_received": False,
                "provider_state": None,
            }
        try:
            payload = json.loads(bounded_read_text(path, max_bytes=_MAX_STATE_BYTES))
        except (OSError, UnicodeError, ValueError, ToolingError):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние CodeRabbit review повреждено.",
            )
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние CodeRabbit review имеет неверную схему.",
            )
        iterations = payload.get("iterations", 0)
        terminal = payload.get("terminal", False)
        if not isinstance(iterations, int) or not 0 <= iterations <= MAX_REVIEW_ITERATIONS:
            raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "Лимит CodeRabbit review повреждён.")
        if not isinstance(terminal, bool):
            raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "Флаг завершения CodeRabbit review повреждён.")
        active = payload.get("active", False)
        if not isinstance(active, bool):
            raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "Флаг активного CodeRabbit review повреждён.")
        complete_received = payload.get(
            "complete_received", payload.get("provider_state") == "complete"
        )
        if not isinstance(complete_received, bool):
            raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "Флаг завершения потока CodeRabbit повреждён.")
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
                    "Текстовое поле состояния CodeRabbit повреждено.",
                )
        for key in ("last_head", "base_sha", "reviewed_head"):
            value = payload.get(key)
            if value is not None and _SHA_RE.fullmatch(value) is None:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "SHA состояния CodeRabbit повреждён.",
                )
        operation_id = payload.get("operation_id")
        if active and (
            not isinstance(operation_id, str)
            or not operation_id.startswith("coderabbit-")
            or len(operation_id) > 80
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Идентификатор активного CodeRabbit review повреждён.",
            )
        if active and payload.get("started_at") is None:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Время активного CodeRabbit review отсутствует.",
            )
        if complete_received and payload.get("reviewed_head") is None:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Завершённый CodeRabbit review не содержит reviewed head.",
            )
        findings_count = payload.get("findings_count", 0)
        attempt = payload.get("attempt", iterations)
        if not isinstance(findings_count, int) or not 0 <= findings_count <= 128:
            raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "Счётчик findings CodeRabbit повреждён.")
        if not isinstance(attempt, int) or not 0 <= attempt <= MAX_REVIEW_ITERATIONS + 1:
            raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "Счётчик попыток CodeRabbit повреждён.")
        return payload

    def _state(self, root: Path) -> tuple[int, bool]:
        payload = self._load_review_state(root)
        return int(payload.get("iterations", 0)), bool(payload.get("terminal", False))

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
        rate_limit_hint: str | None = None,
    ) -> None:
        layout = StateLayout.for_repository(root)
        layout.ensure()
        scoped = ScopedPath(layout.repository_directory)
        payload = {
            "schema_version": 1,
            "iterations": iterations,
            "last_head": head,
            "terminal": terminal,
            "active": active,
            "operation_id": operation_id[:80],
            "started_at": started_at[:40],
            "attempt": attempt,
            "repository_identity": repository_identity[:512],
            "base_sha": base_sha,
            "reviewed_head": head if complete_received else None,
            "provider_state": provider_state[:80],
            "complete_received": complete_received,
            "last_event_type": last_event_type[:80],
            "findings_count": findings_count,
            "findings_digest": findings_digest,
            "rate_limit_hint": rate_limit_hint[:240] if rate_limit_hint else None,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        scoped.atomic_write_text(
            _STATE_FILE_NAME,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        )

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
    ) -> AdapterOutcome:
        if not _SHA_RE.fullmatch(base_sha) or not _SHA_RE.fullmatch(head_sha):
            raise ToolingError(ResultCode.TOOLING_INVALID_INVOCATION, "CodeRabbit base/head должны быть exact SHA.")
        settings = self._settings(config)
        review_state = self._load_review_state(root)
        iterations = int(review_state.get("iterations", 0))
        terminal = bool(review_state.get("terminal", False))
        review_classification = self._review_state_classification(review_state)
        if review_classification in {"active", "incomplete_unknown", "rate_limited"}:
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
        operation_id = f"coderabbit-{secrets.token_hex(8)}"
        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        attempt = iterations + 1
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
        )
        result = runtime.command(
            command,
            "review",
            "--agent",
            "--committed",
            "--base-commit",
            base_sha,
            timeout=20 * 60,
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
                provider_state="rate_limited" if rate_limited else "failed",
                active=False,
                complete_received=False,
                last_event_type="error",
                rate_limit_hint="provider_rate_limit_observed" if rate_limited else None,
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
                last_event_type="error",
                rate_limit_hint="provider_rate_limit_observed" if error.rate_limited else None,
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
        findings_digest = hashlib.sha256(
            json.dumps(
                [finding.model_dump(mode="json") for finding in parsed.findings],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
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
                diagnostics=runtime_diagnostics
                + (
                    f"base_sha={base_sha}",
                    f"reviewed_head={head_sha}",
                    f"operation_id={operation_id}",
                    "untrusted_provider_text_not_executed",
                ),
            ),
        )
        findings = tuple(
            IntegrationFinding(
                kind="coderabbit",
                identifier=f.path,
                path=f.path,
                severity=f.severity.value,
                message=f.impact[:400],
                reviewed_head=head_sha,
                base_sha=base_sha,
                disposition=f.disposition.value,
                resolution=f.resolution[:400],
            )
            for f in parsed.findings
        )
        return AdapterOutcome(record, findings)


def _git_head(root: Path) -> str | None:
    try:
        return GitClient(root).head().casefold()
    except (OSError, ToolingError):
        return None


__all__ = [
    "MAX_REVIEW_ITERATIONS",
    "CodeRabbitAdapter",
    "CodeRabbitStreamError",
    "ParsedCodeRabbitReview",
    "WslDistribution",
    "WslReviewEnvironment",
    "WslSelection",
    "parse_agent_ndjson",
    "parse_wsl_verbose",
    "review_iteration_allowed",
    "select_wsl_distribution",
]
