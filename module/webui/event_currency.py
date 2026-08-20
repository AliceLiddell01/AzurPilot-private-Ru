"""Мост доказанных изменений валюты события в планировщик EventShop."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from module.event_datamine.registry import load_event_artifact_registry
from module.webui.event_observation import (
    EVENT_OBSERVATION_ROOT,
    CurrencyEvidenceSource,
    normalize_current_pt_value,
)
from module.webui.event_observation_update import (
    persist_current_pt_transition,
    persist_event_pt_total_transition,
)
from module.webui.event_shop_priority import (
    EVENT_SHOP_PRIORITY_ROOT,
    wake_event_shop_after_currency_increase,
)


def _shop_uses_runtime_currency(spec: Mapping[str, Any], runtime_token: str) -> bool:
    """Проверить связь каталога магазина с runtime-валютой декларативно через EventSpec."""

    currency_ids = {
        str(currency.get("id"))
        for currency in spec.get("currencies") or []
        if isinstance(currency, Mapping)
        and currency.get("id") is not None
        and str(currency.get("runtime_token") or "").lower()
        == str(runtime_token or "").lower()
    }
    if not currency_ids:
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("currency_id") is not None
        and str(item.get("currency_id")) in currency_ids
        for item in spec.get("shop_items") or []
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
    """Сохранить доказательство PT, не смешивая накопительный счётчик и баланс.

    `dashboard_ocr` содержит накопительный PT за событие. Он используется только
    как дельта относительно предыдущего счётчика и только после того, как магазин
    уже дал абсолютный текущий баланс. `event_shop_ocr` остаётся единственным
    абсолютным источником доступного к покупке PT и сам EventShop не пробуждает.
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
    provenance = spec.get("provenance")
    source_revision = (
        str(provenance.get("revision") or "")
        if isinstance(provenance, Mapping)
        else ""
    )
    if not event_id:
        return None

    evidence_at = observed_at or datetime.now(timezone.utc)

    if source == "event_shop_ocr":
        observation, _, _ = persist_current_pt_transition(
            instance=instance,
            event_id=event_id,
            server=server,
            source_revision=source_revision,
            value=value,
            observed_at=evidence_at,
            source="event_shop_ocr",
            root=observation_root,
        )
        return observation

    if source != "dashboard_ocr":
        raise ValueError(f"Неподдерживаемый источник PT: {source}")

    observation, previous_value, current_value, derived = (
        persist_event_pt_total_transition(
            instance=instance,
            event_id=event_id,
            server=server,
            source_revision=source_revision,
            value=value,
            observed_at=evidence_at,
            source="dashboard_ocr",
            derive_current_pt=_shop_uses_runtime_currency(spec, "pt"),
            root=observation_root,
        )
    )

    if not derived:
        return observation
    if str(observation.get("current_pt_source") or "") != "event_pt_delta":
        return observation
    if str(observation.get("current_pt_status") or "") != "observed":
        return observation

    current_value = normalize_current_pt_value(current_value)
    wake_event_shop_after_currency_increase(
        config=config,
        event_id=event_id,
        previous_value=previous_value,
        current_value=current_value,
        source="event_pt_delta",
        root=priority_root,
    )
    return observation
