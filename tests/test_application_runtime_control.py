from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from module.application import runtime_control
from module.application.runtime_control import (
    RuntimeControlError,
    RuntimeControlOperation,
    RuntimeControlResult,
    RuntimeOwnerIdentity,
    SharedWebUIBootstrapper,
    WebUIControlClient,
    WebUIControlServer,
)


def test_control_plane_executes_owner_operation_once_and_is_idempotent(tmp_path: Path) -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    calls: list[tuple[RuntimeControlOperation, str]] = []

    def executor(
        operation: RuntimeControlOperation,
        profile: str,
        *,
        request_id: str,
        idempotency_key: str,
        session_id: str | None,
        expires_at: str,
    ) -> RuntimeControlResult:
        calls.append((operation, profile))
        return RuntimeControlResult(
            ok=True,
            code="RUNTIME_STARTED",
            message="Профиль запущен",
            operation=operation,
            profile=profile,
            request_id=request_id,
            idempotency_key=idempotency_key,
            details={"session_id": session_id},
            owner=owner,
        )

    server = WebUIControlServer(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        executor=executor,
        poll_interval=0.005,
    )
    client = WebUIControlClient(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        timeout=1.0,
        poll_interval=0.005,
    )
    server.start()
    try:
        first = client.call(
            RuntimeControlOperation.START_PROFILE,
            "ap",
            session_id="session-1",
            idempotency_key="operation-1",
        )
        second = client.call(
            RuntimeControlOperation.START_PROFILE,
            "ap",
            session_id="session-1",
            idempotency_key="operation-1",
        )
    finally:
        server.close()

    assert first.ok is True
    assert second.as_dict() == first.as_dict()
    assert calls == [(RuntimeControlOperation.START_PROFILE, "ap")]
    assert not list((tmp_path / "config" / "state" / "webui-control" / "requests").glob("*.json"))


def test_control_plane_rejects_changed_owner_and_unsafe_error_key(tmp_path: Path) -> None:
    expected = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    current = RuntimeOwnerIdentity(pid=4322, created_at=1234.5)
    server = WebUIControlServer(
        tmp_path,
        owner_reader=lambda: current.as_dict(),
        owner_matches=lambda candidate: candidate == current,
        executor=lambda *args, **kwargs: pytest.fail("executor не должен быть вызван"),
    )
    request = {
        "schema_version": 2,
        "request_id": "request-1",
        "idempotency_key": "key-1",
        "operation": "start_profile",
        "profile": "ap",
        "session_id": None,
        "expected_owner": expected.as_dict(),
        "created_at": "2026-09-04T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    requests = tmp_path / "config" / "state" / "webui-control" / "requests"
    results = tmp_path / "config" / "state" / "webui-control" / "results"
    requests.mkdir(parents=True)
    results.mkdir(parents=True)
    (requests / "key-1.json").write_text(json.dumps(request), encoding="utf-8")

    assert server.serve_once() == 1
    result = RuntimeControlResult.from_dict(json.loads((results / "key-1.json").read_text(encoding="utf-8")))
    assert result.code == "RUNTIME_OWNER_CHANGED"

    malicious = dict(request)
    malicious["request_id"] = "request-2"
    malicious["idempotency_key"] = "../escape"
    (requests / "malicious.json").write_text(json.dumps(malicious), encoding="utf-8")
    assert server.serve_once() == 0
    assert not (tmp_path / "config" / "state" / "webui-control" / "escape.json").exists()
    assert not (requests / "malicious.json").exists()


def test_control_plane_serializes_concurrent_owner_operations(tmp_path: Path) -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    active = 0
    maximum = 0
    guard = threading.Lock()

    def executor(
        operation: RuntimeControlOperation,
        profile: str,
        *,
        request_id: str,
        idempotency_key: str,
        session_id: str | None,
        expires_at: str,
    ) -> RuntimeControlResult:
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.03)
            return RuntimeControlResult(
                True,
                "RUNTIME_STARTED",
                "Профиль запущен",
                operation,
                profile,
                request_id,
                idempotency_key,
                owner=owner,
            )
        finally:
            with guard:
                active -= 1

    server = WebUIControlServer(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        executor=executor,
        poll_interval=0.005,
    )
    client = WebUIControlClient(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        timeout=2.0,
        poll_interval=0.005,
    )
    server.start()
    results: list[RuntimeControlResult] = []
    try:
        threads = [
            threading.Thread(
                target=lambda key=key: results.append(
                    client.call(RuntimeControlOperation.START_PROFILE, "ap", idempotency_key=key)
                )
            )
            for key in ("operation-1", "operation-2")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        assert all(not thread.is_alive() for thread in threads)
    finally:
        server.close()

    assert len(results) == 2
    assert all(result.ok for result in results)
    assert maximum == 1


def test_runtime_control_rejects_nonfinite_owner_identity() -> None:
    with pytest.raises(RuntimeControlError) as error:
        RuntimeOwnerIdentity.from_value({"pid": 1, "created_at": float("nan")})
    assert error.value.code == "RUNTIME_OWNER_INVALID"


def test_control_client_accepts_ownerless_failure_result() -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    result = RuntimeControlResult(
        False,
        "RUNTIME_OWNER_UNAVAILABLE",
        "Общий WebUI owner завершил работу",
        RuntimeControlOperation.START_PROFILE,
        "ap",
        "request-1",
        "key-1",
        owner=None,
    )

    WebUIControlClient._validate_result(
        result,
        RuntimeControlOperation.START_PROFILE,
        "ap",
        "key-1",
        owner=owner,
    )


def test_control_plane_wraps_permission_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(_path: Path) -> bytes:
        raise PermissionError("синтетическая ошибка доступа")

    monkeypatch.setattr(Path, "read_bytes", deny)
    monkeypatch.setattr(runtime_control.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeControlError) as error:
        runtime_control._read_bounded(tmp_path / "request.json", 1024)

    assert error.value.code == "RUNTIME_CONTROL_READ_FAILED"


def test_control_client_wraps_plane_lock_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)

    class BlockedLock:
        def __enter__(self) -> None:
            raise TimeoutError("synthetic control plane lock timeout")

        def __exit__(self, *_args: object) -> bool:
            return False

    def blocked_lock(*_args: object, **_kwargs: object) -> BlockedLock:
        return BlockedLock()

    monkeypatch.setattr(runtime_control, "application_host_lock", blocked_lock)
    client = WebUIControlClient(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        timeout=1.0,
    )

    with pytest.raises(RuntimeControlError) as error:
        client.call(RuntimeControlOperation.START_PROFILE, "ap")

    assert error.value.code == "RUNTIME_CONTROL_TIMEOUT"


def test_control_plane_requires_positive_timeout_and_rejects_expired_request(tmp_path: Path) -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    with pytest.raises(ValueError):
        WebUIControlClient(
            tmp_path,
            owner_reader=lambda: owner.as_dict(),
            owner_matches=lambda candidate: candidate == owner,
            timeout=0,
        )
    with pytest.raises(ValueError):
        SharedWebUIBootstrapper(
            tmp_path,
            owner_reader=lambda: owner.as_dict(),
            owner_matches=lambda candidate: candidate == owner,
            timeout=0,
        )

    calls = 0

    def executor(*args: object, **kwargs: object) -> RuntimeControlResult:
        nonlocal calls
        calls += 1
        raise AssertionError("просроченный request не должен достигать executor")

    server = WebUIControlServer(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        executor=executor,
    )
    requests = tmp_path / "config" / "state" / "webui-control" / "requests"
    results = tmp_path / "config" / "state" / "webui-control" / "results"
    requests.mkdir(parents=True)
    results.mkdir(parents=True)
    now = datetime.now(UTC)
    request = {
        "schema_version": 2,
        "request_id": "expired-request",
        "idempotency_key": "expired-key",
        "operation": "start_profile",
        "profile": "ap",
        "session_id": None,
        "expected_owner": owner.as_dict(),
        "created_at": (now - timedelta(seconds=2)).isoformat(),
        "expires_at": (now - timedelta(seconds=1)).isoformat(),
    }
    (requests / "expired-key.json").write_text(json.dumps(request), encoding="utf-8")

    assert server.serve_once() == 1
    result = RuntimeControlResult.from_dict(
        json.loads((results / "expired-key.json").read_text(encoding="utf-8"))
    )
    assert result.code == "RUNTIME_CONTROL_EXPIRED"
    assert calls == 0


def test_control_plane_keeps_timed_out_client_request_until_server_rejects_it(
    tmp_path: Path,
) -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    calls = 0

    def executor(*args: object, **kwargs: object) -> RuntimeControlResult:
        nonlocal calls
        calls += 1
        raise AssertionError("просроченный request не должен достигать executor")

    server = WebUIControlServer(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        executor=executor,
    )
    client = WebUIControlClient(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        timeout=0.01,
        poll_interval=0.001,
    )

    with pytest.raises(RuntimeControlError) as error:
        client.call(
            RuntimeControlOperation.START_PROFILE,
            "ap",
            idempotency_key="client-timeout",
        )

    assert error.value.code == "RUNTIME_CONTROL_TIMEOUT"
    requests = tmp_path / "config" / "state" / "webui-control" / "requests"
    results = tmp_path / "config" / "state" / "webui-control" / "results"
    assert (requests / "client-timeout.json").exists()
    assert server.serve_once() == 1
    result = RuntimeControlResult.from_dict(
        json.loads((results / "client-timeout.json").read_text(encoding="utf-8"))
    )
    assert result.code == "RUNTIME_CONTROL_EXPIRED"
    assert calls == 0


def test_control_plane_keeps_expired_request_when_error_result_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    server = WebUIControlServer(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        executor=lambda *args, **kwargs: pytest.fail("executor не должен быть вызван"),
    )
    requests = tmp_path / "config" / "state" / "webui-control" / "requests"
    requests.mkdir(parents=True)
    now = datetime.now(UTC)
    request = {
        "schema_version": 2,
        "request_id": "expired-request",
        "idempotency_key": "expired-key",
        "operation": "start_profile",
        "profile": "ap",
        "session_id": None,
        "expected_owner": owner.as_dict(),
        "created_at": (now - timedelta(seconds=2)).isoformat(),
        "expires_at": (now - timedelta(seconds=1)).isoformat(),
    }
    request_path = requests / "expired-key.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(server, "_write_error_result", lambda *args, **kwargs: False)

    assert server.serve_once() == 0
    assert request_path.exists()


def test_control_plane_retains_recent_results_for_possible_client_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    server = WebUIControlServer(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        executor=lambda *args, **kwargs: pytest.fail("executor не должен быть вызван"),
    )
    results = tmp_path / "config" / "state" / "webui-control" / "results"
    results.mkdir(parents=True)
    old_result = results / "old-result.json"
    recent_result = results / "recent-result.json"
    old_result.write_text("{}", encoding="utf-8")
    recent_result.write_text("{}", encoding="utf-8")
    now = time.time()
    os.utime(old_result, (now - 121, now - 121))
    os.utime(recent_result, (now - 1, now - 1))
    monkeypatch.setattr(runtime_control, "_MAX_RESULT_FILES", 0)

    server._prune_results()

    assert not old_result.exists()
    assert recent_result.exists()


def test_control_plane_expires_requests_before_request_batch_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    calls: list[str] = []

    def executor(
        operation: RuntimeControlOperation,
        profile: str,
        *,
        request_id: str,
        idempotency_key: str,
        session_id: str | None,
        expires_at: str,
    ) -> RuntimeControlResult:
        calls.append(idempotency_key)
        return RuntimeControlResult(
            True,
            "RUNTIME_STARTED",
            "Профиль запущен",
            operation,
            profile,
            request_id,
            idempotency_key,
            owner=owner,
        )

    server = WebUIControlServer(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        executor=executor,
    )
    requests = tmp_path / "config" / "state" / "webui-control" / "requests"
    results = tmp_path / "config" / "state" / "webui-control" / "results"
    requests.mkdir(parents=True)
    results.mkdir(parents=True)
    now = datetime.now(UTC)

    def request(key: str, *, expires_at: datetime) -> dict[str, object]:
        return {
            "schema_version": 2,
            "request_id": f"request-{key}",
            "idempotency_key": key,
            "operation": "start_profile",
            "profile": "ap",
            "session_id": None,
            "expected_owner": owner.as_dict(),
            "created_at": (now - timedelta(seconds=2)).isoformat(),
            "expires_at": expires_at.isoformat(),
        }

    (requests / "a-expired.json").write_text(
        json.dumps(request("expired-key", expires_at=now - timedelta(seconds=1))),
        encoding="utf-8",
    )
    (requests / "z-valid.json").write_text(
        json.dumps(request("valid-key", expires_at=now + timedelta(seconds=60))),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_control, "_MAX_REQUEST_FILES", 1)

    assert server.serve_once() == 2
    expired = RuntimeControlResult.from_dict(
        json.loads((results / "expired-key.json").read_text(encoding="utf-8"))
    )
    valid = RuntimeControlResult.from_dict(
        json.loads((results / "valid-key.json").read_text(encoding="utf-8"))
    )
    assert expired.code == "RUNTIME_CONTROL_EXPIRED"
    assert valid.code == "RUNTIME_STARTED"
    assert calls == ["valid-key"]
    assert not list(requests.glob("*.json"))


def test_control_plane_rejects_executor_result_from_different_owner(tmp_path: Path) -> None:
    owner = RuntimeOwnerIdentity(pid=4321, created_at=1234.5)
    foreign = RuntimeOwnerIdentity(pid=9876, created_at=5432.1)

    def executor(
        operation: RuntimeControlOperation,
        profile: str,
        *,
        request_id: str,
        idempotency_key: str,
        session_id: str | None,
        expires_at: str,
    ) -> RuntimeControlResult:
        return RuntimeControlResult(
            ok=True,
            code="RUNTIME_STARTED",
            message="Профиль запущен",
            operation=operation,
            profile=profile,
            request_id=request_id,
            idempotency_key=idempotency_key,
            owner=foreign,
        )

    server = WebUIControlServer(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        executor=executor,
        poll_interval=0.005,
    )
    client = WebUIControlClient(
        tmp_path,
        owner_reader=lambda: owner.as_dict(),
        owner_matches=lambda candidate: candidate == owner,
        timeout=1.0,
        poll_interval=0.005,
    )
    server.start()
    try:
        result = client.call(
            RuntimeControlOperation.START_PROFILE,
            "ap",
            idempotency_key="foreign-owner-result",
        )
    finally:
        server.close()

    assert result.ok is False
    assert result.code == "RUNTIME_EXECUTION_INVALID"


def test_bootstrap_replaces_stale_owner_only_through_canonical_gui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import module.application.runtime_control as runtime_control

    (tmp_path / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python_executable = tmp_path / "python.exe"
    python_executable.write_bytes(b"synthetic")
    stale = RuntimeOwnerIdentity(pid=100, created_at=200.0)
    fresh = RuntimeOwnerIdentity(pid=101, created_at=201.0)
    current = {"owner": stale.as_dict()}

    class FakeProcess:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            current["owner"] = fresh.as_dict()

        @staticmethod
        def poll() -> None:
            return None

    monkeypatch.setattr(runtime_control.subprocess, "Popen", FakeProcess)
    bootstrapper = SharedWebUIBootstrapper(
        tmp_path,
        owner_reader=lambda: current["owner"],
        owner_matches=lambda owner: owner == fresh,
        python_executable=python_executable,
        timeout=0.2,
        poll_interval=0.001,
    )

    assert bootstrapper.ensure() == fresh


def test_bootstrap_stops_owned_process_when_owner_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python_executable = tmp_path / "python.exe"
    python_executable.write_bytes(b"synthetic")
    owner_reads = iter(
        (None, None, RuntimeControlError("RUNTIME_OWNER_INVALID", "invalid owner"))
    )

    class FakeProcess:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.terminated = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: float) -> None:
            del timeout

    process = FakeProcess()
    monkeypatch.setattr(runtime_control.subprocess, "Popen", lambda *_args, **_kwargs: process)
    bootstrapper = SharedWebUIBootstrapper(
        tmp_path,
        owner_reader=lambda: next(owner_reads),
        owner_matches=lambda _owner: True,
        python_executable=python_executable,
        timeout=0.2,
        poll_interval=0.001,
    )

    with pytest.raises(RuntimeControlError) as error:
        bootstrapper.ensure()

    assert error.value.code == "RUNTIME_OWNER_INVALID"
    assert process.terminated is True


def test_bootstrap_does_not_stop_previous_process_on_new_launch_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python_executable = tmp_path / "python.exe"
    python_executable.write_bytes(b"synthetic")

    class PreviousProcess:
        terminated = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, *, timeout: float) -> None:
            del timeout

    previous = PreviousProcess()

    def fail_launch(*_args: object, **_kwargs: object) -> object:
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(runtime_control.subprocess, "Popen", fail_launch)
    bootstrapper = SharedWebUIBootstrapper(
        tmp_path,
        owner_reader=lambda: None,
        owner_matches=lambda _owner: True,
        python_executable=python_executable,
        timeout=0.2,
        poll_interval=0.001,
    )
    bootstrapper._process = previous  # type: ignore[assignment]

    with pytest.raises(RuntimeControlError) as error:
        bootstrapper.ensure()

    assert error.value.code == "RUNTIME_BOOTSTRAP_FAILED"
    assert previous.terminated is False
