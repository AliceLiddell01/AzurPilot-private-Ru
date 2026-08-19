"""Мост доказанных изменений валюты события в планировщик EventShop."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from module.event_datamine.registry import load_event_artifact_registry
from module.webui.event_observation import (
    EVENT_OBSERVATION_ROOT,
    CurrencyEvidenceSource,
    normalize_current_pt_value,
)
from module.webui.event_observation_update import persist_current_pt_transition
from module.webui.event_shop_priority import (
    EVENT_SHOP_PRIORITY_ROOT,
    wake_event_shop_after_currency_increase,
)


def persist_event_currency_update(
    config: Any,
    value: Any,
    *,
    source: CurrencyEvidenceSource,
    observed_at: datetime | None = None,
    observation_root: Path | str = EVENT_OBSERVATION_ROOT,
    priority_root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> dict[str, Any] | None:
    """Сохранить PT и передать доказанный переход единому wake-контракту EventShop.

    Точное предыдущее PT читается под той же блокировкой, под которой
    принимаются новые данные наблюдения. Поэтому конкурентная запись не может
    превратить фактическое снижение в ложный сигнал роста.
    """

    instance = str(getattr(config, "config_name", "") or "")
    if not instance:
        raise ValueError("Для наблюдения валюты события требуется имя профиля")

    server = str(getattr(config, "SERVER", "EN") or "EN").upper()
    artifact = load_event_artifact_registry().resolve_current(server, datetime.now())
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
    observation, previous_value, accepted = persist_current_pt_transition(
        instance=instance,
        event_id=event_id,
        server=server,
        source_revision=source_revision,
        value=value,
        observed_at=evidence_at,
        source=source,
        root=observation_root,
    )

    if not accepted:
        return observation
    if str(observation.get("current_pt_source") or "") != source:
        return observation
    if str(observation.get("current_pt_status") or "") != "observed":
        return observation

    current_value = normalize_current_pt_value(observation.get("current_pt"))
    wake_event_shop_after_currency_increase(
        config=config,
        event_id=event_id,
        previous_value=previous_value,
        current_value=current_value,
        source=source,
        root=priority_root,
    )
    return observation
