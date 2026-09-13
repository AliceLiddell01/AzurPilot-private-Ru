import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import module.webui.event_observation as observation_store
import module.webui.event_observation_update as observation_update_store
from module.webui.event_observation import (
    current_pt_candidate_is_newer,
    event_observation_path,
    load_event_observation,
)
from module.webui.event_observation_update import persist_current_pt_observation


def test_pt_persistence_is_owned_only_by_transaction_module():
    assert not hasattr(observation_store, "persist_current_pt_observation")
    assert callable(observation_update_store.persist_current_pt_observation)


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
    assert not current_pt_candidate_is_newer("invalid", existing)


def test_revision_cleanup_removes_only_expired_sibling_snapshots(tmp_path):
    event_id = "en:cleanup"
    server = "EN"
    instance = "ap"
    now = datetime.now(timezone.utc)
    expired_revision = "a" * 40
    recent_revision = "b" * 40
    current_revision = "c" * 40

    for revision, value in ((expired_revision, 100), (recent_revision, 200)):
        persist_current_pt_observation(
            instance=instance,
            event_id=event_id,
            server=server,
            source_revision=revision,
            value=value,
            observed_at=now - timedelta(minutes=5),
            root=tmp_path,
        )

    expired_path = event_observation_path(
        instance,
        event_id,
        server,
        tmp_path,
        source_revision=expired_revision,
    )
    recent_path = event_observation_path(
        instance,
        event_id,
        server,
        tmp_path,
        source_revision=recent_revision,
    )
    expired_mtime = (now - timedelta(hours=49)).timestamp()
    recent_mtime = (now - timedelta(hours=1)).timestamp()
    os.utime(expired_path, (expired_mtime, expired_mtime))
    os.utime(recent_path, (recent_mtime, recent_mtime))

    persist_current_pt_observation(
        instance=instance,
        event_id=event_id,
        server=server,
        source_revision=current_revision,
        value=300,
        observed_at=now,
        root=tmp_path,
    )

    current_path = event_observation_path(
        instance,
        event_id,
        server,
        tmp_path,
        source_revision=current_revision,
    )
    assert not expired_path.exists()
    assert recent_path.is_file()
    assert current_path.is_file()
