from __future__ import annotations

import logging

import pytest

from module.application import runtime_log_projection as projection
from module.application.runtime_log_projection import (
    RuntimeLogEvent,
    RuntimeLogProjectionHandler,
    format_runtime_log_timestamp,
    read_runtime_log_events,
    read_runtime_log_tail,
)


def test_runtime_log_timestamp_format_matches_legacy_text_shape():
    assert format_runtime_log_timestamp(
        "2026-09-25T10:00:00.123+07:00"
    ) == "2026-09-25 10:00:00.123"
    assert format_runtime_log_timestamp(
        "2026-09-25 10:00:00.123"
    ) == "2026-09-25 10:00:00.123"
    assert format_runtime_log_timestamp("неизвестно") == "неизвестно"


def test_runtime_log_projection_is_bounded_and_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(projection, "_MAX_FILE_BYTES", 240)
    handler = RuntimeLogProjectionHandler("alpha", repository_root=tmp_path)
    logger = logging.getLogger("tests.runtime.log_projection")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for index in range(8):
            logger.info(
                "Запись %s credential=secret-value path=C:\\Secrets\\value.txt", index
            )
    finally:
        logger.removeHandler(handler)
        handler.close()

    lines = read_runtime_log_tail("alpha", 20, repository_root=tmp_path)
    text = "".join(lines)

    assert "Запись 7" in text
    assert "secret-value" not in text
    assert "C:\\Secrets\\value.txt" not in text
    assert (
        tmp_path / "config" / "state" / "bot-runtime" / "logs" / "alpha.log"
    ).stat().st_size <= 240


def test_runtime_log_projection_skips_blank_info_records(tmp_path):
    handler = RuntimeLogProjectionHandler("alpha", repository_root=tmp_path)
    logger = logging.getLogger("tests.runtime.log_projection.blank")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("")
        logger.info("   ")
        logger.info("Полезная строка")
    finally:
        logger.removeHandler(handler)
        handler.close()

    events = read_runtime_log_events("alpha", repository_root=tmp_path)

    assert [event.message for event in events] == ["Полезная строка"]


def test_runtime_log_projection_keeps_rich_and_exception_metadata(tmp_path):
    handler = RuntimeLogProjectionHandler("alpha", repository_root=tmp_path)
    logger = logging.getLogger("tests.runtime.log_projection.events")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "[bold]Секция[/bold]",
            extra={
                "markup": True,
                "azurpilot_log_kind": "section",
                "azurpilot_section_level": 3,
                "azurpilot_section_title": "Секция",
            },
        )
        try:
            raise ValueError("ошибка среды выполнения")
        except ValueError:
            logger.exception("Не удалось выполнить операцию")
    finally:
        logger.removeHandler(handler)
        handler.close()

    events = read_runtime_log_events("alpha", repository_root=tmp_path)
    assert events[0].kind == "section"
    assert events[0].section_level == 3
    assert events[0].message == "<<< Секция >>>"
    assert events[1].traceback is not None
    assert "Traceback (most recent call last):\n" in events[1].traceback
    assert "\\n" not in events[1].traceback
    assert "ValueError: ошибка среды выполнения" in events[1].traceback

    lines = read_runtime_log_tail("alpha", repository_root=tmp_path)
    text = "".join(lines)
    assert "<<< Секция >>>" in text
    assert "[bold]" not in text
    assert '"section_level"' not in text
    assert "ValueError: ошибка среды выполнения" in text


def test_runtime_log_tail_preserves_physical_lines_and_line_limit(tmp_path):
    handler = RuntimeLogProjectionHandler("alpha", repository_root=tmp_path)
    logger = logging.getLogger("tests.runtime.log_projection.physical-lines")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("До исключения")
        try:
            raise ValueError("ошибка среды выполнения")
        except ValueError:
            logger.exception("Не удалось выполнить операцию")
    finally:
        logger.removeHandler(handler)
        handler.close()

    lines = read_runtime_log_tail("alpha", 100, repository_root=tmp_path)

    assert any("Не удалось выполнить операцию" in line for line in lines)
    assert any("Traceback (most recent call last)" in line for line in lines)
    assert any("ValueError: ошибка среды выполнения" in line for line in lines)
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in lines)
    assert read_runtime_log_tail("alpha", 2, repository_root=tmp_path) == lines[-2:]


def test_logger_hr_levels_project_once_as_structured_events(tmp_path):
    import module.logger as logger_module

    handler = RuntimeLogProjectionHandler("alpha", repository_root=tmp_path)
    logger = logger_module.logger
    handlers_before = logger.handlers[:]
    level_before = logger.level
    try:
        logger.handlers[:] = [handler]
        logger.setLevel(logging.DEBUG)
        for level in range(4):
            logger_module.hr(f"Раздел {level}", level=level)
    finally:
        logger.handlers[:] = handlers_before
        logger.setLevel(level_before)
        handler.close()

    events = read_runtime_log_events("alpha", repository_root=tmp_path)
    lines = read_runtime_log_tail("alpha", repository_root=tmp_path)

    assert len(events) == 4
    assert len(lines) == 4
    assert all(isinstance(line, str) for line in lines)
    assert [event.kind for event in events] == ["section"] * 4
    assert [event.section_level for event in events] == [0, 1, 2, 3]
    assert [event.message for event in events] == [
        "РАЗДЕЛ 0",
        "РАЗДЕЛ 1",
        "РАЗДЕЛ 2",
        "<<< РАЗДЕЛ 3 >>>",
    ]
    assert all("[bold]" not in event.message for event in events)
    assert "INFO │ <<< РАЗДЕЛ 3 >>>" in lines[3]
    assert "[bold]" not in "".join(lines)


def test_markup_removal_does_not_split_secret_sanitization(tmp_path):
    handler = RuntimeLogProjectionHandler("alpha", repository_root=tmp_path)
    logger = logging.getLogger("tests.runtime.log_projection.markup-redaction")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "pass[bold]word=markup-secret[/bold]",
            extra={"markup": True},
        )
    finally:
        logger.removeHandler(handler)
        handler.close()

    event = read_runtime_log_events("alpha", repository_root=tmp_path)[0]

    assert event.message == "password=***"
    assert "markup-secret" not in event.message


def test_runtime_log_events_are_bounded_after_sanitizing(tmp_path, monkeypatch):
    monkeypatch.setattr(projection, "_MAX_RECORD_CHARS", 256)
    handler = RuntimeLogProjectionHandler("alpha", repository_root=tmp_path)
    logger = logging.getLogger("tests.runtime.log_projection.bounded-events")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("credential=raw-secret %s", "д" * 2000)
    finally:
        logger.removeHandler(handler)
        handler.close()

    log_path = tmp_path / "config" / "state" / "bot-runtime" / "logs" / "alpha.log"
    raw_event = log_path.read_text(encoding="utf-8").rstrip("\n")
    events = read_runtime_log_events("alpha", repository_root=tmp_path)

    assert len(raw_event) <= 256
    assert len(events) == 1
    assert "raw-secret" not in events[0].message
    assert len(events[0].message) < 256


def test_runtime_log_projection_reads_legacy_plain_lines(tmp_path):
    log_path = tmp_path / "config" / "state" / "bot-runtime" / "logs" / "alpha.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "2026-09-25 10:00:00.000 │ INFO │ старая строка\n"
        "2026-09-25 10:00:01.000 │ INFO │ [bold]<<< СТАРЫЙ РАЗДЕЛ >>>[/bold]\n",
        encoding="utf-8",
    )

    events = read_runtime_log_events("alpha", repository_root=tmp_path)

    assert events[0] == RuntimeLogEvent(
        timestamp="2026-09-25 10:00:00.000",
        level=logging.INFO,
        level_name="INFO",
        message="старая строка",
    )
    assert events[1].message == "<<< СТАРЫЙ РАЗДЕЛ >>>"
    assert "[bold]" not in events[1].message
    assert "[/bold]" not in events[1].message
    assert read_runtime_log_tail("alpha", repository_root=tmp_path)[-1] == (
        "2026-09-25 10:00:01.000 │ INFO │ <<< СТАРЫЙ РАЗДЕЛ >>>\n"
    )


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

    monkeypatch.setattr(
        runtime_process_manager, "configure_runtime_logging", lambda **_: None
    )
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
    monkeypatch.setattr(
        runtime_process_manager, "configure_runtime_logging", lambda **_: None
    )
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
        raise RuntimeError("Не удалось выполнить задачу рабочего процесса")

    monkeypatch.setattr(
        runtime_process_manager, "configure_runtime_logging", lambda **_: None
    )
    monkeypatch.setattr(
        projection, "RuntimeLogProjectionHandler", lambda _profile: handler
    )
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

    with pytest.raises(
        RuntimeError, match="Не удалось выполнить задачу рабочего процесса"
    ):
        runtime_process_manager.BotRuntimeWorkerManager._run_process_body(
            "alpha", "alas"
        )

    assert added_handlers == [handler]
    assert removed_handlers == [handler]
    assert handler.closed is True
