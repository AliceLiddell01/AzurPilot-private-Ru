from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from module.dev_runtime import evidence as evidence_module
from module.dev_runtime.bounded_io import BoundedReadTooLarge, read_bounded_bytes
from module.dev_runtime.evidence import (
    EVIDENCE_EVENT_TYPES,
    EvidenceCorrupt,
    EvidenceError,
    EvidenceStore,
    EvidenceUnavailable,
    capture_git_snapshot,
    validate_session_id,
)
from module.dev_runtime.contracts import DevEnvironment
from module.dev_runtime.target import DevTarget


_TIME = "2026-08-30T00:00:00+00:00"


def test_event_registry_is_public_and_single_source(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert "session_ready" in EVIDENCE_EVENT_TYPES
    assert "runtime_error" in EVIDENCE_EVENT_TYPES
    assert not hasattr(evidence_module, "_EVENT_TYPES")
    with pytest.raises(EvidenceError) as error:
        store.append_event("unknown_event", {}, timestamp=_TIME)
    assert error.value.code == "DEV_EVIDENCE_EVENT_INVALID"


def _environment(tmp_path: Path) -> DevEnvironment:
    root = (tmp_path / "checkout").resolve()
    (root / "config" / "state").mkdir(parents=True)
    return DevEnvironment(root, root / ".venv" / "Scripts" / "python.exe", DevTarget("ap"))


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


def test_evidence_logs_do_not_split_oversized_physical_lines_or_cursors(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.environment.log_file.parent.mkdir(parents=True, exist_ok=True)
    store.environment.log_file.write_bytes("до сессии\n".encode("utf-8"))
    store.capture_log_boundary()
    with store.environment.log_file.open("ab") as handle:
        handle.write(
            b"password=" + b"s" * evidence_module._MAX_LOG_LINE_BYTES + b" secret-after-limit\n"
        )
        handle.write("следующая физическая строка\n".encode("utf-8"))

    page = store.logs_page(limit=200)

    assert len(page["items"]) == 2
    assert page["items"][0]["truncated"] is True
    assert page["items"][1]["text"] == "следующая физическая строка"
    assert page["more"] is False
    assert "secret-after-limit" not in json.dumps(page, ensure_ascii=False)

    logs = store._manifest_locked()["logs"]
    assert isinstance(logs, dict)
    identity = evidence_module._FileIdentity.from_value(logs["boundary_identity"])
    assert identity is not None
    bad_cursor = store._cursor(offset=logs["boundary_offset"] + 1, identity=identity)
    with pytest.raises(EvidenceError) as error:
        store.logs_page(cursor=bad_cursor, limit=1)
    assert error.value.code == "DEV_EVIDENCE_CURSOR_INVALID"


def test_evidence_logs_finalize_with_end_boundary_and_fail_closed_after_loss(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.environment.log_file.parent.mkdir(parents=True, exist_ok=True)
    store.environment.log_file.write_bytes("до сессии\n".encode("utf-8"))
    store.capture_log_boundary()
    with store.environment.log_file.open("ab") as handle:
        handle.write("только эта строка\n".encode("utf-8"))
    store.finalize(stopped_at=_TIME, cleanup_confirmed=True)
    with store.environment.log_file.open("ab") as handle:
        handle.write("следующая сессия\n".encode("utf-8"))

    page = store.logs_page(limit=200)

    assert [item["text"] for item in page["items"]] == ["только эта строка"]
    assert page["more"] is False
    assert page["next_cursor"] is None

    store.environment.log_file.write_bytes("обрезано\n".encode("utf-8"))
    with pytest.raises(EvidenceError) as error:
        store.logs_page(limit=1)
    assert error.value.code == "DEV_EVIDENCE_LOG_BOUNDARY_LOST"


def test_evidence_reads_reject_oversized_files_before_buffering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path / "manifest")
    store.manifest_path.write_bytes(b"x" * (evidence_module._MAX_MANIFEST_BYTES + 1))
    with pytest.raises(EvidenceCorrupt) as manifest_error:
        store.summary()
    assert manifest_error.value.code == "DEV_EVIDENCE_TOO_LARGE"

    timeline_store = _store(tmp_path / "timeline")
    timeline_store.timeline_path.write_bytes(b"x" * (evidence_module._MAX_TIMELINE_BYTES + 1))
    with pytest.raises(EvidenceCorrupt) as timeline_error:
        timeline_store.timeline_page()
    assert timeline_error.value.code == "DEV_EVIDENCE_TOO_LARGE"

    screenshot_store = _store(tmp_path / "screenshot")
    monkeypatch.setattr(evidence_module, "_MAX_IMAGE_BYTES", 8)
    screenshot_metadata = {
        "screenshot_id": "shot-1",
        "timestamp": _TIME,
        "mime": "image/png",
        "width": 1,
        "height": 1,
        "byte_size": 8,
        "sha256": "0" * 64,
    }
    screenshot_store.screenshot_dir.mkdir(parents=True, exist_ok=True)
    (screenshot_store.screenshot_dir / "shot-1.json").write_text(
        json.dumps(screenshot_metadata),
        encoding="utf-8",
    )
    (screenshot_store.screenshot_dir / "shot-1.png").write_bytes(b"x" * 9)
    screenshot_manifest = json.loads(screenshot_store.manifest_path.read_text(encoding="utf-8"))
    screenshot_manifest["screenshots"] = {"count": 1, "latest": screenshot_metadata}
    screenshot_store.manifest_path.write_text(
        json.dumps(screenshot_manifest),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceCorrupt) as screenshot_error:
        screenshot_store.summary()
    assert screenshot_error.value.code == "DEV_EVIDENCE_TOO_LARGE"

    active_environment = _environment(tmp_path / "active")
    active_environment.state_file.parent.mkdir(parents=True, exist_ok=True)
    active_environment.state_file.write_bytes(b"x" * (64 * 1024 + 1))
    assert evidence_module._read_active_session(active_environment) is None


def test_bounded_read_requests_only_limit_plus_one_bytes() -> None:
    reads: list[int] = []

    class Reader:
        def __enter__(self) -> "Reader":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            reads.append(size)
            return b"x" * size

    class FakePath:
        def open(self, mode: str) -> Reader:
            assert mode == "rb"
            return Reader()

    with pytest.raises(BoundedReadTooLarge):
        read_bounded_bytes(FakePath(), max_bytes=8)

    assert reads == [9]


def test_evidence_lock_closes_handle_when_initialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    lock_path = environment.evidence_lock_file

    class Handle:
        closed = False

        def close(self) -> None:
            self.closed = True

    handle = Handle()
    original_open = Path.open
    original_stat = Path.stat

    def fake_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if path == lock_path and mode == "a+b":
            return handle
        return original_open(path, mode, *args, **kwargs)

    def fail_stat(path: Path, *args: object, **kwargs: object) -> object:
        if path == lock_path:
            raise OSError("synthetic lock initialization failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    monkeypatch.setattr(Path, "stat", fail_stat)

    with pytest.raises(OSError, match="lock initialization failure"):
        with evidence_module._exclusive_lock(lock_path, environment.repository_root):
            pass

    assert handle.closed is True


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


def test_historical_screenshot_requires_stopped_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    screenshot = store.persist_screenshot(np.zeros((1, 1, 3), dtype=np.uint8), timestamp=_TIME)
    screenshot_id = screenshot.result.details["screenshot"]["screenshot_id"]

    before_stop = store.read_persisted_screenshot(screenshot_id)
    assert before_stop.result.ok is False
    assert before_stop.result.code == "DEV_EVIDENCE_NOT_FINALIZED"

    store.finalize(stopped_at=_TIME, cleanup_confirmed=True)
    after_stop = store.read_persisted_screenshot(screenshot_id)
    assert after_stop.result.ok is True
    assert after_stop.image == screenshot.image


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
            return _GitResult(0, "codex/dev-runtime-review\n")
        return _GitResult(0, " M config/ap.json\n")

    snapshot = capture_git_snapshot(root, runner=runner)

    assert snapshot.available is True
    assert snapshot.head == head
    assert snapshot.branch == "codex/dev-runtime-review"
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


def test_git_snapshot_does_not_inherit_mcp_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = (tmp_path / "checkout").resolve()
    root.mkdir()
    popen_calls: list[dict[str, object]] = []

    class FakeStdout:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self, _size: int) -> bytes:
            payload, self.payload = self.payload, b""
            return payload

    class FakeProcess:
        def __init__(self, payload: bytes) -> None:
            self.stdout = FakeStdout(payload)
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        popen_calls.append(kwargs)
        payload = {
            "rev-parse": b"a" * 40 + b"\n",
            "symbolic-ref": b"codex/dev-runtime-review\n",
            "status": b"",
        }[command[1]]
        return FakeProcess(payload)

    monkeypatch.setattr(evidence_module.subprocess, "Popen", fake_popen)

    snapshot = capture_git_snapshot(root)

    assert snapshot.available is True
    assert len(popen_calls) == 3
    assert all(call["stdin"] is subprocess.DEVNULL for call in popen_calls)


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


def test_dependency_count_survives_timeline_retention(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("module.dev_runtime.evidence._MAX_TIMELINE_EVENTS", 2)
    store = _store(tmp_path)
    for index in range(3):
        store.record_dependency(
            {
                "task": "DependencyTask",
                "required_by": "RootTask",
                "root": "RootTask",
                "reason": "dependency",
                "sequence": index + 1,
                "timestamp": _TIME,
            }
        )

    summary = store.summary()

    assert summary["dependency_summary"]["count"] == 3
    assert summary["dependency_summary"]["last"]["sequence"] == 3
    assert summary["timeline"]["event_count"] == 2
    assert summary["timeline"]["truncated"] is True
    assert summary["evidence_health"]["status"] == "degraded"


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


def test_retention_evicts_oldest_session_when_bytes_exceed_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    newest = EvidenceStore.create(
        environment,
        session_id="newest",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp=_TIME,
    )
    oldest = EvidenceStore.create(
        environment,
        session_id="oldest",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp=_TIME,
    )
    (oldest.root / "large.bin").write_bytes(b"x" * 4096)
    newest_time = datetime(2026, 8, 30, tzinfo=UTC).timestamp()
    oldest_time = datetime(2026, 8, 29, tzinfo=UTC).timestamp()
    os.utime(newest.root, (newest_time, newest_time))
    os.utime(oldest.root, (oldest_time, oldest_time))
    monkeypatch.setattr(
        evidence_module,
        "_MAX_RETENTION_BYTES",
        evidence_module._safe_tree_size(newest.root, environment.repository_root) + 1,
    )

    assert EvidenceStore.prune(environment, now=lambda: datetime(2026, 8, 30, tzinfo=UTC)) is True
    assert newest.root.exists()
    assert not oldest.root.exists()


def test_retention_keeps_active_session_even_when_budget_is_impossible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    active = EvidenceStore.create(
        environment,
        session_id="active-budget",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp=_TIME,
    )
    old = EvidenceStore.create(
        environment,
        session_id="old-budget",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp=_TIME,
    )
    (active.root / "large.bin").write_bytes(b"x" * 4096)
    old_time = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    os.utime(old.root, (old_time, old_time))
    monkeypatch.setattr(evidence_module, "_MAX_RETENTION_BYTES", 1)

    assert EvidenceStore.prune(environment, active_session_id=active.session_id) is False
    assert active.root.exists()
    assert not old.root.exists()


def test_retention_leaves_corrupt_session_untouched_and_reports_failure(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    corrupt = EvidenceStore.create(
        environment,
        session_id="corrupt",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp=_TIME,
    )
    manifest = json.loads(corrupt.manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = "повреждение"
    corrupt.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    old_time = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    os.utime(corrupt.root, (old_time, old_time))

    assert EvidenceStore.prune(environment, now=lambda: datetime(2026, 8, 30, tzinfo=UTC)) is False
    assert corrupt.root.exists()


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
        EvidenceStore.for_session(environment, "old-session-without-evidence").summary()
