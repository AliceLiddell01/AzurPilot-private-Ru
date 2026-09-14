from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import module.webui.event_observation_update as observation_update_store
from module.webui.event_observation import (
    apply_current_pt_evidence,
    empty_event_observation,
    load_event_observation,
)
from module.webui.event_observation_update import (
    persist_current_pt_observation,
    persist_current_pt_transition,
)
from module.webui.event_shop_observation import persist_event_shop_observation


def _spec():
    return {
        "id": "event-test",
        "server": "EN",
        "provenance": {"revision": "d" * 40},
        "currencies": [],
        "shop_items": [],
    }


def test_transition_returns_exact_previous_value_from_locked_state(tmp_path):
    spec = _spec()
    first_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    second_at = first_at + timedelta(minutes=1)

    persist_current_pt_observation(
        instance="test-instance",
        event_id=spec["id"],
        server=spec["server"],
        source_revision=spec["provenance"]["revision"],
        value=200,
        observed_at=first_at,
        source="event_shop_ocr",
        root=tmp_path,
    )

    observation, previous_value, accepted = persist_current_pt_transition(
        instance="test-instance",
        event_id=spec["id"],
        server=spec["server"],
        source_revision=spec["provenance"]["revision"],
        value=150,
        observed_at=second_at,
        source="event_shop_ocr",
        root=tmp_path,
    )

    assert accepted is True
    assert previous_value == 200
    assert observation["current_pt"] == 150


def test_newer_pt_advances_global_observation_clock():
    base_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    candidate_at = base_at + timedelta(minutes=5)
    observation = empty_event_observation(
        "event-test", "EN", "test-instance", "d" * 40
    )
    observation.update(
        {
            "observed_at": base_at.isoformat(),
            "source": "event_shop_scanner",
            "current_pt": 100,
            "current_pt_source": "event_shop_ocr",
            "current_pt_observed_at": base_at.isoformat(),
            "current_pt_status": "observed",
        }
    )

    accepted = apply_current_pt_evidence(
        observation,
        value=150,
        timestamp=candidate_at.isoformat(),
        source="event_shop_ocr",
    )

    assert accepted is True
    assert observation["current_pt"] == 150
    assert observation["current_pt_observed_at"] == candidate_at.isoformat()
    assert observation["observed_at"] == candidate_at.isoformat()
    assert observation["source"] == "event_shop_ocr"


def test_newer_pt_does_not_roll_back_newer_global_shop_clock():
    pt_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    candidate_at = pt_at + timedelta(minutes=3)
    shop_at = pt_at + timedelta(minutes=6)
    observation = empty_event_observation(
        "event-test", "EN", "test-instance", "d" * 40
    )
    observation.update(
        {
            "observed_at": shop_at.isoformat(),
            "source": "event_shop_scanner",
            "current_pt": 100,
            "current_pt_source": "event_shop_ocr",
            "current_pt_observed_at": pt_at.isoformat(),
            "current_pt_status": "observed",
        }
    )

    accepted = apply_current_pt_evidence(
        observation,
        value=150,
        timestamp=candidate_at.isoformat(),
        source="event_shop_ocr",
    )

    assert accepted is True
    assert observation["current_pt"] == 150
    assert observation["current_pt_observed_at"] == candidate_at.isoformat()
    assert observation["observed_at"] == shop_at.isoformat()
    assert observation["source"] == "event_shop_scanner"


def test_pt_and_shop_writers_share_one_read_modify_write_lock(monkeypatch, tmp_path):
    spec = _spec()
    pt_save_entered = Event()
    release_pt_save = Event()
    shop_started = Event()
    shop_finished = Event()
    original_save = observation_update_store.save_event_observation

    def blocking_save(instance, observation, **kwargs):
        if (
            observation.get("current_pt") == 200
            and observation.get("shop_source") != "event_shop_scanner"
            and not pt_save_entered.is_set()
        ):
            pt_save_entered.set()
            assert release_pt_save.wait(timeout=5)
        return original_save(instance, observation, **kwargs)

    monkeypatch.setattr(observation_update_store, "save_event_observation", blocking_save)

    def write_pt():
        return persist_current_pt_observation(
            instance="test-instance",
            event_id=spec["id"],
            server=spec["server"],
            source_revision=spec["provenance"]["revision"],
            value=200,
            observed_at=datetime.now(timezone.utc),
            source="event_shop_ocr",
            root=tmp_path,
        )

    def write_shop():
        shop_started.set()
        try:
            return persist_event_shop_observation(
                instance="test-instance",
                spec=spec,
                runtime_items=[],
                observed_at=datetime.now(timezone.utc),
                root=tmp_path,
            )
        finally:
            shop_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        pt_future = executor.submit(write_pt)
        assert pt_save_entered.wait(timeout=5)
        shop_future = executor.submit(write_shop)
        assert shop_started.wait(timeout=5)
        try:
            assert not shop_finished.wait(timeout=1.0)
        finally:
            release_pt_save.set()

        assert shop_finished.wait(timeout=5)
        pt_future.result(timeout=5)
        shop_future.result(timeout=5)

    stored = load_event_observation(
        "test-instance",
        spec["id"],
        spec["server"],
        spec["provenance"]["revision"],
        root=tmp_path,
    )
    assert stored["current_pt"] == 200
    assert stored["current_pt_source"] == "event_shop_ocr"
    assert stored["shop_source"] == "event_shop_scanner"
    assert stored["shop_observed_at"]
