"""Каноническое обнаружение конфигурационных профилей AzurPilot."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MAX_PROFILE_CONFIG_BYTES = 1024 * 1024
MAX_PROFILE_CONFIG_CANDIDATES = 512
SUPPORTED_MOD_PROFILE_ROOTS = {
    "fpy": "Fpy",
    "maa": "Maa",
}
_INVALID_PROFILE_NAME_CHARS = frozenset(".\\/:*?\"'<>|")


class InvalidProfileConfigError(ValueError):
    """Загруженный документ не соответствует контракту профиля."""


class ProfileDiscoveryError(ValueError):
    """Небезопасное состояние filesystem discovery в строгом режиме."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProfileIdentity:
    name: str
    mod_name: str = "alas"


@dataclass(frozen=True)
class ProfileConfig:
    identity: ProfileIdentity
    path: Path

    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def mod_name(self) -> str:
        return self.identity.mod_name


def profile_identity_from_filename(filename: str) -> ProfileIdentity | None:
    """Распознать допустимое имя обычного или mod-профиля."""
    if not isinstance(filename, str) or not filename.casefold().endswith(".json"):
        return None
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        return None

    stem = filename[:-5]
    config_name, mod_suffix = os.path.splitext(stem)
    if not mod_suffix:
        config_name = stem
        mod_name = "alas"
    else:
        mod_name = mod_suffix[1:].casefold()
        if mod_name not in SUPPORTED_MOD_PROFILE_ROOTS:
            return None

    if (
        not config_name
        or config_name.casefold().startswith("template")
        or any(char in _INVALID_PROFILE_NAME_CHARS for char in config_name)
    ):
        return None
    return ProfileIdentity(config_name, mod_name)


def _has_scheduler_group(data: Mapping[str, object]) -> bool:
    return any(
        isinstance(group, Mapping) and isinstance(group.get("Scheduler"), Mapping)
        for group in data.values()
    )


def is_profile_payload(data: object, mod_name: str = "alas") -> bool:
    """Проверить structural contract обычного или поддерживаемого mod-профиля."""
    if not isinstance(data, Mapping):
        return False

    if mod_name == "alas":
        alas = data.get("Alas")
        general = data.get("General")
        return (
            isinstance(alas, Mapping)
            and isinstance(alas.get("Emulator"), Mapping)
            and isinstance(general, Mapping)
            and _has_scheduler_group(data)
        )

    root_name = SUPPORTED_MOD_PROFILE_ROOTS.get(mod_name)
    root = data.get(root_name) if root_name else None
    return (
        isinstance(root, Mapping)
        and isinstance(root.get("Emulator"), Mapping)
        and _has_scheduler_group(data)
    )


def parse_profile_config_bytes(
    content: bytes, filename: str
) -> tuple[ProfileIdentity, dict[str, object]]:
    """Разобрать upload и отклонить non-profile JSON до записи на диск."""
    identity = profile_identity_from_filename(filename)
    if identity is None or len(content) > MAX_PROFILE_CONFIG_BYTES:
        raise InvalidProfileConfigError("PROFILE_CONFIG_INVALID")
    try:
        data = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InvalidProfileConfigError("PROFILE_CONFIG_INVALID") from exc
    if not isinstance(data, dict) or not is_profile_payload(data, identity.mod_name):
        raise InvalidProfileConfigError("PROFILE_CONFIG_INVALID")
    return identity, data


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _safe_profile_path(path: Path, config_dir: Path) -> Path:
    try:
        if _is_link(config_dir) or _is_link(path):
            raise OSError
        resolved_config = config_dir.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if resolved.parent != resolved_config or not resolved.is_file():
            raise OSError
        if resolved.stat().st_size > MAX_PROFILE_CONFIG_BYTES:
            raise OSError
        return resolved
    except (OSError, RuntimeError) as exc:
        raise ProfileDiscoveryError("PROFILE_CONFIG_UNSAFE") from exc


def classify_profile_config(
    path: Path | str,
    config_dir: Path | str,
    *,
    strict: bool = False,
) -> ProfileConfig | None:
    """Классифицировать один root-level JSON без materialization и side effects."""
    candidate = Path(path)
    root = Path(config_dir)
    identity = profile_identity_from_filename(candidate.name)
    if identity is None:
        return None
    try:
        resolved = _safe_profile_path(candidate, root)
    except ProfileDiscoveryError:
        if strict:
            raise
        return None
    try:
        content = resolved.read_bytes()
        parse_profile_config_bytes(content, candidate.name)
    except OSError as exc:
        if strict:
            raise ProfileDiscoveryError("PROFILE_CONFIG_UNSAFE") from exc
        return None
    except InvalidProfileConfigError:
        return None
    return ProfileConfig(identity=identity, path=resolved)


def discover_profile_configs(
    config_dir: Path | str = Path("config"),
    *,
    strict: bool = False,
) -> tuple[ProfileConfig, ...]:
    """Вернуть реальные root-level профили в детерминированном порядке."""
    root = Path(config_dir)
    try:
        if _is_link(root) or not root.is_dir():
            if strict and root.exists():
                raise ProfileDiscoveryError("PROFILE_CONFIG_UNSAFE")
            return ()
        candidates = sorted(root.glob("*.json"), key=lambda item: item.name.casefold())
    except OSError as exc:
        if strict:
            raise ProfileDiscoveryError("PROFILE_CONFIG_UNSAFE") from exc
        return ()

    if len(candidates) > MAX_PROFILE_CONFIG_CANDIDATES:
        if strict:
            raise ProfileDiscoveryError("PROFILE_CONFIG_COUNT_EXCEEDED")
        return ()

    profiles = []
    for candidate in candidates:
        profile = classify_profile_config(candidate, root, strict=strict)
        if profile is not None:
            profiles.append(profile)
    return tuple(profiles)


def discover_profile_names(
    config_dir: Path | str = Path("config"),
    *,
    strict: bool = False,
) -> list[str]:
    """Вернуть имена реальных профилей без legacy default fallback."""
    return [profile.name for profile in discover_profile_configs(config_dir, strict=strict)]
