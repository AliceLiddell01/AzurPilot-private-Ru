from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

from module.webui.event_observation import (
    _current_pt_candidate_is_newer,
    load_event_observation,
    persist_current_pt_observation,
)


def test_later_arriving_older_pt_ocr_cannot_replace_newer_persisted_evidence(tmp_path):
    revision = "c" * 40
    newer_at = datetime(2026, 8, 13, 17, 2, 29, tzinfo=timezone.utc)
    older_at = newer_at - timedelta(minutes=5)
    persist_current_pt_observation(
        instance="ap",
        event_id="en:test",
        server="EN",
        source_revision=revision,
        value=777,
        observed_at=newer_at,
        root=tmp_path,
    )
    result = persist_current_pt_observation(
        instance="ap",
        event_id="en:test",
        server="EN",
        source_revision=revision,
        value=111,
        observed_at=older_at,
        root=tmp_path,
    )
    assert result["current_pt"] == 777
    assert result["current_pt_observed_at"] == newer_at.isoformat()
    stored = load_event_observation("ap", "en:test", "EN", revision, root=tmp_path)
    assert stored["current_pt"] == 777


def test_parallel_pt_writers_preserve_newest_evidence(tmp_path):
    revision = "c" * 40
    newer_at = datetime(2026, 8, 13, 17, 2, 29, tzinfo=timezone.utc)
    older_at = newer_at - timedelta(minutes=5)
    barrier = Barrier(2)

    def write(value, observed_at):
        barrier.wait(timeout=5)
        return persist_current_pt_observation(
            instance="ap",
            event_id="en:parallel",
            server="EN",
            source_revision=revision,
            value=value,
            observed_at=observed_at,
            root=tmp_path,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        older = executor.submit(write, 111, older_at)
        newer = executor.submit(write, 777, newer_at)
        older.result(timeout=10)
        newer.result(timeout=10)

    stored = load_event_observation(
        "ap", "en:parallel", "EN", revision, root=tmp_path
    )
    assert stored["current_pt"] == 777
    assert stored["current_pt_observed_at"] == newer_at.isoformat()


def test_equal_pt_timestamp_keeps_existing_evidence(tmp_path):
    revision = "c" * 40
    observed_at = datetime(2026, 8, 13, 17, 2, 29, tzinfo=timezone.utc)
    persist_current_pt_observation(
        instance="ap",
        event_id="en:test",
        server="EN",
        source_revision=revision,
        value=777,
        observed_at=observed_at,
        root=tmp_path,
    )
    result = persist_current_pt_observation(
        instance="ap",
        event_id="en:test",
        server="EN",
        source_revision=revision,
        value=111,
        observed_at=observed_at,
        root=tmp_path,
    )
    assert result["current_pt"] == 777


def test_pt_timestamp_comparison_fails_closed_on_invalid_candidate():
    existing = {"current_pt_observed_at": "2026-08-13T17:02:29+00:00"}
    assert not _current_pt_candidate_is_newer("invalid", existing)
