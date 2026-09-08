"""Регрессии локального хранилища инцидентов и контракта корреляции приложения."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from alas import AzurLaneAutoScript
from module.config.config import Function
from module.logger import logger
from module.observability import scheduler_task_run
from module.observability.incident import (
    build_incident_metadata,
    create_incident_directory,
    write_incident_log,
    write_incident_metadata,
)
from module.observability.scheduler import get_current_task_name
from module.observability.tracing import TraceCorrelation


@pytest.fixture(autouse=True)
def _diagnostic_context():
    """Изолировать диагностический контекст логгера между тестами."""
    logger.reset_diagnostic_context()
    yield
    logger.reset_diagnostic_context()


def _task(command: str = "Research") -> Function:
    return Function({"Scheduler": {"Command": command}})


def test_incident_metadata_is_bounded_canonical_and_serializable():
    timestamp = datetime(2026, 9, 7, 0, 34, 12, 123456, tzinfo=UTC)
    exception = RuntimeError(
        "password=raw-secret C:\\Users\\operator\\incident.log"
    )

    with scheduler_task_run(
        profile="Профиль с пробелом",
        task=_task(),
        registry=("Research",),
    ):
        metadata = build_incident_metadata(
            profile="Профиль с пробелом",
            exception=exception,
            timestamp=timestamp,
            correlation=TraceCorrelation(
                trace_id="a" * 32,
                span_id="b" * 16,
            ),
        )

    assert metadata.to_dict() == {
        "schema_version": 1,
        "timestamp_utc": "2026-09-07T00:34:12.123Z",
        "profile": "Профиль с пробелом",
        "task": "Research",
        "exception_type": "RuntimeError",
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
    }
    serialized = json.dumps(metadata.to_dict(), ensure_ascii=False)
    assert "raw-secret" not in serialized
    assert "C:\\Users\\operator" not in serialized

    invalid_correlation = build_incident_metadata(
        profile="profile-a",
        exception=ValueError("synthetic"),
        correlation=TraceCorrelation(trace_id="A" * 32, span_id="invalid"),
    )
    assert invalid_correlation.trace_id is None
    assert invalid_correlation.span_id is None
    disabled_correlation = build_incident_metadata(
        profile="profile-a",
        exception=ValueError("synthetic"),
    )
    assert disabled_correlation.trace_id is None
    assert disabled_correlation.span_id is None


def test_incident_directory_is_readable_collision_safe_and_profile_scoped(tmp_path):
    timestamp = datetime(2026, 9, 7, 0, 34, 12, 123456, tzinfo=UTC)

    first, first_time = create_incident_directory(
        tmp_path / "log" / "error",
        profile="Профиль с пробелом",
        exception=RuntimeError("message is never used in the directory name"),
        timestamp=timestamp,
    )
    second, second_time = create_incident_directory(
        tmp_path / "log" / "error",
        profile="Профиль с пробелом",
        exception=RuntimeError("another message"),
        timestamp=timestamp,
    )

    assert first_time == second_time == timestamp
    assert first.parent.name == "Профиль с пробелом"
    assert first.name == "2026-09-07_00-34-12.123_RuntimeError"
    assert second.name == "2026-09-07_00-34-12.123_RuntimeError_001"
    assert first.is_dir()
    assert second.is_dir()
    assert all(character not in first.name for character in '<>:"/\\|?*')
    assert "message" not in first.name


def test_error_retention_keeps_mixed_format_incidents_by_actual_time(tmp_path):
    script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
    names = (
        "1757000000000",
        "1757000100000",
        "2026-09-07_00-34-12.123_RuntimeError",
        "unknown-bundle",
    )
    for name in names:
        (tmp_path / name).mkdir()

    script.keep_last_errlog(str(tmp_path), n=2)

    assert not (tmp_path / "1757000000000").exists()
    assert (tmp_path / "1757000100000").is_dir()
    assert (tmp_path / "2026-09-07_00-34-12.123_RuntimeError").is_dir()
    assert (tmp_path / "unknown-bundle").is_dir()


def test_error_retention_orders_legacy_epoch_directories_naturally(tmp_path):
    script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
    for name in ("2", "10", "100"):
        (tmp_path / name).mkdir()

    script.keep_last_errlog(str(tmp_path), n=2)

    assert not (tmp_path / "2").exists()
    assert (tmp_path / "10").is_dir()
    assert (tmp_path / "100").is_dir()


def test_error_retention_preserves_current_timestamp_collision_order(tmp_path):
    script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
    names = (
        "2026-09-07_00-34-12.123_RuntimeError",
        "2026-09-07_00-34-12.123_RuntimeError_001",
        "2026-09-07_00-34-12.123_RuntimeError_1000",
    )
    for name in names:
        (tmp_path / name).mkdir()

    script.keep_last_errlog(str(tmp_path), n=2)

    assert not (tmp_path / names[0]).exists()
    assert (tmp_path / names[1]).is_dir()
    assert (tmp_path / names[2]).is_dir()


def test_error_retention_does_nothing_for_non_positive_limit(tmp_path):
    script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
    names = ("1757000000000", "2026-09-07_00-34-12.123_RuntimeError", "unknown")
    for name in names:
        (tmp_path / name).mkdir()

    script.keep_last_errlog(str(tmp_path), n=0)
    script.keep_last_errlog(str(tmp_path), n=-1)

    assert all((tmp_path / name).is_dir() for name in names)


def test_incident_metadata_write_is_atomic_and_contains_no_exception_payload(tmp_path):
    folder = tmp_path / "incident"
    folder.mkdir()
    metadata = build_incident_metadata(
        profile="profile-a",
        exception=RuntimeError("password=raw-secret /var/lib/azurpilot/secret.log"),
    )

    target = write_incident_metadata(folder, metadata)

    assert target == folder / "incident.json"
    assert json.loads(target.read_text(encoding="utf-8")) == metadata.to_dict()
    assert not list(folder.glob(".incident-*.tmp"))
    contents = target.read_text(encoding="utf-8")
    assert "raw-secret" not in contents
    assert "/var/lib/azurpilot" not in contents


def test_incident_log_is_bounded_sanitized_and_atomic(tmp_path):
    folder = tmp_path / "incident"
    folder.mkdir()

    target = write_incident_log(
        folder,
        ("password=raw-secret", "line-2", "line-3"),
        max_bytes=32,
        max_lines=2,
    )

    assert target == folder / "log.txt"
    contents = target.read_text(encoding="utf-8")
    assert "raw-secret" not in contents
    assert "line-2" in contents
    assert "line-3" not in contents
    assert len(target.read_bytes()) <= 32
    assert not list(folder.glob(".incident-log-*.tmp"))


def test_incident_log_normalizes_embedded_line_breaks_before_line_bound(tmp_path):
    folder = tmp_path / "incident"
    folder.mkdir()

    target = write_incident_log(
        folder,
        ("first\nembedded\r\nline", "second"),
        max_lines=2,
    )

    assert target.read_text(encoding="utf-8") == (
        "first embedded line\nsecond\n"
    )


def test_incident_log_does_not_consume_budget_for_empty_utf8_truncation(tmp_path):
    folder = tmp_path / "incident"
    folder.mkdir()

    target = write_incident_log(
        folder,
        ("ёж", "x"),
        max_bytes=2,
        max_lines=2,
    )

    assert target.read_text(encoding="utf-8") == "x\n"


def test_scheduler_boundary_exposes_only_canonical_current_task():
    assert get_current_task_name() is None

    with scheduler_task_run(
        profile="profile-a",
        task=_task(),
        registry=("Research",),
    ):
        assert get_current_task_name() == "Research"
        metadata = build_incident_metadata(
            profile="profile-a",
            exception=ValueError("synthetic"),
        )
        assert metadata.task == "Research"

    assert get_current_task_name() is None


def test_save_error_log_keeps_original_error_and_writes_incident_bundle(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
    script.config_name = "profile-a"
    script.__dict__["config"] = SimpleNamespace(
        Error_LlmAnalysis=False,
        Error_SaveError=True,
        Error_SaveErrorCount=30,
    )

    try:
        raise RuntimeError("password=raw-secret C:\\Users\\operator\\error.log")
    except RuntimeError:
        logger.info("до ошибки")
        logger.error("ошибка с password=raw-secret")
        script.save_error_log()

    bundles = sorted((tmp_path / "log" / "error" / "profile-a").iterdir())
    assert len(bundles) == 1
    metadata = json.loads(
        (bundles[0] / "incident.json").read_text(encoding="utf-8")
    )
    assert metadata["profile"] == "profile-a"
    assert metadata["task"] is None
    assert metadata["exception_type"] == "RuntimeError"
    assert metadata["trace_id"] is None
    assert metadata["span_id"] is None
    assert (bundles[0] / "log.txt").exists()


def test_save_error_log_does_not_mask_original_when_metadata_write_fails(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
    script.config_name = "profile-a"
    script.__dict__["config"] = SimpleNamespace(
        Error_LlmAnalysis=False,
        Error_SaveError=True,
        Error_SaveErrorCount=30,
    )

    with patch(
        "module.observability.incident.write_incident_metadata",
        side_effect=OSError("synthetic metadata failure"),
    ):
        try:
            raise ValueError("исходная ошибка")
        except ValueError:
            logger.error("исходная ошибка")
            script.save_error_log()

    bundles = sorted((tmp_path / "log" / "error" / "profile-a").iterdir())
    assert len(bundles) == 1
    assert not (bundles[0] / "incident.json").exists()
    assert (bundles[0] / "log.txt").exists()
