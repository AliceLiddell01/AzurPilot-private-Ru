"""Сериализованные операции чтения → изменения → записи EventObservation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from module.webui.event_observation import (
    EVENT_OBSERVATION_ROOT,
    CurrencyEvidenceSource,
    apply_current_pt_evidence,
    event_observation_path,
    event_observation_write_lock,
    load_event_observation,
    normalize_current_pt_value,
    save_event_observation,
)

ObservationUpdater = Callable[[dict[str, Any]], bool]


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
    """Выполнить одну сериализованную операцию чтения → изменения → записи."""

    lock_path = _observation_lock_path(
        instance,
        event_id,
        server,
        source_revision,
        root,
    )
    with event_observation_write_lock(lock_path):
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

        previous_value = normalize_current_pt_value(observation.get("current_pt"))
        accepted = apply_current_pt_evidence(
            observation,
            value=value,
            timestamp=timestamp,
            source=source,
        )
        return accepted

    observation = update_event_observation(
        instance=instance,
        event_id=event_id,
        server=server,
        source_revision=source_revision,
        updater=apply,
        root=root,
    )
    return observation, previous_value, accepted


def persist_current_pt_observation(
    *,
    instance: str,
    event_id: str,
    server: str,
    source_revision: str,
    value: Any,
    observed_at: datetime | None = None,
    source: CurrencyEvidenceSource = "event_shop_ocr",
    root: Path | str = EVENT_OBSERVATION_ROOT,
) -> dict[str, Any]:
    """Сохранить свежее OCR-наблюдение PT через единственную транзакцию обновления."""

    observation, _, _ = persist_current_pt_transition(
        instance=instance,
        event_id=event_id,
        server=server,
        source_revision=source_revision,
        value=value,
        observed_at=observed_at,
        source=source,
        root=root,
    )
    return observation
