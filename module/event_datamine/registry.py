"""Детерминированный registry generated Event artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any

from module.event_datamine.artifact import (
    BUILTIN_ARTIFACT_ROOT,
    canonical_json,
    load_artifact,
)
from module.event_datamine.discovery import (
    EventDiscoveryError,
    server_local_wall_time,
)

EVENT_REGISTRY_SCHEMA_VERSION = 2
EVENT_REGISTRY_NAME = "index.json"


def registry_digest(data: Mapping[str, Any]) -> str:
    clean = dict(data)
    clean.pop("digest", None)
    return sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def _entry(path: Path, root: Path, artifact: Mapping[str, Any]) -> dict[str, Any]:
    spec = artifact["event_spec"]
    provenance = spec.get("provenance", {})
    return {
        "path": path.relative_to(root).as_posix(),
        "role": str(artifact.get("role") or "production"),
        "id": str(spec.get("id") or ""),
        "server": str(spec.get("server") or "").upper(),
        "source_status": str(spec.get("source_status") or "unsupported"),
        "farm_start": str(spec.get("farm_start") or ""),
        "farm_end": str(spec.get("farm_end") or ""),
        "shop_end": str(spec.get("shop_end") or ""),
        "revision": str(provenance.get("revision") or ""),
        "artifact_digest": str(artifact.get("digest") or ""),
    }


def _normalize_campaign_selectors(
    raw_bindings: Iterable[Any],
    entries: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    entry_list = list(entries)
    result: list[dict[str, str]] = []
    keys: set[tuple[str, str]] = set()

    for raw in raw_bindings:
        if not isinstance(raw, Mapping):
            raise ValueError("Некорректная campaign selector запись Event registry")
        server = str(raw.get("server") or "").strip().upper()
        selector = str(raw.get("selector") or "").strip()
        event_id = str(raw.get("event_id") or "").strip()
        if not server or not selector or not event_id:
            raise ValueError("Campaign selector Event registry содержит пустое поле")
        if not selector.startswith("event_"):
            raise ValueError(f"Некорректный campaign selector: {selector!r}")

        key = (server, selector)
        if key in keys:
            raise ValueError(
                f"Event registry дублирует campaign selector {server}:{selector}"
            )
        keys.add(key)

        targets = [item for item in entry_list if str(item.get("id") or "") == event_id]
        if len(targets) != 1:
            raise ValueError(
                f"Campaign selector {server}:{selector} не разрешается в один Event artifact"
            )
        target = targets[0]
        if str(target.get("server") or "").upper() != server:
            raise ValueError(
                f"Campaign selector {server}:{selector} указывает на Event artifact другого сервера"
            )
        if str(target.get("role") or "production") != "production":
            raise ValueError(
                f"Campaign selector {server}:{selector} может указывать только на production artifact"
            )

        result.append({
            "server": server,
            "selector": selector,
            "event_id": event_id,
        })

    return sorted(
        result,
        key=lambda item: (item["server"], item["selector"], item["event_id"]),
    )


def build_registry(
    root: Path | str = BUILTIN_ARTIFACT_ROOT,
    *,
    campaign_selectors: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    base = Path(root).resolve()
    entries = []
    for path in sorted(base.rglob("*.json")):
        if path.name in {EVENT_REGISTRY_NAME, "assets.json"}:
            continue
        try:
            artifact = load_artifact(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Некорректный Event artifact {path}") from exc
        entries.append(_entry(path, base, artifact))
    entries = sorted(entries, key=lambda item: (item["id"], item["path"]))
    result = {
        "registry_schema_version": EVENT_REGISTRY_SCHEMA_VERSION,
        "artifacts": entries,
        "campaign_selectors": _normalize_campaign_selectors(
            campaign_selectors,
            entries,
        ),
    }
    result["digest"] = registry_digest(result)
    return result


def write_registry(
    root: Path | str = BUILTIN_ARTIFACT_ROOT,
    *,
    campaign_selector: Mapping[str, Any] | None = None,
    retired_event_id: str | None = None,
) -> Path:
    if campaign_selector is not None and retired_event_id is not None:
        raise ValueError(
            "Нельзя одновременно добавлять selector binding и выводить Event из эксплуатации"
        )

    base = Path(root)
    target = base / EVENT_REGISTRY_NAME
    campaign_selectors: list[dict[str, Any]] = []
    if target.exists():
        existing = validate_registry(
            json.loads(target.read_text(encoding="utf-8"))
        )
        campaign_selectors = [
            dict(item) for item in existing["campaign_selectors"]
        ]

    if retired_event_id is not None:
        normalized_event_id = str(retired_event_id or "").strip()
        if not normalized_event_id:
            raise ValueError("Event identity для retirement не задана")
        campaign_selectors = [
            item
            for item in campaign_selectors
            if str(item.get("event_id") or "").strip() != normalized_event_id
        ]

    if campaign_selector is not None:
        server = str(campaign_selector.get("server") or "").strip().upper()
        selector = str(campaign_selector.get("selector") or "").strip()
        campaign_selectors = [
            item
            for item in campaign_selectors
            if (
                str(item.get("server") or "").strip().upper(),
                str(item.get("selector") or "").strip(),
            ) != (server, selector)
        ]
        campaign_selectors.append(dict(campaign_selector))

    data = build_registry(base, campaign_selectors=campaign_selectors)
    target.parent.mkdir(parents=True, exist_ok=True)
    from deploy.atomic import file_remove, file_write, replace_tmp, to_tmp_file

    temp = to_tmp_file(str(target))
    try:
        file_write(
            temp,
            json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        replace_tmp(temp, str(target))
    except BaseException:
        try:
            file_remove(temp)
        except OSError:
            pass
        raise
    return target


def validate_registry(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError("Event registry должен быть JSON object")
    result = dict(data)
    if int(result.get("registry_schema_version", 0) or 0) != EVENT_REGISTRY_SCHEMA_VERSION:
        raise ValueError(
            "Неподдерживаемая версия Event registry. Удалите только generated index.json "
            "и повторите штатную Event-сборку; artifacts и static assets удалять не нужно."
        )
    if str(result.get("digest") or "") != registry_digest(result):
        raise ValueError("Digest Event registry не совпадает")
    if not isinstance(result.get("artifacts"), list):
        raise ValueError("Event registry не содержит artifacts")
    if any(not isinstance(item, Mapping) for item in result["artifacts"]):
        raise ValueError("Event registry содержит некорректную запись artifacts")
    if not isinstance(result.get("campaign_selectors"), list):
        raise ValueError("Event registry не содержит campaign_selectors")
    result["campaign_selectors"] = _normalize_campaign_selectors(
        result["campaign_selectors"],
        result["artifacts"],
    )
    return result


def _parse_time(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"Некорректный lifecycle Event artifact: {value}") from exc


def artifact_lifecycle(entry: Mapping[str, Any], now: datetime) -> str:
    if entry.get("role") == "demo":
        return "demo"
    current = server_local_wall_time(now, str(entry.get("server") or ""))
    start = _parse_time(entry.get("farm_start"))
    farm_end = _parse_time(entry.get("farm_end"))
    shop_end = _parse_time(entry.get("shop_end") or entry.get("farm_end"))
    if current < start:
        return "upcoming"
    if current <= farm_end:
        return "active"
    if current <= shop_end:
        return "redemption"
    return "expired"


class EventArtifactRegistry:
    def __init__(self, root: Path | str = BUILTIN_ARTIFACT_ROOT) -> None:
        self.root = Path(root).resolve()
        data = validate_registry(
            json.loads((self.root / EVENT_REGISTRY_NAME).read_text(encoding="utf-8"))
        )
        self.entries: tuple[dict[str, Any], ...] = tuple(
            self._validate_entry(item) for item in data["artifacts"]
        )
        identities = [item["id"] for item in self.entries]
        if len(identities) != len(set(identities)):
            raise ValueError("Event registry содержит duplicate event identity")
        self.campaign_selectors: tuple[dict[str, str], ...] = tuple(
            dict(item) for item in data["campaign_selectors"]
        )
        self._campaign_selector_index = {
            (item["server"], item["selector"]): item["event_id"]
            for item in self.campaign_selectors
        }

    def _validate_entry(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValueError("Некорректная запись Event registry")
        entry = dict(raw)
        relative = Path(str(entry.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Путь Event artifact вышел за пределы registry")
        target = (self.root / relative).resolve()
        if self.root not in target.parents:
            raise ValueError("Путь Event artifact вышел за пределы registry")
        artifact = load_artifact(target)
        spec = artifact["event_spec"]
        provenance = spec.get("provenance", {})
        expected = {
            "role": str(artifact.get("role") or "production"),
            "id": str(spec.get("id") or ""),
            "server": str(spec.get("server") or "").upper(),
            "source_status": str(spec.get("source_status") or "unsupported"),
            "farm_start": str(spec.get("farm_start") or ""),
            "farm_end": str(spec.get("farm_end") or ""),
            "shop_end": str(spec.get("shop_end") or ""),
            "revision": str(provenance.get("revision") or ""),
            "artifact_digest": str(artifact.get("digest") or ""),
        }
        for key, value in expected.items():
            if entry.get(key) != value:
                raise ValueError(f"Event registry entry не совпадает с artifact: {key}")
        entry["artifact"] = artifact
        return entry

    def list(self, server: str | None = None) -> tuple[dict[str, Any], ...]:
        if server is None:
            return self.entries
        normalized = server.upper()
        return tuple(item for item in self.entries if item["server"] == normalized)

    def get(self, event_id: str) -> dict[str, Any]:
        matches = [item for item in self.entries if item["id"] == event_id]
        if len(matches) != 1:
            raise KeyError(event_id)
        return matches[0]["artifact"]

    def resolve_campaign_selector(
        self,
        server: str,
        selector: str,
    ) -> dict[str, Any] | None:
        key = (
            str(server or "").strip().upper(),
            str(selector or "").strip(),
        )
        event_id = self._campaign_selector_index.get(key)
        if event_id is None:
            return None
        return self.get(event_id)

    def list_active(
        self, server: str, now: datetime
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            item
            for item in self.list(server)
            if item["role"] == "production"
            and artifact_lifecycle(item, now) in {"active", "redemption"}
        )

    def resolve_current(
        self,
        server: str,
        now: datetime,
        *,
        supplemental: bool = True,
    ) -> dict[str, Any] | None:
        entries = self.list(server)
        for phase in ("active", "redemption"):
            matches = [
                item
                for item in entries
                if item["role"] == "production"
                and artifact_lifecycle(item, now) == phase
            ]
            if len(matches) == 1:
                artifact = matches[0]["artifact"]
                if not supplemental:
                    return artifact
                from module.event_datamine.supplemental import (
                    resolve_supplemental_artifact,
                )

                resolved, _ = resolve_supplemental_artifact(artifact)
                return resolved
            if len(matches) > 1:
                raise EventDiscoveryError(
                    "ambiguous_active_event",
                    f"Event registry содержит несколько {phase} events для {server}",
                    candidates=[item["id"] for item in matches],
                )
        return None


@lru_cache(maxsize=8)
def _validated_registry_snapshot(
    root: str,
    index_revision: str,
) -> EventArtifactRegistry:
    """Переиспользовать уже проверенный snapshot только для точной ревизии index."""

    del index_revision
    return EventArtifactRegistry(root)


def load_event_artifact_registry(
    root: Path | str = BUILTIN_ARTIFACT_ROOT,
) -> EventArtifactRegistry:
    """Загрузить проверенный registry с invalidation по содержимому ``index.json``.

    Сам index остаётся дешёвым source-of-truth поколения. Пока он побайтно тот же,
    runtime использует уже валидированный immutable snapshot и не перечитывает все
    artifacts. После атомарной регенерации index его SHA-256 меняется и создаётся
    новый полностью проверенный snapshot.
    """

    base = Path(root).resolve()
    index_path = base / EVENT_REGISTRY_NAME
    index_revision = sha256(index_path.read_bytes()).hexdigest()
    return _validated_registry_snapshot(str(base), index_revision)
