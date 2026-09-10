"""Typed renderers, не зависящие от конкретного транспорта."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from module.application.errors import NotificationValidationError
from module.application.notifications.models import (
    ChannelCapabilities,
    HandoverPreemptionPayload,
    NotificationEvent,
    RenderedSnapshot,
)


class NotificationRenderer(Protocol):
    renderer_id: str
    renderer_version: str

    def render(
        self,
        event: NotificationEvent,
        *,
        locale: str,
        capabilities: ChannelCapabilities,
        presentation_profile: str = "default",
    ) -> RenderedSnapshot: ...


class HandoverPreemptionRenderer:
    renderer_id = "handover.preemption"
    renderer_version = "v1"

    def render(
        self,
        event: NotificationEvent,
        *,
        locale: str,
        capabilities: ChannelCapabilities,
        presentation_profile: str = "default",
    ) -> RenderedSnapshot:
        if locale not in {"ru-RU", "en-US"}:
            raise NotificationValidationError("renderer_locale_unsupported")
        if presentation_profile != "default":
            raise NotificationValidationError("renderer_presentation_profile_unsupported")
        payload = event.data
        if not isinstance(payload, HandoverPreemptionPayload):
            raise NotificationValidationError("renderer_payload_type_invalid")
        if locale == "en-US":
            title = "Profile handover requested"
            body = f"Operation {payload.operation_id} requests profile handover; reason={payload.reason_code}."
        else:
            title = "Запрошена передача профиля"
            body = f"Операция {payload.operation_id} запросила передачу профиля; причина: {payload.reason_code}."
        if payload.current_task is not None:
            task_reference = f"{payload.current_task.kind}/{payload.current_task.id}"
            body += (
                f" Current task: {task_reference}."
                if locale == "en-US"
                else f" Текущая задача: {task_reference}."
            )
        snapshot = RenderedSnapshot(
            locale=locale,
            renderer_id=self.renderer_id,
            renderer_version=self.renderer_version,
            title=title,
            body=body,
        )
        if not snapshot.is_valid(
            max_title=capabilities.max_title_length,
            max_body=capabilities.max_body_length,
            max_payload_bytes=capabilities.max_payload_bytes,
        ):
            raise NotificationValidationError("rendered_snapshot_invalid")
        return snapshot


class NotificationRendererCatalog:
    def __init__(self, renderers: Iterable[NotificationRenderer] = ()) -> None:
        self._renderers: dict[str, NotificationRenderer] = {}
        for renderer in renderers:
            self.register(renderer)

    def register(self, renderer: NotificationRenderer) -> None:
        if not isinstance(renderer.renderer_id, str) or not renderer.renderer_id:
            raise ValueError("Renderer id должен быть непустым.")
        if not isinstance(renderer.renderer_version, str) or not renderer.renderer_version:
            raise ValueError("Renderer version должен быть непустым.")
        if not callable(getattr(renderer, "render", None)):
            raise TypeError("Renderer должен предоставлять callable render.")
        if renderer.renderer_id in self._renderers:
            raise ValueError("Renderer id уже зарегистрирован.")
        self._renderers[renderer.renderer_id] = renderer

    def get(self, renderer_id: str) -> NotificationRenderer | None:
        return self._renderers.get(renderer_id)

    def require(self, renderer_id: str) -> NotificationRenderer:
        renderer = self.get(renderer_id)
        if renderer is None:
            raise NotificationValidationError("renderer_not_registered")
        return renderer


def build_default_renderer_catalog() -> NotificationRendererCatalog:
    return NotificationRendererCatalog((HandoverPreemptionRenderer(),))


__all__ = [
    "HandoverPreemptionRenderer",
    "NotificationRenderer",
    "NotificationRendererCatalog",
    "build_default_renderer_catalog",
]
