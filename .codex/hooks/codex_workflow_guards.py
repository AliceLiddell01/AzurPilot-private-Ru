"""Не допускать завершения работы, если отправка изменений в Git прервалась неоднозначно."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_MAX_INPUT_BYTES = 128 * 1024
_MAX_STATE_BYTES = 128 * 1024
_MAX_TRANSACTIONS = 128
_RECOVERY_PHASES = frozenset({"push_in_flight", "unknown"})


def _is_link(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or (callable(is_junction) and is_junction())
    except OSError:
        return True


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if _is_link(path) or not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(_MAX_STATE_BYTES + 1)
        if len(raw) > _MAX_STATE_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _repository_root(cwd: object) -> Path | None:
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        candidate = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for root in (candidate, *candidate.parents):
        hooks = root / ".codex" / "hooks.json"
        if not _is_link(hooks) and hooks.is_file():
            return root
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
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser() / "azurpilot"
    try:
        return Path.home() / ".local" / "state" / "azurpilot"
    except RuntimeError:
        return None


def _repository_identity(root: Path) -> str:
    identity = os.path.normcase(str(root.resolve())).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _state_directory(root: Path) -> Path | None:
    base = _state_base()
    if base is None:
        return None
    directory = base / _repository_identity(root)[:24]
    try:
        if _is_link(directory) or (directory.exists() and not directory.is_dir()):
            return None
    except OSError:
        return None
    return directory


def _unfinished_delivery(state_directory: Path, root_identity: str) -> bool:
    transactions = state_directory / "transactions"
    if _is_link(transactions) or not transactions.is_dir():
        return False
    try:
        entries = [
            item
            for item in transactions.iterdir()
            if item.name.startswith("delivery-")
        ][:_MAX_TRANSACTIONS]
    except OSError:
        return False
    for entry in entries:
        if _is_link(entry) or not entry.is_dir():
            continue
        payload = _read_json(entry / "state.json")
        if (
            payload is not None
            and payload.get("operation_id") == entry.name
            and payload.get("repository_root_identity") == root_identity
            and payload.get("phase") in _RECOVERY_PHASES
        ):
            return True
    return False


def process_event(event: object) -> dict[str, str] | None:
    """Блокировать завершение только при неоднозначном состоянии отправки изменений в Git."""

    if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
        return None
    if event.get("stop_hook_active") is True:
        return {}
    root = _repository_root(event.get("cwd"))
    if root is None:
        return {}
    state_directory = _state_directory(root)
    if state_directory is None or not _unfinished_delivery(
        state_directory,
        _repository_identity(root),
    ):
        return {}
    return {
        "decision": "block",
        "reason": (
            "WORKFLOW_CONTINUATION_REQUIRED: проверьте ambiguous delivery "
            "через azur delivery status/recover."
        ),
    }


def _read_event() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            return None
        event = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def main() -> None:
    try:
        result = process_event(_read_event())
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - hook не должен выдавать traceback в UI.
        return


if __name__ == "__main__":
    main()
