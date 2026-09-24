from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

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
