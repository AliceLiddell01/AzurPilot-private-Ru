from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from module.application import bot_runtime_owner as owner_module
from module.application.bot_runtime_owner import BotRuntimeOwner
from module.application.runtime_control import (
    RUNTIME_CONTROL_PROFILE,
    RuntimeControlOperation,
    RuntimeOwnerIdentity,
)


def _prepare_owner(owner: BotRuntimeOwner) -> RuntimeOwnerIdentity:
    identity = RuntimeOwnerIdentity(pid=1234, created_at=1234.5)
    owner.owner_identity = lambda: identity
    owner.owner_matches = lambda _identity: True
    owner._development_profile = lambda: None
    return identity


def _request_fields() -> dict[str, object]:
    return {
        "request_id": "request-1",
        "idempotency_key": "key-1",
        "session_id": None,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
    }


def test_start_rechecks_shutdown_after_acquiring_runtime_lease(tmp_path, monkeypatch):
    owner = BotRuntimeOwner(tmp_path)
    _prepare_owner(owner)
    owner._profiles = lambda: ("alpha",)
    owner._start = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("profile start must be rejected under lease")
    )

    @contextmanager
    def shutdown_wins_lease(_root, *, timeout):
        del timeout
        owner._shutdown_requested.set()
        yield

    monkeypatch.setattr(owner_module, "game_runtime_lease", shutdown_wins_lease)

    result = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "alpha",
        **_request_fields(),
    )

    assert result.ok is False
    assert result.code == "RUNTIME_OWNER_SHUTTING_DOWN"


def test_successful_stop_runtime_marks_shutdown_before_releasing_lease(
    tmp_path, monkeypatch
):
    owner = BotRuntimeOwner(tmp_path)
    _prepare_owner(owner)
    owner._live_profiles = lambda: []
    monkeypatch.setattr(
        owner_module,
        "get_workers",
        lambda _pid, *, repository_root=None: {},
    )

    @contextmanager
    def assert_shutdown_before_unlock(_root, *, timeout):
        del timeout
        try:
            yield
        finally:
            assert owner._shutdown_requested.is_set()

    monkeypatch.setattr(owner_module, "game_runtime_lease", assert_shutdown_before_unlock)

    result = owner.execute(
        RuntimeControlOperation.STOP_RUNTIME,
        RUNTIME_CONTROL_PROFILE,
        **_request_fields(),
    )

    assert result.ok is True
    assert result.code == "RUNTIME_STOPPED"


def test_owner_waits_for_stop_result_write_before_shutdown(tmp_path, monkeypatch):
    owner = BotRuntimeOwner(tmp_path)
    owner._shutdown_requested.set()
    returned = threading.Event()
    wait_entered = threading.Event()
    shutdown_ready_wait = owner._shutdown_ready.wait

    def wait_for_shutdown_ready(timeout=None):
        wait_entered.set()
        return shutdown_ready_wait(timeout)

    monkeypatch.setattr(owner._shutdown_ready, "wait", wait_for_shutdown_ready)
    waiter = threading.Thread(
        target=lambda: (owner.wait_for_shutdown(), returned.set()),
        daemon=True,
    )
    waiter.start()

    assert wait_entered.wait(timeout=1)
    assert not returned.is_set()

    owner._after_result_written(
        SimpleNamespace(
            operation=RuntimeControlOperation.STOP_RUNTIME,
            ok=False,
            code="RUNTIME_CONTROL_EXPIRED",
        )
    )

    assert returned.wait(timeout=1)
    waiter.join(timeout=1)


def test_configured_profile_start_is_queued_after_result_write(tmp_path, monkeypatch):
    owner = BotRuntimeOwner(tmp_path)
    started = threading.Event()
    monkeypatch.setattr(owner, "start_configured_profiles", started.set)

    owner._after_result_written(
        SimpleNamespace(
            operation=RuntimeControlOperation.START_CONFIGURED_PROFILES,
            ok=True,
        )
    )

    assert started.wait(timeout=1)


def test_background_profile_start_logs_exception_and_can_be_queued_again(
    tmp_path, monkeypatch
):
    owner = BotRuntimeOwner(tmp_path)
    failure_logged = threading.Event()
    retry_started = threading.Event()
    messages: list[str] = []

    def fail_start() -> None:
        raise RuntimeError("configured profiles unavailable")

    def log_failure(message: str) -> None:
        messages.append(message)
        failure_logged.set()

    monkeypatch.setattr(owner, "start_configured_profiles", fail_start)
    monkeypatch.setattr(owner_module.logger, "exception", log_failure)

    owner._queue_configured_profile_start()
    failed_thread = owner._autostart_thread
    assert failure_logged.wait(timeout=2)
    assert failed_thread is not None
    failed_thread.join(timeout=1)
    assert not failed_thread.is_alive()
    assert messages == ["Не удалось выполнить фоновый запуск профилей Bot Runtime"]

    monkeypatch.setattr(owner, "start_configured_profiles", retry_started.set)
    owner._queue_configured_profile_start()
    assert retry_started.wait(timeout=2)
    assert owner._autostart_thread is not failed_thread


@pytest.mark.parametrize("shutdown_timing", ("before", "after"))
def test_configured_profile_start_preserves_marker_when_shutdown_interrupts(
    tmp_path, monkeypatch, shutdown_timing
):
    from deploy import config as deploy_config
    from module.config import profile as profile_config

    owner = BotRuntimeOwner(tmp_path)
    owner._profiles = lambda: ("alpha",)
    marker = tmp_path / "config" / "reloadalas"
    marker.parent.mkdir(parents=True)
    marker.write_text("alpha\n", encoding="utf-8")
    monkeypatch.setattr(
        deploy_config,
        "DeployConfig",
        lambda: SimpleNamespace(Run=None),
    )
    monkeypatch.setattr(
        profile_config,
        "profile_identity_from_name",
        lambda name: SimpleNamespace(mod_name="alas", name=name),
    )
    calls = []

    if shutdown_timing == "before":
        owner._shutdown_requested.set()
    else:
        def request_shutdown(*_args, **_kwargs):
            calls.append(True)
            owner._shutdown_requested.set()
            return SimpleNamespace(ok=True, code="RUNTIME_STARTED")

        owner.execute = request_shutdown

    owner.start_configured_profiles()

    assert marker.read_text(encoding="utf-8") == "alpha\n"
    assert calls == ([] if shutdown_timing == "before" else [True])


def test_close_does_not_release_owner_while_workers_remain(tmp_path, monkeypatch):
    owner = BotRuntimeOwner(tmp_path)
    cleared = []
    monkeypatch.setattr(
        owner_module,
        "get_workers",
        lambda _pid, *, repository_root=None: {"ap": {"pid": 321, "created_at": 1.0}},
    )
    monkeypatch.setattr(
        owner_module,
        "clear_owner",
        lambda *_args, **_kwargs: cleared.append(True),
    )

    with pytest.raises(RuntimeError, match="живых worker"):
        owner.close()

    assert cleared == []


def test_close_releases_owner_after_all_workers_are_stopped(tmp_path, monkeypatch):
    owner = BotRuntimeOwner(tmp_path)
    cleared = []
    monkeypatch.setattr(
        owner_module,
        "get_workers",
        lambda _pid, *, repository_root=None: {},
    )
    monkeypatch.setattr(
        owner_module,
        "clear_owner",
        lambda pid, *, repository_root: cleared.append((pid, repository_root)),
    )

    owner.close()

    assert cleared == [(os.getpid(), tmp_path.resolve())]
