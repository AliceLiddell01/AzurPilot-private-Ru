from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

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
        "schema_version": 1,
        "request_id": "request-1",
        "idempotency_key": "key-1",
        "operation": "start_profile",
        "profile": "ap",
        "session_id": None,
        "expected_owner": expected.as_dict(),
        "created_at": "2026-09-04T00:00:00+00:00",
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
    ) -> RuntimeControlResult:
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        try:
            import time

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
    finally:
        server.close()

    assert len(results) == 2
    assert all(result.ok for result in results)
    assert maximum == 1


def test_runtime_control_rejects_nonfinite_owner_identity() -> None:
    with pytest.raises(RuntimeControlError) as error:
        RuntimeOwnerIdentity.from_value({"pid": 1, "created_at": float("nan")})
    assert error.value.code == "RUNTIME_OWNER_INVALID"


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
