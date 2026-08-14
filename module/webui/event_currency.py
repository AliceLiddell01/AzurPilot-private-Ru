"""Bridge proven event-currency updates into EventShop scheduling."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from module.event_datamine.registry import EventArtifactRegistry
from module.logger import logger
from module.webui.event_observation import (
    EVENT_OBSERVATION_ROOT,
    load_event_observation,
    persist_current_pt_observation,
)
from module.webui.event_shop_priority import (
    EVENT_SHOP_PRIORITY_ROOT,
    load_event_shop_priority,
)

CurrencyEvidenceSource = Literal["dashboard_ocr", "event_shop_ocr"]


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def persist_event_currency_update(
    config: Any,
    value: Any,
    *,
    source: CurrencyEvidenceSource,
    observed_at: datetime | None = None,
    observation_root: Path | str = EVENT_OBSERVATION_ROOT,
    priority_root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> dict[str, Any] | None:
    """Persist exact-event PT evidence and wake EventShop after a proven increase.

    The caller-owned config object is reused for scheduling so an unsaved resource
    update and EventShop.NextRun are committed through one config state instead of
    racing two independent AzurLaneConfig instances.
    """

    instance = str(getattr(config, "config_name", "") or "")
    if not instance:
        raise ValueError("Для наблюдения валюты события требуется имя профиля")

    server = str(getattr(config, "SERVER", "EN") or "EN").upper()
    artifact = EventArtifactRegistry().resolve_current(server, datetime.now())
    if not isinstance(artifact, dict):
        return None
    spec = artifact.get("event_spec")
    if not isinstance(spec, dict):
        return None

    event_id = str(spec.get("id") or "")
    source_revision = str(spec.get("provenance", {}).get("revision") or "")
    if not event_id:
        return None

    evidence_at = observed_at or datetime.now(timezone.utc)
    candidate_timestamp = evidence_at.astimezone(timezone.utc).isoformat()
    previous = load_event_observation(
        instance,
        event_id,
        server,
        source_revision,
        root=observation_root,
    )
    previous_value = _optional_non_negative_int(previous.get("current_pt"))

    observation = persist_current_pt_observation(
        instance=instance,
        event_id=event_id,
        server=server,
        source_revision=source_revision,
        value=value,
        observed_at=evidence_at,
        source=source,
        root=observation_root,
    )

    # A concurrent newer writer may have won the persistence race.  Only the
    # exact candidate accepted by persistence is allowed to trigger scheduling.
    if str(observation.get("current_pt_observed_at") or "") != candidate_timestamp:
        return observation
    if str(observation.get("current_pt_source") or "") != source:
        return observation
    if str(observation.get("current_pt_status") or "") != "observed":
        return observation
    if source == "event_shop_ocr":
        return observation

    current_value = _optional_non_negative_int(observation.get("current_pt"))
    if (
        previous_value is None
        or current_value is None
        or current_value <= previous_value
    ):
        return observation

    priority_state = load_event_shop_priority(
        instance,
        event_id,
        root=priority_root,
    )
    active = (
        set(priority_state.get("priorities", {}))
        - set(priority_state.get("purchased", []))
        - set(priority_state.get("blocked", {}))
    )
    if not active:
        return observation
    if not config.is_task_enabled("EventShop"):
        return observation

    if config.task_call("EventShop", force_call=False):
        logger.info(
            "[Магазин события — приоритеты] "
            f"Баланс PT вырос {previous_value} -> {current_value}; "
            "EventShop поставлен на ближайший запуск"
        )
    return observation
