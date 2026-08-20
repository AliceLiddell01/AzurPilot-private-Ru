"""Типизированные наблюдения времени выполнения с безопасным отказом для интерфейса события."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, TypedDict
from uuid import uuid4

from deploy.atomic import (
    atomic_read_text,
    atomic_replace,
    file_remove,
    file_write,
    replace_tmp,
    to_tmp_file,
)
from module.logger import logger
from module.webui.state_lock import (
    STATE_LOCK_RETRY_INTERVAL_SECONDS,
    STATE_LOCK_TIMEOUT_SECONDS,
    state_write_lock,
)

EVENT_OBSERVATION_SCHEMA_VERSION = 3
EVENT_OBSERVATION_ROOT = Path("./config/state/event_observation")
EVENT_OBSERVATION_MAX_AGE = timedelta(hours=48)
ObservationSource = Literal[
    "dashboard_ocr",
    "event_shop_ocr",
    "event_pt_delta",
    "event_shop_scanner",
    "mission_scanner",
    "fixture",
    "replay",
]
CurrencyEvidenceSource = Literal["dashboard_ocr", "event_shop_ocr"]
CurrentPtEvidenceSource = Literal["event_shop_ocr", "event_pt_delta"]
EventPtTotalEvidenceSource = Literal["dashboard_ocr"]


class ObservationFinding(TypedDict):
    code: str
    message: str
    path: str


def _safe_key(value: str, fallback: str) -> str:
    raw = str(value or fallback)
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", raw).strip("._-") or fallback
    return f"{slug[:48]}-{sha256(raw.encode('utf-8')).hexdigest()[:10]}"


def _observation_identity_location(
    instance: str,
    event_id: str,
    server: str,
    root: Path | str,
) -> tuple[Path, str]:
    directory = Path(root) / _safe_key(instance, "alas")
    prefix = f"{_safe_key(server, 'EN')}-{_safe_key(event_id, 'event')}-"
    return directory, prefix


def event_observation_path(
    instance: str,
    event_id: str,
    server: str,
    root: Path | str = EVENT_OBSERVATION_ROOT,
    *,
    source_revision: str = "",
) -> Path:
    directory, prefix = _observation_identity_location(
        instance,
        event_id,
        server,
        root,
    )
    return directory / f"{prefix}{_safe_key(source_revision, 'revision')}.json"


def empty_event_observation(
    event_id: str, server: str, instance: str, source_revision: str = ""
) -> dict[str, Any]:
    return {
        "schema_version": EVENT_OBSERVATION_SCHEMA_VERSION,
        "event_id": str(event_id),
        "server": str(server).upper(),
        "instance": str(instance),
        "source_revision": str(source_revision),
        "observed_at": "",
        "source": "",
        "current_pt": None,
        "current_pt_source": "",
        "current_pt_observed_at": "",
        "current_pt_status": "unavailable",
        "event_pt_total": None,
        "event_pt_total_source": "",
        "event_pt_total_observed_at": "",
        "event_pt_total_status": "unavailable",
        "shop_source": "",
        "shop_observed_at": "",
        "shop_items": [],
        "maps": [],
        "missions": [],
        "findings": [],
    }


def normalize_current_pt_value(value: Any) -> int | None:
    """Нормализовать наблюдаемое значение PT как неотрицательное целое."""

    if value is None or value == "":
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _finding(code: str, message: str, path: str = "") -> ObservationFinding:
    return {"code": code, "message": message, "path": path}


def normalize_event_observation(
    raw: Any,
    *,
    event_id: str,
    server: str,
    instance: str,
    source_revision: str = "",
) -> dict[str, Any]:
    result = empty_event_observation(event_id, server, instance, source_revision)
    if not isinstance(raw, Mapping):
        return result
    if int(raw.get("schema_version", 0) or 0) != EVENT_OBSERVATION_SCHEMA_VERSION:
        result["findings"].append(
            _finding(
                "observation_schema_unsupported",
                "Версия EventObservation не поддерживается",
                "schema_version",
            )
        )
        return result
    if str(raw.get("event_id") or "") != str(event_id):
        result["findings"].append(
            _finding(
                "cross_event_rejected",
                "Наблюдение относится к другому событию",
                "event_id",
            )
        )
        return result
    if str(raw.get("server") or "").upper() != str(server).upper():
        result["findings"].append(
            _finding(
                "cross_server_rejected",
                "Наблюдение относится к другому серверу",
                "server",
            )
        )
        return result
    if str(raw.get("instance") or "") != str(instance):
        result["findings"].append(
            _finding(
                "cross_profile_rejected",
                "Наблюдение относится к другому профилю",
                "instance",
            )
        )
        return result
    if str(raw.get("source_revision") or "") != str(source_revision):
        result["findings"].append(
            _finding(
                "cross_revision_rejected",
                "Наблюдение относится к другой ревизии источника",
                "source_revision",
            )
        )
        return result
    result["observed_at"] = str(raw.get("observed_at") or "")
    result["source"] = str(raw.get("source") or "")
    result["current_pt"] = normalize_current_pt_value(raw.get("current_pt"))
    result["event_pt_total"] = normalize_current_pt_value(raw.get("event_pt_total"))
    for field in (
        "current_pt_source",
        "current_pt_observed_at",
        "current_pt_status",
        "event_pt_total_source",
        "event_pt_total_observed_at",
        "event_pt_total_status",
        "shop_source",
        "shop_observed_at",
    ):
        result[field] = str(raw.get(field) or "")
    for field in ("shop_items", "maps", "missions", "findings"):
        value = raw.get(field)
        if isinstance(value, list):
            result[field] = [dict(item) for item in value if isinstance(item, Mapping)]
    return result


def observation_age(
    observation: Mapping[str, Any], now: datetime | None = None
) -> timedelta | None:
    value = str(observation.get("observed_at") or "").replace("Z", "+00:00")
    try:
        observed = datetime.fromisoformat(value)
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current - observed.replace(tzinfo=None)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=current.tzinfo)
    return current - observed.astimezone(timezone.utc)


def observation_is_fresh(
    observation: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age: timedelta = EVENT_OBSERVATION_MAX_AGE,
) -> bool:
    age = observation_age(observation, now)
    return age is not None and timedelta(0) <= age <= max_age


def _pt_evidence_timestamp(value: Any) -> datetime | None:
    try:
        observed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc)


def current_pt_candidate_is_newer(
    candidate_timestamp: Any, existing: Mapping[str, Any]
) -> bool:
    """Принимать только доказанно более свежие данные PT; равное время не заменяет запись."""

    candidate_at = _pt_evidence_timestamp(candidate_timestamp)
    existing_at = _pt_evidence_timestamp(
        existing.get("current_pt_observed_at") or existing.get("observed_at")
    )
    return candidate_at is not None and (
        existing_at is None or candidate_at > existing_at
    )


def event_pt_total_candidate_is_newer(
    candidate_timestamp: Any, existing: Mapping[str, Any]
) -> bool:
    """Принимать только доказанно более свежий накопительный счётчик PT."""

    candidate_at = _pt_evidence_timestamp(candidate_timestamp)
    existing_at = _pt_evidence_timestamp(
        existing.get("event_pt_total_observed_at") or existing.get("observed_at")
    )
    return candidate_at is not None and (
        existing_at is None or candidate_at > existing_at
    )


def apply_current_pt_evidence(
    observation: dict[str, Any],
    *,
    value: Any,
    timestamp: str,
    source: CurrentPtEvidenceSource,
) -> bool:
    """Применить доказательство текущего доступного баланса PT."""

    if source not in {"event_shop_ocr", "event_pt_delta"}:
        raise ValueError("Текущий баланс PT нельзя обновлять накопительным счётчиком")
    if not current_pt_candidate_is_newer(timestamp, observation):
        return False
    if current_pt_candidate_is_newer(
        timestamp,
        {"current_pt_observed_at": observation.get("observed_at")},
    ):
        observation["source"] = source
        observation["observed_at"] = timestamp
    observation["current_pt_source"] = source
    observation["current_pt_observed_at"] = timestamp
    observation["current_pt"] = normalize_current_pt_value(value)
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
                "OCR не предоставил валидный текущий баланс PT",
                "current_pt",
            )
        )
    return True


def apply_event_pt_total_evidence(
    observation: dict[str, Any],
    *,
    value: Any,
    timestamp: str,
    source: EventPtTotalEvidenceSource,
) -> bool:
    """Применить свежий накопительный счётчик заработанных PT без подмены баланса."""

    if source != "dashboard_ocr":
        raise ValueError("Накопительный счётчик PT принимается только из Dashboard OCR")
    normalized = normalize_current_pt_value(value)
    if normalized is None:
        return False
    if not event_pt_total_candidate_is_newer(timestamp, observation):
        return False
    previous = normalize_current_pt_value(observation.get("event_pt_total"))
    if previous is not None and normalized < previous:
        return False
    if event_pt_total_candidate_is_newer(
        timestamp,
        {"event_pt_total_observed_at": observation.get("observed_at")},
    ):
        observation["source"] = source
        observation["observed_at"] = timestamp
    observation["event_pt_total_source"] = source
    observation["event_pt_total_observed_at"] = timestamp
    observation["event_pt_total"] = normalized
    total_evidence = {"observed_at": timestamp}
    observation["event_pt_total_status"] = (
        "observed" if observation_is_fresh(total_evidence) else "stale"
    )
    observation["findings"] = [
        item
        for item in observation.get("findings", [])
        if item.get("path") != "event_pt_total"
    ]
    return True


def reset_event_pt_total_anchor(observation: dict[str, Any]) -> bool:
    """Сбросить дельта-якорь после абсолютного OCR-баланса магазина."""

    changed = any(
        (
            observation.get("event_pt_total") is not None,
            bool(observation.get("event_pt_total_source")),
            bool(observation.get("event_pt_total_observed_at")),
            str(observation.get("event_pt_total_status") or "") != "unavailable",
        )
    )
    observation["event_pt_total"] = None
    observation["event_pt_total_source"] = ""
    observation["event_pt_total_observed_at"] = ""
    observation["event_pt_total_status"] = "unavailable"
    observation["findings"] = [
        item
        for item in observation.get("findings", [])
        if item.get("path") != "event_pt_total"
    ]
    return changed


def event_observation_write_lock(
    path: Path,
    *,
    timeout: float = STATE_LOCK_TIMEOUT_SECONDS,
    retry_interval: float = STATE_LOCK_RETRY_INTERVAL_SECONDS,
):
    """Сериализовать наблюдение через общую блокировку состояния с ограниченным ожиданием."""

    return state_write_lock(
        path,
        timeout=timeout,
        retry_interval=retry_interval,
    )


def save_event_observation(
    instance: str,
    observation: Mapping[str, Any],
    *,
    source_revision: str,
    root: Path | str = EVENT_OBSERVATION_ROOT,
    allow_nonproduction: bool = False,
) -> Path:
    event_id = str(observation.get("event_id") or "")
    server = str(observation.get("server") or "").upper()
    expected_revision = str(source_revision or "")
    observed_revision = str(observation.get("source_revision") or "")
    if not event_id or not server:
        raise ValueError("EventObservation требует event_id и server")
    if str(observation.get("instance") or "") != str(instance):
        raise ValueError("EventObservation относится к другому профилю")
    if observed_revision != expected_revision:
        raise ValueError("EventObservation относится к другой ревизии источника")
    normalized = normalize_event_observation(
        observation,
        event_id=event_id,
        server=server,
        instance=instance,
        source_revision=expected_revision,
    )
    if normalized["source"] in {"fixture", "replay"} and not allow_nonproduction:
        raise ValueError(
            "Наблюдения источников fixture/replay запрещено сохранять как производственные"
        )
    path = event_observation_path(
        instance,
        event_id,
        server,
        root,
        source_revision=expected_revision,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = to_tmp_file(str(path))
    try:
        file_write(
            temp,
            json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        replace_tmp(temp, str(path))
    except BaseException:
        try:
            file_remove(temp)
        except OSError:
            pass
        raise
    return path


def prune_stale_event_observations(
    *,
    instance: str,
    event_id: str,
    server: str,
    keep_revision: str,
    root: Path | str = EVENT_OBSERVATION_ROOT,
    now: datetime | None = None,
    max_age: timedelta = EVENT_OBSERVATION_MAX_AGE,
) -> tuple[Path, ...]:
    """Удалить только истёкшие snapshots других ревизий той же identity.

    Функция вызывается под identity-lock. Свежие sibling-ревизии сохраняются, поэтому
    поздний writer старого процесса не может удалить новый snapshot только из-за
    того, что его собственная ревизия отличается.
    """

    directory, prefix = _observation_identity_location(instance, event_id, server, root)
    keep_path = event_observation_path(
        instance,
        event_id,
        server,
        root,
        source_revision=keep_revision,
    )
    current = now or datetime.now(timezone.utc)
    cutoff = current.timestamp() - max_age.total_seconds()
    removed: list[Path] = []
    try:
        candidates = tuple(directory.glob(f"{prefix}*.json"))
    except OSError as exc:
        logger.warning(
            f"[WebUI — наблюдение ивента] Не удалось перечислить старые snapshots {directory}: {exc}"
        )
        return ()
    for candidate in candidates:
        if candidate == keep_path:
            continue
        try:
            if candidate.stat().st_mtime > cutoff:
                continue
            file_remove(str(candidate))
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(
                f"[WebUI — наблюдение ивента] Не удалось удалить истёкший snapshot {candidate}: {exc}"
            )
            continue
        removed.append(candidate)
    return tuple(removed)


def load_event_observation(
    instance: str,
    event_id: str,
    server: str,
    source_revision: str = "",
    *,
    root: Path | str = EVENT_OBSERVATION_ROOT,
    allow_nonproduction: bool = False,
) -> dict[str, Any]:
    path = event_observation_path(
        instance, event_id, server, root, source_revision=source_revision
    )
    content = atomic_read_text(str(path))
    if not content:
        return empty_event_observation(event_id, server, instance, source_revision)
    try:
        raw = json.loads(content)
        result = normalize_event_observation(
            raw,
            event_id=event_id,
            server=server,
            instance=instance,
            source_revision=source_revision,
        )
    except (TypeError, ValueError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{uuid4().hex[:12]}")
        try:
            atomic_replace(str(path), str(backup))
        except OSError as backup_exc:
            logger.warning(
                f"[WebUI — наблюдение ивента] Не удалось сохранить повреждённый файл {path}: {backup_exc}"
            )
        else:
            logger.warning(
                f"[WebUI — наблюдение ивента] Повреждённый файл {path} сохранён как {backup}: {exc}"
            )
        return empty_event_observation(event_id, server, instance, source_revision)
    if result["source"] in {"fixture", "replay"} and not allow_nonproduction:
        clean = empty_event_observation(event_id, server, instance, source_revision)
        clean["findings"].append(
            _finding(
                "nonproduction_evidence_rejected",
                "Данные fixture/replay не используются в производственном интерфейсе",
                "source",
            )
        )
        return clean
    return result


def dashboard_pt_observation(
    *,
    instance: str,
    event_id: str,
    server: str,
    value: Any,
    recorded_at: Any,
    source_revision: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Сформировать наблюдение накопительного PT, не подменяя им баланс магазина."""

    result = empty_event_observation(event_id, server, instance, source_revision)
    result["source"] = "dashboard_ocr"
    result["observed_at"] = str(recorded_at or "")
    result["event_pt_total_source"] = "dashboard_ocr"
    result["event_pt_total_observed_at"] = str(recorded_at or "")
    result["event_pt_total"] = normalize_current_pt_value(value)
    if result["event_pt_total"] is None:
        result["findings"].append(
            _finding(
                "event_pt_total_unavailable",
                "OCR не предоставил валидный накопительный счётчик PT",
                "event_pt_total",
            )
        )
    fresh = observation_is_fresh(result, now=now)
    result["event_pt_total_status"] = (
        "observed"
        if fresh and result["event_pt_total"] is not None
        else "stale"
        if result["event_pt_total"] is not None
        else "unavailable"
    )
    if not fresh:
        result["findings"].append(
            _finding(
                "observation_stale",
                "OCR-наблюдение накопительного PT отсутствует или устарело",
                "observed_at",
            )
        )
    return result
