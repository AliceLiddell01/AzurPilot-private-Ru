"""Не допускать завершения работы, если отправка изменений в Git прервалась неоднозначно."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_MAX_INPUT_BYTES = 128 * 1024
_MAX_STATE_BYTES = 4096
_OPERATIONS = frozenset({"push", "pr-create", "pr-edit", "pr-ready", "pr-draft", "pr-merge"})


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


def _repository_identity(root: Path) -> str:
    identity = os.path.normcase(str(root.resolve())).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _unfinished_publication(root: Path) -> bool:
    directory = root / ".codex" / "local"
    if _is_link(root / ".codex") or _is_link(directory):
        return False
    payload = _read_json(directory / "git-publication.json")
    if payload is None or set(payload) != {
        "workflow", "repository_root_identity", "operation", "head_sha"
    }:
        return False
    head = payload.get("head_sha")
    return (
        payload.get("workflow") == "azurpilot-git-workflow"
        and payload.get("repository_root_identity") == _repository_identity(root)
        and isinstance(payload.get("operation"), str)
        and payload["operation"] in _OPERATIONS
        and isinstance(head, str)
        and len(head) in {40, 64}
        and all(char in "0123456789abcdef" for char in head)
    )


def process_event(event: object) -> dict[str, str] | None:
    """Блокировать завершение только при неоднозначном состоянии отправки изменений в Git."""

    if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
        return None
    if event.get("stop_hook_active") is True:
        return {}
    root = _repository_root(event.get("cwd"))
    if root is None:
        return {}
    if not _unfinished_publication(root):
        return {}
    return {
        "decision": "block",
        "reason": (
            "WORKFLOW_CONTINUATION_REQUIRED: восстановите фактическое состояние удалённого репозитория "
            "по azurpilot-git-workflow и подтвердите постусловие публикации."
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
    except Exception:  # noqa: BLE001 - hook не должен выдавать traceback в UI.
        result = None
    sys.stdout.write(
        json.dumps(result if result is not None else {}, ensure_ascii=False, separators=(",", ":"))
    )


if __name__ == "__main__":
    main()
