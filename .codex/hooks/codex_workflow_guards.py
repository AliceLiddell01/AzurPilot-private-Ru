"""Ограниченные project-local guards canonical AzurPilot workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

_PROJECT_IMPORTS_AVAILABLE = True
try:
    from azurpilot.coderabbit_schema import REVIEW_STATE_SCHEMA_VERSION
    from azurpilot.tooling.operator import validate_direct_azur_invocation
    from azurpilot.tooling.ref_policy import is_workflow_guard_ref
    from azurpilot.tooling.result import ResultCode
except ImportError:
    _PROJECT_IMPORTS_AVAILABLE = False
    REVIEW_STATE_SCHEMA_VERSION = 0
    validate_direct_azur_invocation = None
    is_workflow_guard_ref = None
    ResultCode = None

_MAX_INPUT_BYTES = 128 * 1024
_MAX_COMMAND_CHARS = 64 * 1024
_MAX_STATE_BYTES = 128 * 1024
_MAX_TRANSACTION_DIRECTORIES = 128

_REVIEW_STATE_NAME = "coderabbit-review.json"
_REVIEW_SCHEMA_VERSIONS = frozenset(range(1, REVIEW_STATE_SCHEMA_VERSION + 1))
_RECOVERY_PROVIDER_STATES = frozenset(
    {
        "start_unknown",
        "timeout_unknown",
        "timeout_alive",
        "legacy_state_migrated",
    }
)
_DELIVERY_RECOVERY_PHASES = frozenset({"push_in_flight", "unknown"})

_AZUR_NAMES = frozenset({"azur", "azur.exe"})
_PYTHON_NAMES = frozenset({"py", "py.exe", "python", "python.exe", "python3"})
_UV_NAMES = frozenset({"uv", "uv.exe"})
_WSL_NAMES = frozenset({"wsl", "wsl.exe"})
_CODERABBIT_NAMES = frozenset({"coderabbit", "coderabbit.exe"})
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_WRAPPER_NAMES = frozenset(
    {
        "bash",
        "bash.exe",
        "cmd",
        "cmd.exe",
        "command",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "sh.exe",
        "zsh",
        "zsh.exe",
    }
)
_WRAPPER_COMMAND_FLAGS = frozenset(
    {"-c", "-command", "--command", "/c", "/command", "-lc", "--login-command"}
)
_UV_OPTIONS_WITH_VALUE = frozenset(
    {
        "--directory",
        "--from",
        "--package",
        "--project",
        "--python",
        "--with",
        "--with-editable",
        "--with-requirements",
    }
)
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {"-c", "-C", "--config-env", "--git-dir", "--work-tree"}
)
_GIT_READ_ONLY_BRANCH_FLAGS = frozenset(
    {
        "-a",
        "-l",
        "-r",
        "--all",
        "--contains",
        "--format",
        "--list",
        "--merged",
        "--no-merged",
        "--points-at",
        "--show-current",
        "-v",
        "-vv",
    }
)


def _basename(value: str) -> str:
    return value.strip().strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _is_literal_azur(value: str) -> bool:
    return value.strip().strip("'\"").casefold() == "azur"


def _is_azur_executable(value: str) -> bool:
    raw = value.strip().strip("'\"")
    base = _basename(raw)
    if base not in _AZUR_NAMES:
        return False
    return base != "azur" or "/" in raw.replace("\\", "/")


def _direct_azur_boundary_reason(
    tokens: tuple[str, ...], *, nested: bool
) -> str | None:
    """Применить общий operator contract к доказанному AzurPilot launcher."""

    if not tokens:
        return None
    executable = tokens[0]
    is_literal = _is_literal_azur(executable)
    is_project_executable = _is_azur_executable(executable)
    is_project_module = _is_python(executable) and _module_is_azurpilot(tokens, 1)
    if not (is_literal or is_project_executable or is_project_module):
        return None
    validation = validate_direct_azur_invocation(tokens, azur_available=True)
    if validation is ResultCode.OK and not (nested and is_literal):
        return None
    return (
        "AZUR_OPERATOR_CANONICAL_REQUIRED: внутри wrapper вызывайте azur напрямую через PATH."
        if nested and is_literal
        else "AZUR_OPERATOR_CANONICAL_REQUIRED: используйте буквальную команду azur ... через PATH."
    )


def _is_python(value: str) -> bool:
    base = _basename(value)
    return base in _PYTHON_NAMES or bool(
        re.fullmatch(r"python3(?:\.\d+)?(?:\.exe)?", base)
    )


def _is_uv(value: str) -> bool:
    return _basename(value) in _UV_NAMES


def _is_wsl(value: str) -> bool:
    return _basename(value) in _WSL_NAMES


def _is_coderabbit(value: str) -> bool:
    return _basename(value) in _CODERABBIT_NAMES


def _is_wrapper(value: str) -> bool:
    return _basename(value) in _WRAPPER_NAMES


def _strip_leading_environment_prefixes(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Убрать только ограниченные префиксы env/assignment перед анализом команды."""

    remaining = list(tokens)
    while remaining and remaining[0].casefold() in {"--", "exec", "command"}:
        remaining.pop(0)
    if remaining and remaining[0].casefold() == "env":
        remaining.pop(0)
        while remaining:
            current = remaining[0].casefold()
            if current in {"--", "-i", "--ignore-environment"}:
                remaining.pop(0)
                continue
            if current in {"-u", "--unset"} and len(remaining) > 1:
                del remaining[:2]
                continue
            if current.startswith("--unset="):
                remaining.pop(0)
                continue
            break
    while remaining and _ENV_ASSIGNMENT_RE.fullmatch(remaining[0]):
        remaining.pop(0)
    return tuple(remaining)


def _split_shell_segments(command: str) -> tuple[str, ...]:
    """Разделить только явные shell-команды, не пытаясь быть shell parser-ом."""

    segments: list[str] = []
    quote: str | None = None
    start = 0
    index = 0
    while index < len(command):
        char = command[index]
        if quote is not None:
            if char == "`" and quote == '"' and index + 1 < len(command):
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        is_separator = char in ";|\r\n"
        if char == "&":
            is_separator = bool(command[start:index].strip())
            if index + 1 < len(command) and command[index + 1] == "&":
                index += 1
                is_separator = True
        if is_separator:
            segment = command[start:index].strip()
            if segment:
                segments.append(segment)
            start = index + 1
        index += 1
    tail = command[start:].strip()
    if tail:
        segments.append(tail)
    return tuple(segments)


def _tokenize(segment: str) -> tuple[str, ...]:
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0

    def flush() -> None:
        if current:
            tokens.append("".join(current))
            current.clear()

    while index < len(segment):
        char = segment[index]
        if quote is not None:
            if char == "`" and quote == '"' and index + 1 < len(segment):
                current.append(segment[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            else:
                current.append(char)
            index += 1
            continue
        if char in "'\"":
            quote = char
        elif char.isspace():
            flush()
        elif char == "&" and not current:
            # Оператор вызова PowerShell: & "path\\to\\azur.exe".
            pass
        else:
            current.append(char)
        index += 1
    flush()
    return tuple(tokens)


def _module_is_azurpilot(tokens: tuple[str, ...], start: int = 0) -> bool:
    for index in range(start, len(tokens) - 1):
        if tokens[index].casefold() not in {"-m", "--module"}:
            continue
        module = tokens[index + 1].casefold()
        if module == "azurpilot" or module.startswith("azurpilot."):
            return True
    return False


def _uv_payload(tokens: tuple[str, ...]) -> tuple[str, ...]:
    try:
        run_index = next(
            index
            for index, token in enumerate(tokens[1:], 1)
            if token.casefold() == "run"
        )
    except StopIteration:
        return ()
    index = run_index + 1
    while index < len(tokens):
        token = tokens[index]
        lowered = token.casefold()
        if token == "--":
            return tokens[index + 1 :]
        if lowered in _UV_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if lowered.startswith("--"):
            index += 1
            continue
        return tokens[index:]
    return ()


def _git_subcommand(tokens: tuple[str, ...]) -> tuple[str | None, int]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return (
                (tokens[index + 1].casefold(), index + 1)
                if index + 1 < len(tokens)
                else (None, index)
            )
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token.casefold(), index
    return None, index


def _has_ad_hoc_ref(tokens: tuple[str, ...]) -> bool:
    for token in tokens:
        for candidate in (token, *token.split(":")):
            normalized = candidate
            lowered = normalized.casefold()
            if lowered.startswith("refs/heads/"):
                normalized = normalized[len("refs/heads/") :]
            elif lowered.startswith("refs/remotes/"):
                parts = normalized.split("/", 3)
                normalized = parts[3] if len(parts) == 4 else normalized
            if is_workflow_guard_ref(normalized):
                return True
    return False


def _classify_git(tokens: tuple[str, ...]) -> str | None:
    if not tokens or _basename(tokens[0]) not in {"git", "git.exe"}:
        return None
    subcommand, index = _git_subcommand(tokens)
    if subcommand is None or not _has_ad_hoc_ref(tokens[index + 1 :]):
        return None
    if subcommand in {"push", "update-ref"}:
        return "AD_HOC_REMOTE_TOPOLOGY_BLOCKED: используйте реальную branch через typed azur delivery."
    if subcommand == "worktree":
        return (
            (
                "AD_HOC_REMOTE_TOPOLOGY_BLOCKED: не создавайте temporary/scratch/transport/helper ref; "
                "используйте canonical branch."
            )
            if any(token.casefold() == "add" for token in tokens[index + 1 :])
            else None
        )
    if subcommand == "branch":
        flags = {
            token.casefold().split("=", 1)[0]
            for token in tokens[index + 1 :]
            if token.startswith("-")
        }
        if flags & _GIT_READ_ONLY_BRANCH_FLAGS:
            return None
        return "AD_HOC_REMOTE_TOPOLOGY_BLOCKED: не создавайте temporary/scratch/transport/helper ref; используйте canonical branch."
    if subcommand in {"checkout", "switch"}:
        create_flags = {"-b", "-B", "-c", "-C", "--branch", "--create", "--orphan"}
        if any(
            token.casefold().split("=", 1)[0] in create_flags
            for token in tokens[index + 1 :]
        ):
            return "AD_HOC_REMOTE_TOPOLOGY_BLOCKED: не создавайте temporary/scratch/transport/helper ref; используйте canonical branch."
    return None


def _starts_coderabbit(tokens: tuple[str, ...], depth: int = 0) -> bool:
    if not tokens or depth > 2:
        return False
    tokens = _strip_leading_environment_prefixes(tuple(tokens))
    if not tokens:
        return False
    if _is_coderabbit(tokens[0]):
        return True
    if not _is_wrapper(tokens[0]):
        return False
    for index, token in enumerate(tokens[1:], 1):
        if token.casefold() in _WRAPPER_COMMAND_FLAGS:
            inner = " ".join(tokens[index + 1 :])
            return any(
                _starts_coderabbit(_tokenize(segment), depth + 1)
                for segment in _split_shell_segments(inner)
            )
    return False


def _wsl_coderabbit(tokens: tuple[str, ...]) -> bool:
    wsl_options_with_value = frozenset(
        {"-d", "--distribution", "-u", "--user", "-e", "--exec", "--cd"}
    )
    for index, token in enumerate(tokens):
        if not _is_wsl(token):
            continue
        tail = tokens[index + 1 :]
        if "--" in tail:
            return _starts_coderabbit(tail[tail.index("--") + 1 :])
        cursor = 0
        while cursor < len(tail):
            current = tail[cursor].casefold()
            if current in wsl_options_with_value:
                return _starts_coderabbit(tail[cursor + 1 :])
            if current.startswith("-"):
                cursor += 1
                continue
            return _starts_coderabbit(tail[cursor:])
    return False


def _classify_tokens(
    tokens: tuple[str, ...], *, nested: bool = False, depth: int = 0
) -> str | None:
    if not tokens or depth > 3:
        return None
    tokens = _strip_leading_environment_prefixes(tokens)
    if not tokens:
        return None
    if _wsl_coderabbit(tokens):
        return "CODERABBIT_WSL_ROUTE_BLOCKED: используйте штатный host-native workflow CodeRabbit через azur integrations coderabbit."
    if _starts_coderabbit(tokens):
        return "CODERABBIT_OPERATOR_BYPASS_BLOCKED: используйте штатный host-native workflow CodeRabbit через azur integrations coderabbit."
    git_reason = _classify_git(tokens)
    if git_reason:
        return git_reason

    direct_reason = _direct_azur_boundary_reason(tokens, nested=nested)
    if direct_reason:
        return direct_reason
    executable = tokens[0]
    if _is_uv(executable):
        payload = _uv_payload(tokens)
        if payload:
            return _classify_tokens(payload, nested=True, depth=depth + 1)
        return None
    if _is_wrapper(executable):
        for index, token in enumerate(tokens[1:], 1):
            if token.casefold() not in _WRAPPER_COMMAND_FLAGS:
                continue
            inner = " ".join(tokens[index + 1 :])
            for segment in _split_shell_segments(inner):
                reason = _classify_tokens(
                    _tokenize(segment), nested=True, depth=depth + 1
                )
                if reason:
                    return reason
            return None
    return None


def classify_command(command: str) -> str | None:
    """Вернуть machine/actionable reason только для доказанного обхода."""

    if not isinstance(command, str) or len(command) > _MAX_COMMAND_CHARS:
        return None
    for segment in _split_shell_segments(command):
        reason = _classify_tokens(_tokenize(segment))
        if reason:
            return reason
    return None


def _read_json_file(
    path: Path, *, max_bytes: int = _MAX_STATE_BYTES
) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _repository_root(cwd: object) -> Path | None:
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        candidate = Path(cwd).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for root in (candidate, *candidate.parents):
        hooks = root / ".codex" / "hooks.json"
        try:
            if hooks.is_file() and not hooks.is_symlink():
                return root
        except OSError:
            return None
    return None


def _state_base() -> Path | None:
    configured = os.environ.get("AZURPILOT_STATE_HOME")
    if configured:
        try:
            path = Path(configured).expanduser()
        except (OSError, RuntimeError, ValueError):
            return None
        return path if path.is_absolute() else None
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("PROGRAMDATA")
        return Path(base) / "AzurPilot" if base else None
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "azurpilot"
    try:
        return Path.home() / ".local" / "state" / "azurpilot"
    except RuntimeError:
        return None


def _repository_identity(root: Path) -> str:
    value = os.path.normcase(str(root.resolve())).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _state_directory(root: Path) -> Path | None:
    base = _state_base()
    if base is None:
        return None
    directory = base / _repository_identity(root)[:24]
    try:
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            return None
    except OSError:
        return None
    return directory


def _coderabbit_block_reason(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    schema = payload.get("schema_version")
    if not isinstance(schema, int) or schema not in _REVIEW_SCHEMA_VERSIONS:
        return None
    if payload.get("active") is True:
        return "WORKFLOW_CONTINUATION_REQUIRED: завершите или восстановите active CodeRabbit operation через azur integrations coderabbit."
    cycle_status = (
        payload.get("cycle_status").casefold()
        if isinstance(payload.get("cycle_status"), str)
        else None
    )
    phase = (
        payload.get("phase").casefold()
        if isinstance(payload.get("phase"), str)
        else None
    )
    provider_state = (
        payload.get("provider_state").casefold()
        if isinstance(payload.get("provider_state"), str)
        else None
    )
    recovery_provider_states = {value.casefold() for value in _RECOVERY_PROVIDER_STATES}
    recovery = payload.get("recovery")
    recovery_status = (
        recovery.get("status").casefold()
        if isinstance(recovery, dict) and isinstance(recovery.get("status"), str)
        else None
    )
    if payload.get("triage_required") is True or cycle_status == "triage_required":
        return "WORKFLOW_CONTINUATION_REQUIRED: выполните обязательный CodeRabbit triage через typed azur workflow."
    if (
        payload.get("recovery_required") is True
        or cycle_status == "recovery_required"
        or (phase == "recovery" and recovery_status != "completed")
        or provider_state in recovery_provider_states
    ):
        return "WORKFLOW_CONTINUATION_REQUIRED: выполните CodeRabbit recovery через typed azur workflow."
    if provider_state in {"running", "starting"} or phase == "provider":
        return "WORKFLOW_CONTINUATION_REQUIRED: завершите или восстановите active CodeRabbit operation через azur integrations coderabbit."
    findings_count = payload.get("findings_count")
    if (
        phase == "triage"
        and isinstance(findings_count, int)
        and not isinstance(findings_count, bool)
        and findings_count > 0
    ):
        return "WORKFLOW_CONTINUATION_REQUIRED: выполните обязательный CodeRabbit triage через typed azur workflow."
    if (
        payload.get("triage_complete") is False
        and isinstance(findings_count, int)
        and not isinstance(findings_count, bool)
        and findings_count > 0
    ):
        return "WORKFLOW_CONTINUATION_REQUIRED: выполните обязательный CodeRabbit triage через typed azur workflow."
    return None


def _delivery_block_reason(state_directory: Path, root_identity: str) -> str | None:
    transactions = state_directory / "transactions"
    try:
        if transactions.is_symlink() or not transactions.is_dir():
            return None
        entries = sorted(
            (
                entry
                for entry in transactions.iterdir()
                if entry.name.startswith("delivery-")
            ),
            key=lambda entry: entry.name,
        )
    except OSError:
        return None
    for entry in entries[:_MAX_TRANSACTION_DIRECTORIES]:
        if entry.is_symlink() or not entry.is_dir():
            continue
        payload = _read_json_file(entry / "state.json")
        if payload is None:
            continue
        if (
            payload.get("operation_id") == entry.name
            and payload.get("repository_root_identity") == root_identity
            and payload.get("phase") in _DELIVERY_RECOVERY_PHASES
        ):
            return "WORKFLOW_CONTINUATION_REQUIRED: проверьте delivery через azur delivery status/recover."
    return None


def _stop_result(event: dict[str, Any]) -> dict[str, str]:
    if event.get("stop_hook_active") is True:
        return {}
    root = _repository_root(event.get("cwd"))
    if root is None:
        return {}
    state_directory = _state_directory(root)
    if state_directory is None:
        return {}
    review_reason = _coderabbit_block_reason(
        _read_json_file(state_directory / _REVIEW_STATE_NAME)
    )
    if review_reason:
        return {"decision": "block", "reason": review_reason}
    delivery_reason = _delivery_block_reason(
        state_directory, _repository_identity(root)
    )
    if delivery_reason:
        return {"decision": "block", "reason": delivery_reason}
    return {}


def process_event(event: object) -> dict[str, Any] | None:
    """Обработать только wire-поля текущего hook event без transcript parsing."""

    if not _PROJECT_IMPORTS_AVAILABLE:
        return None
    if not isinstance(event, dict):
        return None
    event_name = event.get("hook_event_name")
    if event_name == "PreToolUse":
        if event.get("tool_name") != "Bash":
            return None
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            return None
        command = tool_input.get("command")
        reason = classify_command(command) if isinstance(command, str) else None
        if reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        return None
    if event_name == "Stop":
        return _stop_result(event)
    return None


def _read_event() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def main() -> None:
    if not _PROJECT_IMPORTS_AVAILABLE:
        return
    try:
        event = _read_event()
        result = process_event(event)
        if result is not None:
            sys.stdout.write(
                json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            )
    except Exception:  # noqa: BLE001 - hook не должен показывать traceback в Codex UI.
        return


if __name__ == "__main__":
    main()
