"""Сериализованные read-modify-write операции над EventObservation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from module.webui.event_observation import (
    EVENT_OBSERVATION_ROOT,
    _current_pt_candidate_is_newer,
    _event_observation_write_lock,
    _finding,
    _optional_non_negative_int,
    event_observation_path,
    load_event_observation,
    observation_is_fresh,
    save_event_observation,
)

ObservationUpdater = Callable[[dict[str, Any]], bool]
CurrencyEvidenceSource = Literal["dashboard_ocr", "event_shop_ocr"]


def _observation_lock_path(
    instance: str,
    event_id: str,
    server: str,
    source_revision: str,
    root: Path | str,
) -> Path:
    observation_path = event_observation_path(
        instance,
        event_id,
        server,
        root,
        source_revision=source_revision,
    )
    return observation_path.with_suffix(f"{observation_path.suffix}.lock")


def update_event_observation(
    *,
    instance: str,
    event_id: str,
    server: str,
    source_revision: str,
    updater: ObservationUpdater,
    root: Path | str = EVENT_OBSERVATION_ROOT,
) -> dict[str, Any]:
    """Выполнить один сериализованный load → mutate → save для observation."""

    lock_path = _observation_lock_path(
        instance,
        event_id,
        server,
        source_revision,
        root,
    )
    with _event_observation_write_lock(lock_path):
        observation = load_event_observation(
            instance,
            event_id,
            server,
            source_revision,
            root=root,
        )
        if updater(observation):
            save_event_observation(instance, observation, root=root)
        return observation


def persist_current_pt_transition(
    *,
    instance: str,
    event_id: str,
    server: str,
    source_revision: str,
    value: Any,
    observed_at: datetime | None = None,
    source: CurrencyEvidenceSource = "event_shop_ocr",
    root: Path | str = EVENT_OBSERVATION_ROOT,
) -> tuple[dict[str, Any], int | None, bool]:
    """Сохранить PT и вернуть точное предыдущее значение из той же блокировки."""

    timestamp = (
        (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    )
    previous_value: int | None = None
    accepted = False

    def apply(observation: dict[str, Any]) -> bool:
        nonlocal previous_value
        nonlocal accepted

        previous_value = _optional_non_negative_int(observation.get("current_pt"))
        if not _current_pt_candidate_is_newer(timestamp, observation):
            return False

        if not observation.get("source"):
            observation["source"] = source
        if not observation.get("observed_at"):
            observation["observed_at"] = timestamp
        observation["current_pt_source"] = source
        observation["current_pt_observed_at"] = timestamp
        observation["current_pt"] = _optional_non_negative_int(value)
        current_pt_evidence = {"observed_at": timestamp}
        observation["current_pt_status"] = (
            "observed"
            if observation["current_pt"] is not None
            and observation_is_fresh(current_pt_evidence)
            else "stale"
            if observation["current_pt"] is not None
            else "unavailable"
        )
        observation["findings"] = [
            item
            for item in observation.get("findings", [])
            if item.get("path") != "current_pt"
        ]
        if observation["current_pt"] is None:
            observation["findings"].append(
                _finding(
                    "current_pt_unavailable",
                    "OCR не предоставил валидный баланс PT",
                    "current_pt",
                )
            )
        accepted = True
        return True

    observation = update_event_observation(
        instance=instance,
        event_id=event_id,
        server=server,
        source_revision=source_revision,
        updater=apply,
        root=root,
    )
    return observation, previous_value, accepted
