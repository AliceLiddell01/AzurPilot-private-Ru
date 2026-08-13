"""Детерминированный registry generated Event artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from module.event_datamine.artifact import (
    BUILTIN_ARTIFACT_ROOT,
    canonical_json,
    load_artifact,
)
from module.event_datamine.discovery import EventDiscoveryError

EVENT_REGISTRY_SCHEMA_VERSION = 1
EVENT_REGISTRY_NAME = "index.json"


def registry_digest(data: Mapping[str, Any]) -> str:
    clean = dict(data)
    clean.pop("digest", None)
    from hashlib import sha256

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


def build_registry(root: Path | str = BUILTIN_ARTIFACT_ROOT) -> dict[str, Any]:
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
    result = {
        "registry_schema_version": EVENT_REGISTRY_SCHEMA_VERSION,
        "artifacts": sorted(entries, key=lambda item: (item["id"], item["path"])),
    }
    result["digest"] = registry_digest(result)
    return result


def write_registry(
    root: Path | str = BUILTIN_ARTIFACT_ROOT,
) -> Path:
    base = Path(root)
    data = build_registry(base)
    # Reuse the artifact writer's atomic primitives through a temporary valid
    # envelope, then replace it with the registry JSON deterministically.
    target = base / EVENT_REGISTRY_NAME
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
        raise ValueError("Неподдерживаемая версия Event registry")
    if str(result.get("digest") or "") != registry_digest(result):
        raise ValueError("Digest Event registry не совпадает")
    if not isinstance(result.get("artifacts"), list):
        raise ValueError("Event registry не содержит artifacts")
    return result


def _parse_time(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"Некорректный lifecycle Event artifact: {value}") from exc


def artifact_lifecycle(entry: Mapping[str, Any], now: datetime) -> str:
    if entry.get("role") == "demo":
        return "demo"
    current = now.replace(tzinfo=None) if now.tzinfo is not None else now
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

    def list_active(
        self, server: str, now: datetime
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            item
            for item in self.list(server)
            if item["role"] == "production"
            and artifact_lifecycle(item, now) in {"active", "redemption"}
        )

    def resolve_current(self, server: str, now: datetime) -> dict[str, Any] | None:
        entries = self.list(server)
        for phase in ("active", "redemption"):
            matches = [
                item
                for item in entries
                if item["role"] == "production"
                and artifact_lifecycle(item, now) == phase
            ]
            if len(matches) == 1:
                return matches[0]["artifact"]
            if len(matches) > 1:
                raise EventDiscoveryError(
                    "ambiguous_active_event",
                    f"Event registry содержит несколько {phase} events для {server}",
                    candidates=[item["id"] for item in matches],
                )
        return None
