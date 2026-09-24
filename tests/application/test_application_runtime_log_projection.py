from __future__ import annotations

import logging

import pytest

from module.application import runtime_log_projection as projection
from module.application.runtime_log_projection import (
    RuntimeLogProjectionHandler,
    read_runtime_log_tail,
)


def test_runtime_log_projection_is_bounded_and_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(projection, "_MAX_FILE_BYTES", 240)
    handler = RuntimeLogProjectionHandler("alpha", repository_root=tmp_path)
    logger = logging.getLogger("tests.runtime.log_projection")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for index in range(8):
            logger.info("entry %s credential=secret-value path=C:\\Secrets\\value.txt", index)
    finally:
        logger.removeHandler(handler)
        handler.close()

    lines = read_runtime_log_tail("alpha", 20, repository_root=tmp_path)
    text = "".join(lines)

    assert "entry 7" in text
    assert "secret-value" not in text
    assert "C:\\Secrets\\value.txt" not in text
    assert (tmp_path / "config" / "state" / "bot-runtime" / "logs" / "alpha.log").stat().st_size <= 240


def test_runtime_log_projection_rejects_path_like_profile(tmp_path):
    with pytest.raises(ValueError, match="формат"):
        read_runtime_log_tail("../outside", repository_root=tmp_path)


@pytest.mark.parametrize("profile_name", ["Профиль", "profile name", "a" * 129])
def test_runtime_log_projection_accepts_canonical_profile_names(
    tmp_path,
    profile_name: str,
):
    handler = RuntimeLogProjectionHandler(profile_name, repository_root=tmp_path)
    try:
        assert handler.baseFilename == str(
            tmp_path
            / "config"
            / "state"
            / "bot-runtime"
            / "logs"
            / f"{profile_name}.log"
        )
    finally:
        handler.close()


@pytest.mark.parametrize("profile_name", ["profile.json", "template-copy"])
def test_runtime_log_projection_rejects_noncanonical_profile_names(
    tmp_path,
    profile_name: str,
):
    with pytest.raises(ValueError, match="формат"):
        read_runtime_log_tail(profile_name, repository_root=tmp_path)


@pytest.mark.parametrize("error", [OSError("unavailable"), ValueError("invalid path")])
def test_worker_continues_when_log_projection_initialization_fails(
    monkeypatch,
    error: Exception,
):
    from module.application import runtime_process_manager

    called: list[tuple[str, str, object | None]] = []
    added_handlers: list[object] = []
    removed_handlers: list[object] = []

    def fail_projection(_profile: str):
        raise error

    monkeypatch.setattr(runtime_process_manager, "configure_runtime_logging", lambda **_: None)
    monkeypatch.setattr(projection, "RuntimeLogProjectionHandler", fail_projection)
    monkeypatch.setattr(
        runtime_process_manager.logger,
        "addHandler",
        lambda handler: added_handlers.append(handler),
    )
    monkeypatch.setattr(
        runtime_process_manager.logger,
        "removeHandler",
        lambda handler: removed_handlers.append(handler),
    )
    monkeypatch.setattr(
        runtime_process_manager.BotRuntimeWorkerManager,
        "_run_process_task_body",
        lambda profile, func, event: called.append((profile, func, event)),
    )

    runtime_process_manager.BotRuntimeWorkerManager._run_process_body("alpha", "alas")

    assert called == [("alpha", "alas", None)]
    assert added_handlers == []
    assert removed_handlers == []


@pytest.mark.parametrize("profile_name", ["profile.json", "../outside"])
def test_worker_rejects_noncanonical_profile_before_projection_or_task(
    monkeypatch,
    profile_name: str,
):
    from module.application import runtime_process_manager

    constructed_profiles: list[str] = []
    task_profiles: list[str] = []
    monkeypatch.setattr(runtime_process_manager, "configure_runtime_logging", lambda **_: None)
    monkeypatch.setattr(
        projection,
        "RuntimeLogProjectionHandler",
        lambda profile: constructed_profiles.append(profile),
    )
    monkeypatch.setattr(
        runtime_process_manager.BotRuntimeWorkerManager,
        "_run_process_task_body",
        lambda profile, _func, _event: task_profiles.append(profile),
    )

    with pytest.raises(ValueError, match="формат"):
        runtime_process_manager.BotRuntimeWorkerManager._run_process_body(
            profile_name,
            "alas",
        )

    assert constructed_profiles == []
    assert task_profiles == []


def test_worker_removes_and_closes_projection_when_task_body_fails(monkeypatch):
    from module.application import runtime_process_manager

    class TrackingHandler:
        closed = False

        def close(self) -> None:
            self.closed = True

    handler = TrackingHandler()
    added_handlers: list[object] = []
    removed_handlers: list[object] = []

    def fail_task(*_args: object) -> None:
        raise RuntimeError("worker task failed")

    monkeypatch.setattr(runtime_process_manager, "configure_runtime_logging", lambda **_: None)
    monkeypatch.setattr(projection, "RuntimeLogProjectionHandler", lambda _profile: handler)
    monkeypatch.setattr(
        runtime_process_manager.logger,
        "addHandler",
        lambda candidate: added_handlers.append(candidate),
    )
    monkeypatch.setattr(
        runtime_process_manager.logger,
        "removeHandler",
        lambda candidate: removed_handlers.append(candidate),
    )
    monkeypatch.setattr(
        runtime_process_manager.BotRuntimeWorkerManager,
        "_run_process_task_body",
        fail_task,
    )

    with pytest.raises(RuntimeError, match="worker task failed"):
        runtime_process_manager.BotRuntimeWorkerManager._run_process_body("alpha", "alas")

    assert added_handlers == [handler]
    assert removed_handlers == [handler]
    assert handler.closed is True
