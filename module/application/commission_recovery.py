"""Состояние восстановления награды Commission при переполнении нефти.

Модуль содержит только прикладной контракт состояния и его адаптацию к общему
``RuntimeCache``. Игровой код передаёт сюда подтверждённое OCR-наблюдение, а
WebUI получает ту же проекцию без прямого доступа к Redis.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from typing import Any

from module.application.runtime_cache import (
    RuntimeCache,
    RuntimeCacheError,
    RuntimeCacheStatus,
    get_runtime_cache,
)
from module.config.profile import profile_identity_from_name
from module.config.time_source import now as current_time

EN_SERVER_TIMEZONE = timedelta(hours=-7)
COMMISSION_RECOVERY_SCHEMA_VERSION = 1
COMMISSION_RECOVERY_PREFIX = "commission/recovery/"
MAX_WEEKLY_ACTION_POINT_PURCHASES = 5
ACTION_POINT_GAIN_PER_PURCHASE = 100

_SOURCE_VALUES = frozenset(
    {
        "game_ocr",
        "emergency_ap_purchase",
        "dorm_fallback",
    }
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def next_en_weekly_reset(value: datetime) -> datetime:
    """Вернуть следующий понедельник 00:00 по времени EN-сервера в UTC.

    Global/EN использует фиксированный offset ``UTC-7``. Важен именно момент
    сброса на сервере, а не ISO-неделя или локальная полночь хоста.
    """

    now_utc = _as_utc(value)
    server_now = now_utc + EN_SERVER_TIMEZONE
    days_until_monday = (7 - server_now.weekday()) % 7
    candidate_date = server_now.date() + timedelta(days=days_until_monday)
    candidate_server = datetime.combine(candidate_date, time.min, tzinfo=UTC)
    if candidate_server <= server_now:
        candidate_server += timedelta(days=7)
    return (candidate_server - EN_SERVER_TIMEZONE).astimezone(UTC)


def _serialize_datetime(value: datetime | None) -> str | None:
    return None if value is None else _as_utc(value).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Дата состояния Commission имеет неверный тип.")
    parsed = datetime.fromisoformat(value)
    return _as_utc(parsed)


def _profile_name(profile: str) -> str:
    identity = profile_identity_from_name(profile)
    if identity is None:
        raise ValueError("Имя профиля не соответствует каноническому контракту.")
    return identity.name


def _cache_status_value(status: RuntimeCacheStatus | str) -> str:
    return status.value if isinstance(status, RuntimeCacheStatus) else str(status)


def _purchase_cost(remaining: int | None) -> int | None:
    if remaining is None or remaining <= 0:
        return None
    # Таблица стоимости остаётся у владельца игровой механики; импорт ленивый,
    # поэтому прикладной слой не загружает UI/OCR при обычном импорте WebUI.
    try:
        from module.os_handler.action_point import ACTION_POINTS_BUY

        return ACTION_POINTS_BUY.get(remaining)
    except (ImportError, AttributeError):
        return None


@dataclass(frozen=True, slots=True)
class CommissionRecoveryState:
    """Безопасная проекция состояния недельной покупки AP только для чтения."""

    profile: str
    status: str
    remaining: int | None
    used: int | None
    next_oil_cost: int | None
    next_ap_gain: int | None
    confirmed_at: datetime | None
    reset_at: datetime
    source: str | None
    last_result: str | None
    cache_status: str
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        reset_at_local = self.reset_at.astimezone().isoformat(timespec="seconds")
        return {
            "profile": self.profile,
            "status": self.status,
            "remaining": self.remaining,
            "used": self.used,
            "next_oil_cost": self.next_oil_cost,
            "next_ap_gain": self.next_ap_gain,
            "confirmed_at": _serialize_datetime(self.confirmed_at),
            "reset_at": _serialize_datetime(self.reset_at),
            "reset_at_local": reset_at_local,
            "source": self.source,
            "last_result": self.last_result,
            "cache_status": self.cache_status,
            "error": self.error,
        }


class CommissionRecoveryStore:
    """Профильное состояние Commission поверх кэша владельца приложения."""

    def __init__(
        self,
        cache: RuntimeCache | None,
        *,
        now: Callable[[], datetime] | None = None,
        cache_status: RuntimeCacheStatus | str = RuntimeCacheStatus.READY,
    ) -> None:
        self._cache = cache
        self._now = now or (lambda: current_time(UTC))
        self._cache_status = _cache_status_value(cache_status)

    @classmethod
    def from_environment(
        cls,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> CommissionRecoveryStore:
        """Собрать store через установленную composition root cache boundary."""

        try:
            return cls(get_runtime_cache(), now=now)
        except RuntimeCacheError as error:
            return cls(None, now=now, cache_status=error.status)

    @staticmethod
    def key(profile: str) -> str:
        """Построить безопасный профильный ключ внутри ``azurpilot:``."""

        name = _profile_name(profile)
        # Имена профилей допускают пробелы, а Redis adapter намеренно их
        # отклоняет. Процентное кодирование сохраняет изоляцию профилей.
        from urllib.parse import quote

        encoded = quote(name, safe="-._~")
        key = f"{COMMISSION_RECOVERY_PREFIX}{encoded}"
        if len(key) > 256:
            raise ValueError("Ключ состояния Commission слишком длинный.")
        return key

    def _now_utc(self) -> datetime:
        return _as_utc(self._now())

    def _unavailable(
        self,
        profile: str,
        now: datetime,
        *,
        error: str | None = None,
        observed_remaining: int | None = None,
        source: str | None = None,
        last_result: str | None = None,
    ) -> CommissionRecoveryState:
        return CommissionRecoveryState(
            profile=profile,
            status="unavailable",
            remaining=observed_remaining,
            used=(MAX_WEEKLY_ACTION_POINT_PURCHASES - observed_remaining)
            if observed_remaining is not None
            else None,
            next_oil_cost=_purchase_cost(observed_remaining),
            next_ap_gain=(ACTION_POINT_GAIN_PER_PURCHASE if observed_remaining else None),
            confirmed_at=None,
            reset_at=next_en_weekly_reset(now),
            source=source,
            last_result=last_result,
            cache_status=self._cache_status,
            error=error,
        )

    def _unknown(
        self,
        profile: str,
        now: datetime,
        *,
        last_result: str | None = None,
    ) -> CommissionRecoveryState:
        return CommissionRecoveryState(
            profile=profile,
            status="unknown",
            remaining=None,
            used=None,
            next_oil_cost=None,
            next_ap_gain=None,
            confirmed_at=None,
            reset_at=next_en_weekly_reset(now),
            source=None,
            last_result=last_result,
            cache_status=self._cache_status,
        )

    def _health(self, profile: str, now: datetime) -> CommissionRecoveryState | None:
        if self._cache is None:
            return self._unavailable(profile, now, error=self._cache_status)
        try:
            health = self._cache.health()
        except RuntimeCacheError as error:
            self._cache_status = error.status.value
            return self._unavailable(profile, now, error=error.status.value)
        if not health.available:
            self._cache_status = health.status.value
            return self._unavailable(profile, now, error=health.status.value)
        self._cache_status = health.status.value
        return None

    @staticmethod
    def _decode(profile: str, raw: bytes) -> CommissionRecoveryState:
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("Состояние Commission не является объектом.")
        if document.get("schema_version") != COMMISSION_RECOVERY_SCHEMA_VERSION:
            raise ValueError("Версия состояния Commission не поддерживается.")
        if document.get("profile") != profile or document.get("status") != "confirmed":
            raise ValueError("Профиль или статус состояния Commission не совпадает.")
        remaining = document.get("remaining")
        used = document.get("used")
        next_oil_cost = document.get("next_oil_cost")
        next_ap_gain = document.get("next_ap_gain")
        expected_cost = _purchase_cost(remaining) if isinstance(remaining, int) else None
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, int)
            or not 0 <= remaining <= MAX_WEEKLY_ACTION_POINT_PURCHASES
            or isinstance(used, bool)
            or not isinstance(used, int)
            or used != MAX_WEEKLY_ACTION_POINT_PURCHASES - remaining
            or next_oil_cost != expected_cost
            or (remaining > 0 and next_ap_gain != ACTION_POINT_GAIN_PER_PURCHASE)
            or (remaining == 0 and next_ap_gain is not None)
        ):
            raise ValueError("Числа состояния Commission не прошли проверку.")
        confirmed_at = _parse_datetime(document.get("confirmed_at"))
        reset_at = _parse_datetime(document.get("reset_at"))
        if confirmed_at is None or reset_at is None:
            raise ValueError("Состояние Commission не содержит даты подтверждения.")
        source = document.get("source")
        if source not in _SOURCE_VALUES:
            raise ValueError("Источник состояния Commission не поддерживается.")
        last_result = document.get("last_result")
        if last_result is not None and not isinstance(last_result, str):
            raise ValueError("Результат восстановления Commission имеет неверный тип.")
        return CommissionRecoveryState(
            profile=profile,
            status="confirmed",
            remaining=remaining,
            used=used,
            next_oil_cost=next_oil_cost,
            next_ap_gain=next_ap_gain,
            confirmed_at=confirmed_at,
            reset_at=reset_at,
            source=source,
            last_result=last_result,
            cache_status=RuntimeCacheStatus.READY.value,
        )

    @staticmethod
    def _encode(state: CommissionRecoveryState) -> bytes:
        document = {
            "schema_version": COMMISSION_RECOVERY_SCHEMA_VERSION,
            "profile": state.profile,
            "status": "confirmed",
            "remaining": state.remaining,
            "used": state.used,
            "next_oil_cost": state.next_oil_cost,
            "next_ap_gain": state.next_ap_gain,
            "confirmed_at": _serialize_datetime(state.confirmed_at),
            "reset_at": _serialize_datetime(state.reset_at),
            "source": state.source,
            "last_result": state.last_result,
        }
        return json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")

    def read(self, profile: str) -> CommissionRecoveryState:
        profile = _profile_name(profile)
        now = self._now_utc()
        unavailable = self._health(profile, now)
        if unavailable is not None:
            return unavailable
        assert self._cache is not None
        try:
            raw = self._cache.get(self.key(profile))
        except RuntimeCacheError as error:
            self._cache_status = error.status.value
            return self._unavailable(profile, now, error=error.status.value)
        if raw is None:
            return self._unknown(profile, now)
        try:
            state = self._decode(profile, raw)
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            self._cache_status = RuntimeCacheStatus.INVALID_DATA.value
            return self._unavailable(profile, now, error=RuntimeCacheStatus.INVALID_DATA.value)
        if state.reset_at <= now:
            return self._unknown(profile, now)
        return replace(state, cache_status=self._cache_status)

    def record_observation(
        self,
        profile: str,
        remaining: int,
        *,
        source: str = "game_ocr",
        last_result: str | None = None,
    ) -> CommissionRecoveryState:
        """Записать только подтверждённое OCR-наблюдение из игры."""

        profile = _profile_name(profile)
        if isinstance(remaining, bool) or not 0 <= remaining <= MAX_WEEKLY_ACTION_POINT_PURCHASES:
            raise ValueError("Остаток покупок AP вне диапазона 0..5.")
        if source not in _SOURCE_VALUES:
            raise ValueError("Источник наблюдения Commission не поддерживается.")
        now = self._now_utc()
        reset_at = next_en_weekly_reset(now)
        observed = CommissionRecoveryState(
            profile=profile,
            status="confirmed",
            remaining=remaining,
            used=MAX_WEEKLY_ACTION_POINT_PURCHASES - remaining,
            next_oil_cost=_purchase_cost(remaining),
            next_ap_gain=ACTION_POINT_GAIN_PER_PURCHASE if remaining else None,
            confirmed_at=now,
            reset_at=reset_at,
            source=source,
            last_result=last_result,
            cache_status=self._cache_status,
        )
        unavailable = self._health(profile, now)
        if unavailable is not None:
            return replace(
                unavailable,
                remaining=remaining,
                used=MAX_WEEKLY_ACTION_POINT_PURCHASES - remaining,
                next_oil_cost=_purchase_cost(remaining),
                next_ap_gain=ACTION_POINT_GAIN_PER_PURCHASE if remaining else None,
                source=source,
                last_result=last_result,
            )
        assert self._cache is not None
        try:
            self._cache.set(
                self.key(profile),
                self._encode(observed),
                expires_at=reset_at,
            )
        except RuntimeCacheError as error:
            self._cache_status = error.status.value
            return replace(
                self._unavailable(
                    profile,
                    now,
                    error=error.status.value,
                    observed_remaining=remaining,
                    source=source,
                    last_result=last_result,
                ),
                reset_at=reset_at,
            )
        return replace(observed, cache_status=self._cache_status)

    def invalidate(
        self,
        profile: str,
        *,
        last_result: str | None = "ambiguous_ap_purchase",
    ) -> CommissionRecoveryState:
        """Удалить подтверждённое состояние после неоднозначной AP mutation."""

        if last_result is not None and (
            not last_result or any(character.isspace() for character in last_result)
        ):
            raise ValueError("Результат восстановления Commission некорректен.")
        profile = _profile_name(profile)
        now = self._now_utc()
        unavailable = self._health(profile, now)
        if unavailable is not None:
            return unavailable
        assert self._cache is not None
        try:
            self._cache.delete(self.key(profile))
        except RuntimeCacheError as error:
            self._cache_status = error.status.value
            return self._unavailable(
                profile,
                now,
                error=error.status.value,
                last_result=last_result,
            )
        return self._unknown(profile, now, last_result=last_result)

    def record_result(
        self,
        profile: str,
        result: str,
    ) -> CommissionRecoveryState:
        """Обновить операторский результат только для подтверждённого state."""

        if not result or any(character.isspace() for character in result):
            raise ValueError("Результат восстановления Commission некорректен.")
        profile = _profile_name(profile)
        current = self.read(profile)
        if current.status != "confirmed":
            return current
        now = self._now_utc()
        unavailable = self._health(profile, now)
        if unavailable is not None:
            return unavailable
        assert self._cache is not None
        updated = replace(current, last_result=result, cache_status=self._cache_status)
        try:
            self._cache.set(
                self.key(profile),
                self._encode(updated),
                expires_at=current.reset_at,
            )
        except RuntimeCacheError as error:
            self._cache_status = error.status.value
            return self._unavailable(
                profile,
                now,
                error=error.status.value,
                observed_remaining=current.remaining,
                source=current.source,
                last_result=result,
            )
        return replace(updated, cache_status=self._cache_status)

    def close(self) -> None:
        if self._cache is not None:
            self._cache.close()

    def __enter__(self) -> CommissionRecoveryStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = (
    "ACTION_POINT_GAIN_PER_PURCHASE",
    "COMMISSION_RECOVERY_PREFIX",
    "COMMISSION_RECOVERY_SCHEMA_VERSION",
    "CommissionRecoveryState",
    "CommissionRecoveryStore",
    "EN_SERVER_TIMEZONE",
    "MAX_WEEKLY_ACTION_POINT_PURCHASES",
    "next_en_weekly_reset",
)
