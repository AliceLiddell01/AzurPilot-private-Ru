"""Versioned persistent cache for completed Game Settings audits."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol

from deploy.atomic import atomic_read_text, atomic_write
from module.config.server import GLOBAL_PACKAGE
from module.game_settings.model import (
    FrameRateValue,
    GameSettingResult,
    GameSettingState,
    GameSettingsScanResult,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
)
from module.game_settings.registry import (
    GAME_SETTINGS_OPTIONS_REGISTRY,
    GameSettingCheckSpec,
    build_game_settings_registry,
)
from module.logger import logger


GAME_SETTINGS_SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH = Path("config/game_settings_snapshot.json")
_FINGERPRINT_CONTRACT_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class GameSettingsSnapshotSource(Enum):
    AUDIT = "audit"
    ENFORCEMENT_FINAL_AUDIT = "enforcement_final_audit"


class GameSettingsSnapshotStatus(Enum):
    VALID = "valid"
    MISSING = "missing"
    CORRUPT = "corrupt"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    REQUIREMENTS_CHANGED = "requirements_changed"
    SCOPE_MISMATCH = "scope_mismatch"
    INCOMPLETE = "incomplete"
    STALE = "stale"


class GameSettingsSnapshotAccessSource(Enum):
    SNAPSHOT = "snapshot"
    LIVE_AUDIT = "live_audit"


@dataclass(frozen=True, slots=True)
class GameSettingsEnvironmentScope:
    server: str
    package_name: str
    resolution: tuple[int, int]
    ui_profile: str

    def __post_init__(self) -> None:
        for name, value in (
            ("server", self.server),
            ("package_name", self.package_name),
            ("ui_profile", self.ui_profile),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} должен быть непустой строкой")
        if (
            not isinstance(self.resolution, tuple)
            or len(self.resolution) != 2
            or any(type(value) is not int or value <= 0 for value in self.resolution)
        ):
            raise ValueError("resolution должен быть парой положительных int")


CURRENT_GAME_SETTINGS_SCOPE = GameSettingsEnvironmentScope(
    server="en",
    package_name=GLOBAL_PACKAGE,
    resolution=(1280, 720),
    ui_profile="current",
)


@dataclass(frozen=True, slots=True)
class GameSettingsSnapshot:
    schema_version: int
    scanned_at: datetime
    source: GameSettingsSnapshotSource
    scope: GameSettingsEnvironmentScope
    requirements_fingerprint: str
    scan_result: GameSettingsScanResult

    def __post_init__(self) -> None:
        if self.schema_version != GAME_SETTINGS_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("Неподдерживаемая schema snapshot")
        if not isinstance(self.scanned_at, datetime):
            raise TypeError("scanned_at должен быть datetime")
        if self.scanned_at.tzinfo is None or self.scanned_at.utcoffset() is None:
            raise ValueError("scanned_at должен содержать timezone")
        if not isinstance(self.source, GameSettingsSnapshotSource):
            raise TypeError("source должен быть GameSettingsSnapshotSource")
        if not isinstance(self.scope, GameSettingsEnvironmentScope):
            raise TypeError("scope должен быть GameSettingsEnvironmentScope")
        if not _SHA256_RE.fullmatch(self.requirements_fingerprint):
            raise ValueError("requirements_fingerprint должен быть SHA-256 hex")
        if not isinstance(self.scan_result, GameSettingsScanResult):
            raise TypeError("scan_result должен быть GameSettingsScanResult")

    @property
    def all_required_compatible(self) -> bool | None:
        return self.scan_result.all_required_compatible

    def satisfies(self, keys: Iterable[str]) -> bool:
        return all(
            (check := self.scan_result.get(key)) is not None
            and check.compatible is True
            for key in keys
        )


@dataclass(frozen=True, slots=True)
class GameSettingsSnapshotLoadResult:
    status: GameSettingsSnapshotStatus
    path: Path
    snapshot: GameSettingsSnapshot | None = None
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.status is GameSettingsSnapshotStatus.VALID and self.snapshot is not None


@dataclass(frozen=True, slots=True)
class GameSettingsSnapshotAccessResult:
    snapshot: GameSettingsSnapshot
    source: GameSettingsSnapshotAccessSource
    cache_status: GameSettingsSnapshotStatus
    cache_reason: str | None = None


class _SnapshotScanner(Protocol):
    game_settings_snapshot_path: Path | str

    def scan_game_settings(self) -> GameSettingsScanResult: ...


class GameSettingsSnapshotError(RuntimeError):
    pass


class _ValidationError(ValueError):
    def __init__(self, status: GameSettingsSnapshotStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _registry(
    registry: Iterable[GameSettingCheckSpec] = GAME_SETTINGS_OPTIONS_REGISTRY,
) -> tuple[GameSettingCheckSpec, ...]:
    return build_game_settings_registry(registry)


def _family(entry: GameSettingCheckSpec) -> str:
    mapping = {
        GameSettingState: "toggle",
        FrameRateValue: "frame_rate",
        StoryAutoplayValue: "story_autoplay",
        TextAutoScrollSpeedValue: "text_auto_scroll_speed",
    }
    try:
        return mapping[entry.value_type]
    except KeyError as exc:
        raise TypeError(
            f"Неподдерживаемая value family: {entry.value_type.__name__}"
        ) from exc


def game_settings_requirements_fingerprint(
    registry: Iterable[GameSettingCheckSpec] = GAME_SETTINGS_OPTIONS_REGISTRY,
) -> str:
    entries = _registry(registry)
    canonical = {
        "contract_version": _FINGERPRINT_CONTRACT_VERSION,
        "requirements": [
            {
                "key": entry.key,
                "location": entry.definition.location,
                "kind": entry.requirement.kind.value if entry.requirement else None,
                "value_family": _family(entry),
                "required": (
                    entry.requirement.expected_value.value
                    if entry.requirement is not None
                    else None
                ),
            }
            for entry in entries
        ],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_result(
    result: GameSettingsScanResult,
    registry: Iterable[GameSettingCheckSpec] = GAME_SETTINGS_OPTIONS_REGISTRY,
) -> tuple[GameSettingCheckSpec, ...]:
    entries = _registry(registry)
    if tuple(check.key for check in result) != tuple(entry.key for entry in entries):
        raise ValueError("Snapshot result должен содержать exact current registry")
    for entry, check in zip(entries, result, strict=True):
        requirement = entry.requirement
        if check.definition != entry.definition:
            raise ValueError(f"Definition mismatch: {entry.key}")
        if type(check.detected_value) is not entry.value_type:
            raise TypeError(f"Value family mismatch: {entry.key}")
        if requirement is None or check.required_value is None:
            raise ValueError(f"Missing production requirement: {entry.key}")
        if check.required_value is not requirement.expected_value:
            raise ValueError(f"Requirement mismatch: {entry.key}")
    return entries


def is_current_game_settings_scan_result(result: GameSettingsScanResult) -> bool:
    try:
        _validate_result(result)
    except (TypeError, ValueError):
        return False
    return True


def create_game_settings_snapshot(
    result: GameSettingsScanResult,
    *,
    source: GameSettingsSnapshotSource = GameSettingsSnapshotSource.AUDIT,
    scanned_at: datetime | None = None,
    scope: GameSettingsEnvironmentScope = CURRENT_GAME_SETTINGS_SCOPE,
    registry: Iterable[GameSettingCheckSpec] = GAME_SETTINGS_OPTIONS_REGISTRY,
) -> GameSettingsSnapshot:
    entries = _validate_result(result, registry)
    when = datetime.now(timezone.utc) if scanned_at is None else scanned_at
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("scanned_at должен содержать timezone")
    return GameSettingsSnapshot(
        schema_version=GAME_SETTINGS_SNAPSHOT_SCHEMA_VERSION,
        scanned_at=when,
        source=source,
        scope=scope,
        requirements_fingerprint=game_settings_requirements_fingerprint(entries),
        scan_result=result,
    )


def _document(
    snapshot: GameSettingsSnapshot,
    registry: Iterable[GameSettingCheckSpec] = GAME_SETTINGS_OPTIONS_REGISTRY,
) -> dict[str, object]:
    entries = _validate_result(snapshot.scan_result, registry)
    if snapshot.requirements_fingerprint != game_settings_requirements_fingerprint(entries):
        raise ValueError("Fingerprint не соответствует registry")
    settings = []
    for entry, check in zip(entries, snapshot.scan_result, strict=True):
        settings.append(
            {
                "key": entry.key,
                "location": entry.definition.location,
                "kind": check.kind.value,
                "value_family": _family(entry),
                "detected": check.detected_value.value,
                "required": check.required_value.value,
            }
        )
    return {
        "schema_version": snapshot.schema_version,
        "scanned_at": snapshot.scanned_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "source": snapshot.source.value,
        "scope": {
            "server": snapshot.scope.server,
            "package_name": snapshot.scope.package_name,
            "resolution": list(snapshot.scope.resolution),
            "ui_profile": snapshot.scope.ui_profile,
        },
        "requirements_fingerprint": snapshot.requirements_fingerprint,
        "settings": settings,
    }


def serialize_game_settings_snapshot(
    snapshot: GameSettingsSnapshot,
    *,
    registry: Iterable[GameSettingCheckSpec] = GAME_SETTINGS_OPTIONS_REGISTRY,
) -> str:
    return json.dumps(
        _document(snapshot, registry),
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def save_game_settings_snapshot(
    result: GameSettingsScanResult,
    *,
    path: Path | str = DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
    source: GameSettingsSnapshotSource = GameSettingsSnapshotSource.AUDIT,
    scanned_at: datetime | None = None,
    scope: GameSettingsEnvironmentScope = CURRENT_GAME_SETTINGS_SCOPE,
    registry: Iterable[GameSettingCheckSpec] = GAME_SETTINGS_OPTIONS_REGISTRY,
) -> GameSettingsSnapshot:
    target = Path(path)
    snapshot = create_game_settings_snapshot(
        result,
        source=source,
        scanned_at=scanned_at,
        scope=scope,
        registry=registry,
    )
    atomic_write(
        str(target),
        serialize_game_settings_snapshot(snapshot, registry=registry),
    )
    logger.info("[Снимок игровых настроек] Сохранён: %s", target)
    return snapshot


def invalidate_game_settings_snapshot(
    path: Path | str = DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
) -> None:
    target = Path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        return
    logger.info("[Снимок игровых настроек] Инвалидирован: %s", target)


def _exact(data: dict[str, object], keys: set[str], context: str) -> None:
    if set(data) != keys:
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            f"{context}: неожиданный набор полей",
        )


def _timestamp(raw: object) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "Некорректный scanned_at",
        )
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "Некорректный scanned_at",
        ) from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "scanned_at не содержит timezone",
        )
    return value


def _scope(raw: object) -> GameSettingsEnvironmentScope:
    if not isinstance(raw, dict):
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "scope должен быть JSON object",
        )
    _exact(raw, {"server", "package_name", "resolution", "ui_profile"}, "scope")
    resolution = raw["resolution"]
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(type(value) is not int or value <= 0 for value in resolution)
    ):
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "Некорректное resolution",
        )
    try:
        return GameSettingsEnvironmentScope(
            server=raw["server"],
            package_name=raw["package_name"],
            resolution=(resolution[0], resolution[1]),
            ui_profile=raw["ui_profile"],
        )
    except (TypeError, ValueError) as exc:
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            f"Некорректный scope: {exc}",
        ) from exc


def _source(raw: object) -> GameSettingsSnapshotSource:
    try:
        return GameSettingsSnapshotSource(raw)
    except (TypeError, ValueError) as exc:
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "Неизвестный source",
        ) from exc


def _settings(
    raw: object,
    entries: tuple[GameSettingCheckSpec, ...],
) -> GameSettingsScanResult:
    if not isinstance(raw, list):
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "settings должен быть JSON array",
        )
    by_key = {entry.key: entry for entry in entries}
    parsed: dict[str, GameSettingResult] = {}
    required_fields = {
        "key",
        "location",
        "kind",
        "value_family",
        "detected",
        "required",
    }
    for item in raw:
        if not isinstance(item, dict):
            raise _ValidationError(
                GameSettingsSnapshotStatus.CORRUPT,
                "Элемент settings должен быть JSON object",
            )
        _exact(item, required_fields, "setting")
        key = item["key"]
        if not isinstance(key, str) or not key:
            raise _ValidationError(
                GameSettingsSnapshotStatus.CORRUPT,
                "Некорректный key настройки",
            )
        if key in parsed:
            raise _ValidationError(
                GameSettingsSnapshotStatus.CORRUPT,
                f"Повторяющийся key: {key}",
            )
        entry = by_key.get(key)
        if entry is None:
            raise _ValidationError(
                GameSettingsSnapshotStatus.INCOMPLETE,
                f"Неизвестный лишний key: {key}",
            )
        requirement = entry.requirement
        if requirement is None:
            raise _ValidationError(
                GameSettingsSnapshotStatus.REQUIREMENTS_CHANGED,
                f"Requirement удалён: {key}",
            )
        if item["kind"] != requirement.kind.value or item["value_family"] != _family(entry):
            raise _ValidationError(
                GameSettingsSnapshotStatus.CORRUPT,
                f"Несовпадение value family: {key}",
            )
        if item["location"] != entry.definition.location:
            raise _ValidationError(
                GameSettingsSnapshotStatus.REQUIREMENTS_CHANGED,
                f"Location изменён: {key}",
            )
        if item["required"] != requirement.expected_value.value:
            raise _ValidationError(
                GameSettingsSnapshotStatus.REQUIREMENTS_CHANGED,
                f"Required value изменён: {key}",
            )
        detected = item["detected"]
        if not isinstance(detected, str):
            raise _ValidationError(
                GameSettingsSnapshotStatus.CORRUPT,
                f"Некорректное detected value: {key}",
            )
        try:
            parsed[key] = entry.make_result(entry.value_type(detected))
        except (TypeError, ValueError) as exc:
            raise _ValidationError(
                GameSettingsSnapshotStatus.CORRUPT,
                f"Некорректное типизированное значение: {key}",
            ) from exc
    if set(parsed) != set(by_key):
        raise _ValidationError(
            GameSettingsSnapshotStatus.INCOMPLETE,
            "Отсутствуют current registry keys",
        )
    return GameSettingsScanResult(parsed[entry.key] for entry in entries)


def deserialize_game_settings_snapshot(
    payload: str,
    *,
    registry: Iterable[GameSettingCheckSpec] = GAME_SETTINGS_OPTIONS_REGISTRY,
) -> GameSettingsSnapshot:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "Некорректный JSON",
        ) from exc
    if not isinstance(data, dict):
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "Верхний уровень должен быть JSON object",
        )
    schema = data.get("schema_version")
    if schema != GAME_SETTINGS_SNAPSHOT_SCHEMA_VERSION:
        raise _ValidationError(
            GameSettingsSnapshotStatus.UNSUPPORTED_SCHEMA,
            f"Неподдерживаемая schema: {schema!r}",
        )
    _exact(
        data,
        {
            "schema_version",
            "scanned_at",
            "source",
            "scope",
            "requirements_fingerprint",
            "settings",
        },
        "snapshot",
    )
    fingerprint = data["requirements_fingerprint"]
    if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
        raise _ValidationError(
            GameSettingsSnapshotStatus.CORRUPT,
            "Некорректный requirements fingerprint",
        )
    entries = _registry(registry)
    result = _settings(data["settings"], entries)
    if fingerprint != game_settings_requirements_fingerprint(entries):
        raise _ValidationError(
            GameSettingsSnapshotStatus.REQUIREMENTS_CHANGED,
            "Requirements fingerprint изменился",
        )
    return GameSettingsSnapshot(
        schema_version=schema,
        scanned_at=_timestamp(data["scanned_at"]),
        source=_source(data["source"]),
        scope=_scope(data["scope"]),
        requirements_fingerprint=fingerprint,
        scan_result=result,
    )


def load_game_settings_snapshot(
    *,
    path: Path | str = DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
    expected_scope: GameSettingsEnvironmentScope = CURRENT_GAME_SETTINGS_SCOPE,
    max_age: timedelta | None = None,
    now: datetime | None = None,
    registry: Iterable[GameSettingCheckSpec] = GAME_SETTINGS_OPTIONS_REGISTRY,
) -> GameSettingsSnapshotLoadResult:
    target = Path(path)
    if max_age is not None and max_age < timedelta(0):
        raise ValueError("max_age не может быть отрицательным")
    if not target.exists():
        logger.info("[Снимок игровых настроек] Промах кэша: файл отсутствует")
        return GameSettingsSnapshotLoadResult(
            GameSettingsSnapshotStatus.MISSING,
            target,
            reason="Файл отсутствует",
        )
    try:
        payload = atomic_read_text(str(target), encoding="utf-8", errors="strict")
        if not payload:
            raise _ValidationError(
                GameSettingsSnapshotStatus.CORRUPT,
                "Пустой файл",
            )
        snapshot = deserialize_game_settings_snapshot(payload, registry=registry)
    except _ValidationError as exc:
        logger.warning(
            "[Снимок игровых настроек] Промах кэша: %s (%s): %s",
            exc.status.value,
            target,
            exc.reason,
        )
        return GameSettingsSnapshotLoadResult(exc.status, target, reason=exc.reason)
    except (OSError, UnicodeError) as exc:
        logger.warning(
            "[Снимок игровых настроек] Промах кэша: повреждённый файл (%s): %s",
            target,
            type(exc).__name__,
        )
        return GameSettingsSnapshotLoadResult(
            GameSettingsSnapshotStatus.CORRUPT,
            target,
            reason=f"Ошибка чтения: {type(exc).__name__}",
        )
    if snapshot.scope != expected_scope:
        logger.info("[Снимок игровых настроек] Промах кэша: среда не совпадает")
        return GameSettingsSnapshotLoadResult(
            GameSettingsSnapshotStatus.SCOPE_MISMATCH,
            target,
            snapshot,
            "Environment scope не совпадает",
        )
    if max_age is not None:
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now должен содержать timezone")
        if current - snapshot.scanned_at > max_age:
            logger.info("[Снимок игровых настроек] Промах кэша: снимок устарел")
            return GameSettingsSnapshotLoadResult(
                GameSettingsSnapshotStatus.STALE,
                target,
                snapshot,
                "Снимок старше consumer max_age",
            )
    logger.info("[Снимок игровых настроек] Попадание в кэш")
    return GameSettingsSnapshotLoadResult(
        GameSettingsSnapshotStatus.VALID,
        target,
        snapshot,
    )


def get_game_settings_snapshot(**kwargs) -> GameSettingsSnapshotLoadResult:
    return load_game_settings_snapshot(**kwargs)


def refresh_game_settings_snapshot(
    scanner_factory: Callable[[], _SnapshotScanner],
    *,
    path: Path | str = DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
    expected_scope: GameSettingsEnvironmentScope = CURRENT_GAME_SETTINGS_SCOPE,
) -> GameSettingsSnapshot:
    target = Path(path)
    scanner = scanner_factory()
    scanner.game_settings_snapshot_path = target
    scanner.scan_game_settings()
    loaded = load_game_settings_snapshot(path=target, expected_scope=expected_scope)
    if not loaded.valid or loaded.snapshot is None:
        raise GameSettingsSnapshotError(
            "Полный игровой аудит завершился без валидного снимка: "
            f"{loaded.status.value}: {loaded.reason}"
        )
    logger.info("[Снимок игровых настроек] Обновлён после полного игрового аудита")
    return loaded.snapshot


def get_or_refresh_game_settings_snapshot(
    scanner_factory: Callable[[], _SnapshotScanner],
    *,
    path: Path | str = DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
    expected_scope: GameSettingsEnvironmentScope = CURRENT_GAME_SETTINGS_SCOPE,
    max_age: timedelta | None = None,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> GameSettingsSnapshotAccessResult:
    if not force_refresh:
        cached = load_game_settings_snapshot(
            path=path,
            expected_scope=expected_scope,
            max_age=max_age,
            now=now,
        )
        if cached.valid and cached.snapshot is not None:
            return GameSettingsSnapshotAccessResult(
                cached.snapshot,
                GameSettingsSnapshotAccessSource.SNAPSHOT,
                cached.status,
            )
        status = cached.status
        reason = cached.reason
    else:
        status = GameSettingsSnapshotStatus.VALID
        reason = "Запрошено принудительное обновление"
        logger.info("[Снимок игровых настроек] Запрошено принудительное обновление")
    snapshot = refresh_game_settings_snapshot(
        scanner_factory,
        path=path,
        expected_scope=expected_scope,
    )
    return GameSettingsSnapshotAccessResult(
        snapshot,
        GameSettingsSnapshotAccessSource.LIVE_AUDIT,
        status,
        reason,
    )
