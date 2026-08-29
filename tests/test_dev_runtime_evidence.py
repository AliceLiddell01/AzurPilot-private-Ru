from __future__ import annotations

import base64
import io
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from module.dev_runtime.evidence import (
    EvidenceCorrupt,
    EvidenceError,
    EvidenceStore,
    EvidenceUnavailable,
    capture_git_snapshot,
    validate_session_id,
)
from module.dev_runtime.contracts import DevEnvironment


_TIME = "2026-08-30T00:00:00+00:00"


def _environment(tmp_path: Path) -> DevEnvironment:
    root = (tmp_path / "checkout").resolve()
    (root / "config" / "state").mkdir(parents=True)
    return DevEnvironment(root, root / ".venv" / "Scripts" / "python.exe")


def _store(tmp_path: Path, session_id: str = "session-1") -> EvidenceStore:
    environment = _environment(tmp_path)
    return EvidenceStore.create(
        environment,
        session_id=session_id,
        root_tasks=["RootTask"],
        excluded_tasks=["ExcludedTask"],
        timestamp=_TIME,
    )


def test_evidence_store_reopens_and_keeps_session_scoped_lifecycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_event("session_ready", {"state": "running"}, timestamp=_TIME)
    store.record_task("RootTask", timestamp=_TIME)
    store.record_dependency(
        {
            "task": "DependencyTask",
            "required_by": "RootTask",
            "root": "RootTask",
            "reason": "dependency_override",
            "sequence": 7,
            "timestamp": _TIME,
        }
    )
    store.record_task("RootTask", outcome="returned", timestamp=_TIME)
    store.finalize(stopped_at=_TIME, cleanup_confirmed=True)

    reopened = EvidenceStore.for_session(store.environment, "session-1")
    summary = reopened.summary()
    timeline = reopened.timeline_page(limit=20)

    assert summary["session_id"] == "session-1"
    assert summary["profile"] == "ap"
    assert summary["current_task"] is None
    assert summary["lifecycle"]["duration_seconds"] == 0
    assert summary["cleanup"] == {
        "status": "complete",
        "confirmed": True,
        "preserved": False,
        "updated_at": _TIME,
    }
    assert summary["dependency_summary"]["count"] == 1
    assert [event["type"] for event in timeline["events"]] == [
        "session_ready",
        "task_started",
        "dependency_registered",
        "task_finished",
    ]
    assert [event["sequence"] for event in timeline["events"]] == [1, 2, 3, 4]
    assert all(event["timestamp"].endswith("+00:00") for event in timeline["events"])


def test_evidence_store_rejects_unsafe_identity_and_corrupt_manifest(tmp_path: Path) -> None:
    for value in ("../foreign", "..", "", "session/path", "session\\path"):
        with pytest.raises(ValueError):
            validate_session_id(value)

    store = _store(tmp_path)
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = "must not be accepted"
    store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceCorrupt):
        store.summary()
    with pytest.raises(EvidenceError) as error:
        EvidenceStore.create(
            store.environment,
            session_id="session-1",
            root_tasks=["RootTask"],
            excluded_tasks=[],
            timestamp=_TIME,
        )
    assert error.value.code == "DEV_EVIDENCE_SESSION_EXISTS"


def test_evidence_store_rejects_corrupt_event_and_false_complete_health(tmp_path: Path) -> None:
    store = _store(tmp_path)
    timeline = json.loads(store.timeline_path.read_text(encoding="utf-8"))
    timeline["events"] = [
        {
            "sequence": 1,
            "timestamp": _TIME,
            "type": "session_ready",
            "fields": {},
        },
        {
            "sequence": 1,
            "timestamp": _TIME,
            "type": "runtime_warning",
            "fields": {"reason": "повтор"},
        },
    ]
    store.timeline_path.write_text(json.dumps(timeline), encoding="utf-8")

    with pytest.raises(EvidenceCorrupt):
        store.timeline_page()

    store = _store(tmp_path / "health")
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    manifest["evidence_health"] = {"status": "complete", "reasons": ["git_snapshot_failed"]}
    store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(EvidenceCorrupt):
        store.summary()


def test_evidence_logs_use_boundary_cursor_and_sanitization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.environment.log_file.parent.mkdir(parents=True, exist_ok=True)
    store.environment.log_file.write_bytes(b"old session\n")
    store.capture_log_boundary()
    with store.environment.log_file.open("ab") as handle:
        handle.write(b"new password=secret C:\\private\\token.txt\n")
        handle.write(b"second\n")
        handle.write(b"invalid-utf8-\xff\n")

    first = store.logs_page(limit=1)
    assert first["items"][0]["text"] == "new password=*** [путь скрыт]"
    assert first["more"] is True
    assert "old session" not in json.dumps(first, ensure_ascii=False)

    second = store.logs_page(cursor=first["next_cursor"], limit=2)
    assert [item["text"] for item in second["items"]] == ["second", "invalid-utf8-�"]
    assert second["next_cursor"] is None

    with pytest.raises(EvidenceError) as error:
        store.logs_page(cursor="not-a-valid-cursor", limit=1)
    assert error.value.code == "DEV_EVIDENCE_CURSOR_INVALID"

    malformed_cursor = base64.urlsafe_b64encode(
        json.dumps(
            {
                "session_id": store.session_id,
                "offset": 0,
                "identity": {"device": "не число"},
            }
        ).encode("utf-8")
    ).decode("ascii")
    with pytest.raises(EvidenceError) as error:
        store.logs_page(cursor=malformed_cursor, limit=1)
    assert error.value.code == "DEV_EVIDENCE_CURSOR_INVALID"
    assert store.summary()["evidence_health"]["status"] != "corrupt"

    store.environment.log_file.write_bytes(b"rotated\n")
    with pytest.raises(EvidenceError) as error:
        store.logs_page(limit=1)
    assert error.value.code == "DEV_EVIDENCE_LOG_BOUNDARY_LOST"


def test_evidence_logs_respect_hard_page_byte_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.environment.log_file.parent.mkdir(parents=True, exist_ok=True)
    store.environment.log_file.write_bytes("до сессии\n".encode("utf-8"))
    store.capture_log_boundary()
    with store.environment.log_file.open("ab") as handle:
        for _index in range(32):
            handle.write(("x" * 4096 + "\n").encode("utf-8"))

    page = store.logs_page(limit=200)

    assert len(page["items"]) < 200
    assert sum(len(item["text"].encode("utf-8")) for item in page["items"]) <= 64 * 1024
    assert page["more"] is True
    assert page["next_cursor"]


def test_evidence_create_does_not_overwrite_nonempty_session_directory(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    collision = environment.evidence_root / "session-1"
    collision.mkdir(parents=True)
    marker = collision / "unrelated.txt"
    marker.write_text("сохранить", encoding="utf-8")

    with pytest.raises(EvidenceError) as error:
        EvidenceStore.create(
            environment,
            session_id="session-1",
            root_tasks=["RootTask"],
            excluded_tasks=[],
            timestamp=_TIME,
        )
    assert error.value.code == "DEV_EVIDENCE_SESSION_EXISTS"

    assert marker.read_text(encoding="utf-8") == "сохранить"
    assert not (collision / "manifest.json").exists()


def test_screenshot_metadata_is_bounded_and_self_verified(tmp_path: Path) -> None:
    store = _store(tmp_path)
    image = np.zeros((2, 3, 3), dtype=np.uint8)
    image[:, :, 0] = 255

    screenshot = store.persist_screenshot(image, timestamp=_TIME)

    assert screenshot.result.ok is True
    assert screenshot.mime_type == "image/png"
    assert screenshot.image is not None
    metadata = screenshot.result.details["screenshot"]
    assert metadata["mime"] == "image/png"
    assert metadata["width"] == 3
    assert metadata["height"] == 2
    assert metadata["byte_size"] == len(screenshot.image)
    assert metadata["sha256"]
    assert all(
        "path" not in key.casefold() and "file" not in key.casefold()
        for key in metadata
    )

    screenshot_path = store.screenshot_dir / f"{metadata['screenshot_id']}.png"
    screenshot_path.unlink()
    with pytest.raises(EvidenceCorrupt) as error:
        store.summary()
    assert error.value.code == "DEV_EVIDENCE_CORRUPT"


def test_screenshot_bytes_must_be_png(tmp_path: Path) -> None:
    store = _store(tmp_path)
    from PIL import Image

    Image.init()
    output = io.BytesIO()
    Image.new("RGB", (1, 1)).save(output, format="JPEG")

    screenshot = store.persist_screenshot(output.getvalue(), timestamp=_TIME)

    assert screenshot.result.ok is False
    assert screenshot.result.code == "DEV_SCREENSHOT_INVALID"
    assert store.summary()["screenshots"]["count"] == 0


def test_structured_error_has_safe_relative_frames_and_redacted_message(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        raise ValueError("password=secret C:\\private\\token.txt")
    except ValueError as exc:
        store.record_error(exc, phase="task", task="RootTask", timestamp=_TIME)

    error = store.summary()["last_error"]
    assert error["type"] == "ValueError"
    assert "secret" not in error["message"]
    assert "token.txt" not in error["message"]
    assert error["task"] == "RootTask"
    assert error["frames"]
    assert all("locals" not in frame for frame in error["frames"])
    assert all("C:\\" not in json.dumps(frame) for frame in error["frames"])

    store.record_error(ValueError(), phase="task", task="RootTask", timestamp=_TIME)
    assert store.summary()["last_error"]["message"] == "ValueError"


@dataclass
class _GitResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_git_snapshot_uses_fixed_local_commands_and_ignores_untracked_content(tmp_path: Path) -> None:
    root = (tmp_path / "checkout").resolve()
    root.mkdir()
    calls: list[tuple[str, ...]] = []
    head = "a" * 40

    def runner(command: list[str], **_kwargs: object) -> _GitResult:
        calls.append(tuple(command))
        if command[1] == "rev-parse":
            return _GitResult(0, head + "\n")
        if command[1] == "symbolic-ref":
            return _GitResult(0, "codex/stage4\n")
        return _GitResult(0, " M config/ap.json\n")

    snapshot = capture_git_snapshot(root, runner=runner)

    assert snapshot.available is True
    assert snapshot.head == head
    assert snapshot.branch == "codex/stage4"
    assert snapshot.dirty is True
    assert snapshot.changed_paths == ("config/ap.json",)
    assert calls == [
        ("git", "rev-parse", "--verify", "HEAD"),
        ("git", "symbolic-ref", "--quiet", "--short", "HEAD"),
        ("git", "status", "--porcelain=v1", "--untracked-files=no"),
    ]


def test_git_snapshot_degrades_on_timeout_or_nonzero(tmp_path: Path) -> None:
    root = (tmp_path / "checkout").resolve()
    root.mkdir()

    def timeout_runner(_command: list[str], **_kwargs: object) -> _GitResult:
        raise TimeoutError("истёк срок")

    timeout_snapshot = capture_git_snapshot(root, runner=timeout_runner)
    assert timeout_snapshot.available is False
    assert timeout_snapshot.dirty is None

    def failed_runner(_command: list[str], **_kwargs: object) -> _GitResult:
        return _GitResult(1, "", "ошибка")

    failed_snapshot = capture_git_snapshot(root, runner=failed_runner)
    assert failed_snapshot.available is False
    assert failed_snapshot.reason == "git_snapshot_nonzero"


def test_timeline_sequence_allocation_is_safe_for_concurrent_writers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    errors: list[BaseException] = []

    def append(index: int) -> None:
        try:
            store.append_event("runtime_warning", {"reason": f"проверка-{index}"}, timestamp=_TIME)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    events = store.timeline_page(limit=100)["events"]
    assert [event["sequence"] for event in events] == list(range(1, 13))


def test_dependency_rejects_unknown_reason_without_mutating_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    before_manifest = store.manifest_path.read_bytes()
    before_timeline = store.timeline_path.read_bytes()

    with pytest.raises(EvidenceError) as error:
        store.record_dependency(
            {
                "task": "DependencyTask",
                "required_by": "RootTask",
                "root": "RootTask",
                "reason": "unknown",
                "sequence": 1,
                "timestamp": _TIME,
            }
        )

    assert store.manifest_path.read_bytes() == before_manifest
    assert store.timeline_path.read_bytes() == before_timeline
    assert error.value.code == "DEV_EVIDENCE_DEPENDENCY_INVALID"


def test_timeline_truncation_remains_readable_and_is_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("module.dev_runtime.evidence._MAX_TIMELINE_EVENTS", 2)
    store = _store(tmp_path)
    for index in range(3):
        store.append_event("runtime_warning", {"reason": f"событие-{index}"}, timestamp=_TIME)

    page = store.timeline_page(limit=2)
    assert [event["sequence"] for event in page["events"]] == [2, 3]
    assert page["truncated"] is True
    assert store.summary()["evidence_health"]["status"] == "degraded"


def test_retention_keeps_active_session_and_ignores_foreign_directory(tmp_path: Path, monkeypatch) -> None:
    environment = _environment(tmp_path)
    monkeypatch.setattr("module.dev_runtime.evidence._MAX_RETENTION_SESSIONS", 1)
    active = EvidenceStore.create(
        environment,
        session_id="active",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp=_TIME,
    )
    old = EvidenceStore.create(
        environment,
        session_id="old",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp=_TIME,
    )
    foreign = environment.evidence_root / "foreign"
    foreign.mkdir()
    (foreign / "do-not-touch.txt").write_text("данные", encoding="utf-8")
    old_time = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    os.utime(old.root, (old_time, old_time))
    os.utime(foreign, (old_time, old_time))

    assert EvidenceStore.prune(environment, active_session_id=active.session_id) is True
    assert active.root.exists()
    assert not old.root.exists()
    assert (foreign / "do-not-touch.txt").exists()


def test_hooks_are_noop_without_active_dev_session(monkeypatch) -> None:
    monkeypatch.delenv("AZURPILOT_DEV_SESSION_ID", raising=False)
    from module.dev_runtime import hooks

    hooks.record_task_started("ap", "RootTask")
    hooks.record_task_finished("ap", "RootTask")
    hooks.record_runtime_error("ap", ValueError("не должно записываться"), phase="task")
    hooks.record_dependency_registered(
        "ap",
        caller="RootTask",
        target="DependencyTask",
        timestamp=_TIME,
    )
    hooks.serve_pending_screenshot(np.zeros((1, 1), dtype=np.uint8))


def test_old_session_without_evidence_is_reported_as_unavailable(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    with pytest.raises(EvidenceUnavailable):
        EvidenceStore.for_session(environment, "old-stage3").summary()
