"""Проверяемая runtime-policy для generated Event-карт.

Policy лежит рядом с generated package и содержит только наблюдаемые runtime-факты,
которых нет в ShareCfg. Production-код знает только схему и разрешённые типы
поведения, а не identity конкретного события.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from module.event_datamine.artifact import canonical_json

RUNTIME_POLICY_SCHEMA_VERSION = 3
GENERATED_EVENT_ROOT = Path(__file__).resolve().parents[2] / "campaign" / "generated_event"
_ALLOWED_UI_LAYOUTS = frozenset({"legacy", "20241219", "20260326"})
_ALLOWED_BOSS_CLEAR_STRATEGIES = frozenset({"campaign", "boss_fleet", "fleet_1"})
_SAFE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SAFE_TEMPLATE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_ALLOWED_TOP_LEVEL = {
    "runtime_policy_schema_version",
    "generated_package",
    "event_id",
    "campaign_ui",
    "evidence",
    "map_evidence",
    "runtime_maps",
    "digest",
}
_ALLOWED_CAMPAIGN_UI = {"layout"}
_ALLOWED_EVIDENCE = {"archive_sha256", "kind", "note", "observed_at"}
_ALLOWED_MAP_EVIDENCE = {"repository", "revision"}
_ALLOWED_MAP = {
    "map_id",
    "chapter_name",
    "source_path",
    "siren_recognition",
    "stage_entry",
    "boss_clear",
}
_ALLOWED_SIREN = {"templates", "boss_icon_small"}
_ALLOWED_STAGE_ENTRY = {"one_time", "has_mode_switch"}
_ALLOWED_BOSS_CLEAR = {"strategy"}


class EventRuntimePolicyError(ValueError):
    """Runtime-policy generated Event-карт не прошла проверку."""


@dataclass(frozen=True)
class SirenRecognitionPolicy:
    templates: tuple[str, ...]
    boss_icon_small: bool


@dataclass(frozen=True)
class StageEntryPolicy:
    one_time: bool | None = None
    has_mode_switch: bool | None = None


@dataclass(frozen=True)
class BossClearPolicy:
    strategy: str


@dataclass(frozen=True)
class MapRuntimePolicy:
    map_id: int
    chapter_name: str
    source_path: str
    siren_recognition: SirenRecognitionPolicy | None = None
    stage_entry: StageEntryPolicy | None = None
    boss_clear: BossClearPolicy | None = None

    def config_items(self) -> tuple[tuple[str, Any], ...]:
        """Преобразовать семантическую policy в ограниченный набор runtime-настроек."""

        result: list[tuple[str, Any]] = []
        if self.siren_recognition is not None:
            result.append(("MAP_SIREN_TEMPLATE", list(self.siren_recognition.templates)))
            if self.siren_recognition.boss_icon_small:
                result.append(("MAP_SIREN_HAS_BOSS_ICON_SMALL", True))
        if self.stage_entry is not None:
            if self.stage_entry.one_time is not None:
                result.append(("MAP_IS_ONE_TIME_STAGE", self.stage_entry.one_time))
            if self.stage_entry.has_mode_switch is not None:
                result.append(("MAP_HAS_MODE_SWITCH", self.stage_entry.has_mode_switch))
        return tuple(result)


def runtime_policy_digest(data: Mapping[str, Any]) -> str:
    clean = dict(data)
    clean.pop("digest", None)
    return sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def _package_path(parts: tuple[str, ...], root: Path | str) -> Path:
    if not parts or any(not _SAFE_PART.fullmatch(part) for part in parts):
        raise EventRuntimePolicyError("Некорректный generated campaign package")
    base = Path(root).resolve()
    target = base.joinpath(*parts, "runtime.json").resolve()
    if base not in target.parents:
        raise EventRuntimePolicyError("Runtime-policy вышла за пределы generated_event")
    return target


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise EventRuntimePolicyError(
            f"{label} содержит неизвестные поля: {sorted(unknown)}"
        )


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise EventRuntimePolicyError(f"{label} должен быть bool")
    return value


def _safe_source_path(value: Any) -> str:
    source_path = str(value or "").strip()
    path = PurePosixPath(source_path)
    if (
        not source_path
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix != ".py"
    ):
        raise EventRuntimePolicyError(
            f"Некорректный source_path runtime evidence: {source_path!r}"
        )
    return source_path


def _parse_siren_policy(raw: Any, *, map_id: int) -> SirenRecognitionPolicy:
    if not isinstance(raw, Mapping):
        raise EventRuntimePolicyError(
            f"siren_recognition карты {map_id} должна быть JSON object"
        )
    _reject_unknown(raw, _ALLOWED_SIREN, f"siren_recognition карты {map_id}")
    templates = raw.get("templates")
    if not isinstance(templates, list):
        raise EventRuntimePolicyError(
            f"siren_recognition карты {map_id} требует список templates"
        )
    normalized: list[str] = []
    for value in templates:
        name = str(value or "").strip()
        if not _SAFE_TEMPLATE.fullmatch(name):
            raise EventRuntimePolicyError(
                f"Карта {map_id} содержит некорректное имя siren template: {name!r}"
            )
        if name in normalized:
            raise EventRuntimePolicyError(
                f"Карта {map_id} содержит дублирующий siren template: {name!r}"
            )
        normalized.append(name)
    boss_icon_small = _strict_bool(
        raw.get("boss_icon_small", False),
        f"siren_recognition.boss_icon_small карты {map_id}",
    )
    if not normalized and not boss_icon_small:
        raise EventRuntimePolicyError(
            f"Карта {map_id} не содержит доказанного способа распознавания siren"
        )
    return SirenRecognitionPolicy(tuple(normalized), boss_icon_small)


def _parse_stage_entry(raw: Any, *, map_id: int) -> StageEntryPolicy:
    if not isinstance(raw, Mapping):
        raise EventRuntimePolicyError(
            f"stage_entry карты {map_id} должна быть JSON object"
        )
    _reject_unknown(raw, _ALLOWED_STAGE_ENTRY, f"stage_entry карты {map_id}")
    if not raw:
        raise EventRuntimePolicyError(
            f"stage_entry карты {map_id} не должна быть пустой"
        )
    one_time = (
        _strict_bool(raw["one_time"], f"stage_entry.one_time карты {map_id}")
        if "one_time" in raw
        else None
    )
    has_mode_switch = (
        _strict_bool(
            raw["has_mode_switch"],
            f"stage_entry.has_mode_switch карты {map_id}",
        )
        if "has_mode_switch" in raw
        else None
    )
    return StageEntryPolicy(one_time=one_time, has_mode_switch=has_mode_switch)


def _parse_boss_clear(raw: Any, *, map_id: int) -> BossClearPolicy:
    if not isinstance(raw, Mapping):
        raise EventRuntimePolicyError(
            f"boss_clear карты {map_id} должна быть JSON object"
        )
    _reject_unknown(raw, _ALLOWED_BOSS_CLEAR, f"boss_clear карты {map_id}")
    strategy = str(raw.get("strategy") or "").strip()
    if strategy not in _ALLOWED_BOSS_CLEAR_STRATEGIES:
        raise EventRuntimePolicyError(
            f"Карта {map_id} содержит неподдерживаемую boss strategy: {strategy!r}"
        )
    return BossClearPolicy(strategy=strategy)


def runtime_map_policies(data: Mapping[str, Any]) -> dict[int, MapRuntimePolicy]:
    raw_maps = data.get("runtime_maps", [])
    if not isinstance(raw_maps, list):
        raise EventRuntimePolicyError("runtime_maps должен быть списком")
    result: dict[int, MapRuntimePolicy] = {}
    for raw in raw_maps:
        if not isinstance(raw, Mapping):
            raise EventRuntimePolicyError(
                "runtime map policy должна быть JSON object"
            )
        _reject_unknown(raw, _ALLOWED_MAP, "runtime map policy")
        map_id = raw.get("map_id")
        if isinstance(map_id, bool) or not isinstance(map_id, int) or map_id <= 0:
            raise EventRuntimePolicyError(
                f"Некорректный runtime map_id: {map_id!r}"
            )
        if map_id in result:
            raise EventRuntimePolicyError(
                f"Runtime-policy дублирует карту {map_id}"
            )
        chapter_name = str(raw.get("chapter_name") or "").strip()
        if not chapter_name:
            raise EventRuntimePolicyError(
                f"Runtime-policy карты {map_id} не содержит chapter_name"
            )
        siren = (
            _parse_siren_policy(raw["siren_recognition"], map_id=map_id)
            if "siren_recognition" in raw
            else None
        )
        stage_entry = (
            _parse_stage_entry(raw["stage_entry"], map_id=map_id)
            if "stage_entry" in raw
            else None
        )
        boss_clear = (
            _parse_boss_clear(raw["boss_clear"], map_id=map_id)
            if "boss_clear" in raw
            else None
        )
        if siren is None and stage_entry is None and boss_clear is None:
            raise EventRuntimePolicyError(
                f"Runtime-policy карты {map_id} не содержит поддерживаемых runtime-фактов"
            )
        result[map_id] = MapRuntimePolicy(
            map_id=map_id,
            chapter_name=chapter_name,
            source_path=_safe_source_path(raw.get("source_path")),
            siren_recognition=siren,
            stage_entry=stage_entry,
            boss_clear=boss_clear,
        )
    return result


def validate_runtime_policy(data: Any, *, package: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise EventRuntimePolicyError("Runtime-policy должна быть JSON object")
    result = dict(data)
    _reject_unknown(result, _ALLOWED_TOP_LEVEL, "Runtime-policy")
    version = result.get("runtime_policy_schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise EventRuntimePolicyError(
            "runtime_policy_schema_version должен быть int"
        )
    if version != RUNTIME_POLICY_SCHEMA_VERSION:
        raise EventRuntimePolicyError(
            "Неподдерживаемая версия runtime-policy"
        )
    if str(result.get("generated_package") or "") != package:
        raise EventRuntimePolicyError(
            "Runtime-policy не соответствует generated package"
        )
    event_id = str(result.get("event_id") or "").strip()
    if not event_id or ":" not in event_id:
        raise EventRuntimePolicyError(
            "Runtime-policy не содержит Event identity"
        )

    campaign_ui = result.get("campaign_ui")
    if not isinstance(campaign_ui, Mapping):
        raise EventRuntimePolicyError(
            "campaign_ui должна быть JSON object"
        )
    _reject_unknown(campaign_ui, _ALLOWED_CAMPAIGN_UI, "campaign_ui")
    layout = str(campaign_ui.get("layout") or "")
    if layout not in _ALLOWED_UI_LAYOUTS:
        raise EventRuntimePolicyError(
            f"Неподдерживаемый campaign UI layout: {layout!r}"
        )

    evidence = result.get("evidence")
    if not isinstance(evidence, Mapping):
        raise EventRuntimePolicyError(
            "Runtime-policy требует evidence"
        )
    _reject_unknown(evidence, _ALLOWED_EVIDENCE, "Runtime-policy evidence")
    archive_sha256 = str(evidence.get("archive_sha256") or "").lower()
    if not _SHA256.fullmatch(archive_sha256):
        raise EventRuntimePolicyError(
            "Runtime-policy содержит некорректный evidence SHA-256"
        )

    runtime_maps = runtime_map_policies(result)
    map_evidence = result.get("map_evidence")
    if runtime_maps:
        if not isinstance(map_evidence, Mapping):
            raise EventRuntimePolicyError(
                "Runtime-policy карт требует map_evidence"
            )
        _reject_unknown(
            map_evidence,
            _ALLOWED_MAP_EVIDENCE,
            "map_evidence",
        )
        repository = str(map_evidence.get("repository") or "").strip()
        revision = str(map_evidence.get("revision") or "").lower()
        if not repository or "/" not in repository:
            raise EventRuntimePolicyError(
                "map_evidence требует repository"
            )
        if not _SHA40.fullmatch(revision):
            raise EventRuntimePolicyError(
                "map_evidence содержит некорректный Git SHA"
            )
    elif map_evidence is not None:
        raise EventRuntimePolicyError(
            "map_evidence не должен существовать без runtime_maps"
        )

    expected = str(result.get("digest") or "")
    if expected != runtime_policy_digest(result):
        raise EventRuntimePolicyError(
            "Digest runtime-policy не совпадает"
        )
    return result


def map_runtime_policy(
    data: Mapping[str, Any] | None,
    *,
    map_id: int,
    chapter_name: str,
) -> MapRuntimePolicy | None:
    if data is None:
        return None
    item = runtime_map_policies(data).get(map_id)
    if item is None:
        return None
    if item.chapter_name.casefold() != str(chapter_name or "").strip().casefold():
        raise EventRuntimePolicyError(
            f"Runtime-policy карты {map_id} относится к chapter "
            f"{item.chapter_name!r}, а ShareCfg содержит {chapter_name!r}"
        )
    return item


def validate_runtime_template_assets(
    policy: MapRuntimePolicy,
    *,
    server: str,
    asset_root: Path | str,
) -> None:
    """Проверить наличие локальных CV-шаблонов, на которые ссылается policy."""

    siren = policy.siren_recognition
    if siren is None:
        return
    root = Path(asset_root).resolve() / str(server).lower() / "template"
    for name in siren.templates:
        matches = [
            root / f"TEMPLATE_SIREN_{name}{suffix}"
            for suffix in (".gif", ".png")
        ]
        if not any(path.is_file() for path in matches):
            raise EventRuntimePolicyError(
                f"Для карты {policy.map_id} отсутствует runtime "
                f"siren template {name!r}"
            )


def load_generated_runtime_policy(
    package_parts: tuple[str, ...],
    *,
    root: Path | str = GENERATED_EVENT_ROOT,
) -> dict[str, Any] | None:
    """Загрузить policy для generated package без сетевых запросов."""

    target = _package_path(package_parts, root)
    if not target.is_file():
        return None
    package = ".".join(package_parts)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EventRuntimePolicyError(
            f"Не удалось прочитать runtime-policy {target}"
        ) from exc
    return validate_runtime_policy(data, package=package)
