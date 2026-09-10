"""Нейтральный contract channel adapters и их bounded registry."""

from __future__ import annotations

from collections.abc import Iterable
from re import fullmatch
from typing import Final, Protocol, runtime_checkable

from module.application.notifications.models import (
    ChannelCapabilities,
    DeliveryResult,
    PreparedDelivery,
)

STAGE2_CHANNEL_TYPES: Final[frozenset[str]] = frozenset({"test"})
SUPPORTED_CHANNEL_TYPES: Final[frozenset[str]] = frozenset(
    {*STAGE2_CHANNEL_TYPES, "desktop-agent"}
)


@runtime_checkable
class NotificationChannel(Protocol):
    """Внешний adapter получает только уже подготовленный channel-safe DTO."""

    @property
    def instance_id(self) -> str: ...

    @property
    def channel_type(self) -> str: ...

    @property
    def capabilities(self) -> ChannelCapabilities: ...

    def send(self, prepared: PreparedDelivery) -> DeliveryResult:
        """Адаптер обязан соблюдать bounded timeout из PreparedDelivery."""
        ...


class NotificationChannelCatalog:
    """Явный in-process registry для проверенных channel adapters."""

    def __init__(self, channels: Iterable[NotificationChannel] = ()) -> None:
        self._channels: dict[str, NotificationChannel] = {}
        for channel in channels:
            self.register(channel)

    def register(self, channel: NotificationChannel) -> None:
        try:
            send = getattr(channel, "send", None)
            instance_id = channel.instance_id
            channel_type = channel.channel_type
            capabilities = channel.capabilities
        except Exception:  # noqa: BLE001 - adapter contract переводится в typed error.
            raise TypeError("Channel должен предоставлять атрибуты contract.") from None
        if not callable(send):
            raise TypeError("Channel должен предоставлять callable send.")
        if not isinstance(instance_id, str) or fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", instance_id
        ) is None:
            raise ValueError("Channel instance id имеет неверный формат.")
        if instance_id in self._channels:
            raise ValueError("Channel instance id уже зарегистрирован.")
        if not isinstance(channel_type, str) or fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", channel_type
        ) is None:
            raise ValueError("Channel type имеет неверный формат.")
        if channel_type not in SUPPORTED_CHANNEL_TYPES:
            raise ValueError("Channel type не поддерживается текущим контрактом.")
        if not isinstance(capabilities, ChannelCapabilities) or not capabilities.is_valid():
            raise ValueError("Channel capabilities не прошли bounded validation.")
        self._channels[instance_id] = channel

    def get(self, instance_id: str) -> NotificationChannel | None:
        return self._channels.get(instance_id)

    def require(self, instance_id: str) -> NotificationChannel:
        channel = self.get(instance_id)
        if channel is None:
            raise LookupError("Notification channel instance не зарегистрирован.")
        return channel

    def __len__(self) -> int:
        return len(self._channels)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._channels)


__all__ = [
    "STAGE2_CHANNEL_TYPES",
    "SUPPORTED_CHANNEL_TYPES",
    "NotificationChannel",
    "NotificationChannelCatalog",
]
