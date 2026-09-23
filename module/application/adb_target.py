"""Общие read-only правила подтверждения ADB target и MuMu aliases."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from threading import Lock
from time import monotonic
from typing import Literal

AdbTargetResolutionReason = Literal["not_found", "ambiguous", "unavailable"]


class AdbTargetResolutionError(ValueError):
    """Не удалось однозначно подтвердить ownership ADB target."""

    def __init__(self, message: str, *, reason: AdbTargetResolutionReason) -> None:
        super().__init__(message)
        self.reason = reason


def safe_serial(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("serial должен быть строкой")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(char.isspace() or ord(char) < 32 for char in normalized)
    ):
        raise ValueError("serial содержит недопустимое значение")
    return normalized


def read_only_emulator_serial_aliases(target_serial: str) -> tuple[str, ...]:
    """Найти aliases настроенного эмулятора без Device и lifecycle recovery."""

    try:
        from module.device.platform.emulator_windows import EmulatorManager
    except (ImportError, OSError):
        return ()

    try:
        instances = tuple(EmulatorManager().all_emulator_instances)
    except (AttributeError, OSError, TypeError, ValueError):
        return ()

    matches: list[tuple[str, ...]] = []
    for instance in instances:
        try:
            aliases = getattr(instance, "adb_serials", ())
        except (AttributeError, OSError, TypeError, ValueError):
            continue
        if isinstance(aliases, (str, bytes)) or not isinstance(aliases, Sequence):
            continue
        normalized = tuple(
            alias for alias in aliases if isinstance(alias, str) and alias
        )
        if target_serial in normalized:
            matches.append(normalized)

    if len(matches) != 1:
        return ()
    return matches[0]


READ_ONLY_EMULATOR_ALIASES_CACHE_TTL_SECONDS = 1.0
READ_ONLY_EMULATOR_ALIASES_CACHE_MAX_ENTRIES = 32
_read_only_emulator_aliases_cache: dict[
    tuple[int, str], tuple[float, tuple[object, ...]]
] = {}
_read_only_emulator_aliases_cache_lock = Lock()


def cached_read_only_emulator_serial_aliases(
    target_serial: str,
    *,
    provider: Callable[[str], object] = read_only_emulator_serial_aliases,
) -> object:
    """Вернуть bounded TTL-кэш read-only aliases для заданного provider."""

    cache_key = (id(provider), target_serial)
    now = monotonic()
    with _read_only_emulator_aliases_cache_lock:
        cached = _read_only_emulator_aliases_cache.get(cache_key)
        if (
            cached is not None
            and now - cached[0] < READ_ONLY_EMULATOR_ALIASES_CACHE_TTL_SECONDS
        ):
            return cached[1]

    aliases = provider(target_serial)
    if isinstance(aliases, (str, bytes)) or not isinstance(aliases, Sequence):
        return aliases

    snapshot = tuple(aliases)
    with _read_only_emulator_aliases_cache_lock:
        if (
            cache_key not in _read_only_emulator_aliases_cache
            and len(_read_only_emulator_aliases_cache)
            >= READ_ONLY_EMULATOR_ALIASES_CACHE_MAX_ENTRIES
        ):
            oldest_key = min(
                _read_only_emulator_aliases_cache,
                key=lambda item: _read_only_emulator_aliases_cache[item][0],
            )
            del _read_only_emulator_aliases_cache[oldest_key]
        _read_only_emulator_aliases_cache[cache_key] = (monotonic(), snapshot)
    return snapshot


def resolve_adb_target_serial(
    configured_serial: str | None,
    inventory_serials: Sequence[str],
    *,
    aliases_provider: Callable[[str], Sequence[str]] = read_only_emulator_serial_aliases,
) -> str:
    """Однозначно сопоставить configured serial с ADB inventory.

    Configured serial остаётся пользовательской identity. Alias допускается
    только как repository-owned доказательство того же emulator instance.
    """

    if isinstance(inventory_serials, (str, bytes)):
        raise AdbTargetResolutionError(
            "ADB inventory имеет неподтверждённый формат.",
            reason="unavailable",
        )
    try:
        normalized_inventory = tuple(safe_serial(serial) for serial in inventory_serials)
    except (TypeError, ValueError) as exc:
        raise AdbTargetResolutionError(
            "ADB inventory имеет неподтверждённый serial.",
            reason="unavailable",
        ) from exc

    if configured_serial is None:
        if len(normalized_inventory) == 1:
            return normalized_inventory[0]
        raise AdbTargetResolutionError(
            "ADB target невозможно подтвердить без configured serial.",
            reason="not_found" if not normalized_inventory else "ambiguous",
        )

    try:
        normalized_configured = safe_serial(configured_serial)
    except (TypeError, ValueError) as exc:
        raise AdbTargetResolutionError(
            "Configured ADB target имеет неподтверждённый serial.",
            reason="unavailable",
        ) from exc

    exact_matches = tuple(
        serial for serial in normalized_inventory if serial == normalized_configured
    )
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise AdbTargetResolutionError(
            "Configured ADB target совпал с несколькими устройствами.",
            reason="ambiguous",
        )

    try:
        aliases = aliases_provider(normalized_configured)
    except Exception as exc:
        raise AdbTargetResolutionError(
            "Repository-owned ADB alias evidence недоступно.",
            reason="unavailable",
        ) from exc
    if isinstance(aliases, (str, bytes)) or not isinstance(aliases, Sequence):
        raise AdbTargetResolutionError(
            "Repository-owned ADB alias evidence имеет неподтверждённый формат.",
            reason="unavailable",
        )
    safe_aliases: set[str] = set()
    for alias in aliases:
        try:
            safe_aliases.add(safe_serial(alias))
        except (TypeError, ValueError):
            continue

    alias_matches = tuple(
        serial for serial in normalized_inventory if serial in safe_aliases
    )
    if len(alias_matches) == 1:
        return alias_matches[0]
    raise AdbTargetResolutionError(
        "Configured ADB target не подтверждён однозначным alias mapping.",
        reason="not_found" if not alias_matches else "ambiguous",
    )


__all__ = [
    "READ_ONLY_EMULATOR_ALIASES_CACHE_MAX_ENTRIES",
    "READ_ONLY_EMULATOR_ALIASES_CACHE_TTL_SECONDS",
    "AdbTargetResolutionError",
    "AdbTargetResolutionReason",
    "cached_read_only_emulator_serial_aliases",
    "read_only_emulator_serial_aliases",
    "resolve_adb_target_serial",
    "safe_serial",
]
