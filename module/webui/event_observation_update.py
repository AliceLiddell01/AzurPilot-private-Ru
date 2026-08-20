"""Сериализованные операции чтения → изменения → записи EventObservation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from module.webui.event_observation import (
    EVENT_OBSERVATION_ROOT,
    CurrentPtEvidenceSource,
    EventPtTotalEvidenceSource,
    apply_current_pt_evidence,
    apply_event_pt_total_evidence,
    event_observation_path,
    event_observation_write_lock,
    load_event_observation,
    normalize_current_pt_value,
    observation_is_fresh,
    prune_stale_event_observations,
    reset_event_pt_total_anchor,
    save_event_observation,
)

ObservationUpdater = Callable[[dict[str, Any]], bool]


def _observation_lock_path(
    instance: str,
    event_id: str,
    server: str,
    root: Path | str,
) -> Path:
    """Вернуть один lock-path для всех ревизий одной EventObservation identity."""

    observation_path = event_observation_path(
        instance,
        event_id,
        server,
        root,
        source_revision="",
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
            observation["findings"] = [
                item
                for item in observation.get("findings", [])
                if item.get("code") != "observation_schema_unsupported"
            ]
            save_event_observation(
                instance,
                observation,
                source_revision=source_revision,
                root=root,
            )
            prune_stale_event_observations(
                instance=instance,
                event_id=event_id,
                server=server,
                keep_revision=source_revision,
                root=root,
            )
        return observation


def _evidence_timestamp(observed_at: datetime | None) -> tuple[datetime, str]:
    if observed_at is None:
        evidence_at = datetime.now(timezone.utc)
    else:
        evidence_at = observed_at
        if evidence_at.tzinfo is None or evidence_at.utcoffset() is None:
            raise ValueError("Метка времени доказательства должна содержать часовой пояс")
    evidence_at = evidence_at.astimezone(timezone.utc)
    return evidence_at, evidence_at.isoformat()


def persist_current_pt_transition(
    *,
    instance: str,
    event_id: str,
    server: str,
    source_revision: str,
    value: Any,
    observed_at: datetime | None = None,
    source: CurrentPtEvidenceSource = "event_shop_ocr",
    root: Path | str = EVENT_OBSERVATION_ROOT,
) -> tuple[dict[str, Any], int | None, bool]:
    """Сохранить доступный PT и вернуть точное предыдущее значение из той же блокировки."""

    _, timestamp = _evidence_timestamp(observed_at)
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
        if accepted and source == "event_shop_ocr":
            reset_event_pt_total_anchor(observation)
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


def persist_event_pt_total_transition(
    *,
    instance: str,
    event_id: str,
    server: str,
    source_revision: str,
    value: Any,
    observed_at: datetime | None = None,
    source: EventPtTotalEvidenceSource = "dashboard_ocr",
    derive_current_pt: bool = True,
    root: Path | str = EVENT_OBSERVATION_ROOT,
) -> tuple[dict[str, Any], int | None, int | None, bool]:
    """Сохранить накопительный PT и при доказанной связи применить только его прирост."""

    evidence_at, timestamp = _evidence_timestamp(observed_at)
    previous_current: int | None = None
    current_value: int | None = None
    derived = False

    def apply(observation: dict[str, Any]) -> bool:
        nonlocal previous_current
        nonlocal current_value
        nonlocal derived

        previous_total = normalize_current_pt_value(observation.get("event_pt_total"))
        previous_current = normalize_current_pt_value(observation.get("current_pt"))
        accepted = apply_event_pt_total_evidence(
            observation,
            value=value,
            timestamp=timestamp,
            source=source,
        )
        current_value = previous_current
        if not accepted:
            return False

        current_total = normalize_current_pt_value(observation.get("event_pt_total"))
        if (
            not derive_current_pt
            or previous_total is None
            or current_total is None
            or current_total <= previous_total
            or previous_current is None
            or str(observation.get("event_pt_total_status") or "") != "observed"
        ):
            return True

        current_evidence = {
            "observed_at": str(observation.get("current_pt_observed_at") or "")
        }
        if not observation_is_fresh(current_evidence, now=evidence_at):
            return True

        derived = apply_current_pt_evidence(
            observation,
            value=previous_current + (current_total - previous_total),
            timestamp=timestamp,
            source="event_pt_delta",
        )
        current_value = normalize_current_pt_value(observation.get("current_pt"))
        return True

    observation = update_event_observation(
        instance=instance,
        event_id=event_id,
        server=server,
        source_revision=source_revision,
        updater=apply,
        root=root,
    )
    return observation, previous_current, current_value, derived


def persist_current_pt_observation(
    *,
    instance: str,
    event_id: str,
    server: str,
    source_revision: str,
    value: Any,
    observed_at: datetime | None = None,
    source: CurrentPtEvidenceSource = "event_shop_ocr",
    root: Path | str = EVENT_OBSERVATION_ROOT,
) -> dict[str, Any]:
    """Сохранить свежий текущий баланс PT через единственную транзакцию обновления."""

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
