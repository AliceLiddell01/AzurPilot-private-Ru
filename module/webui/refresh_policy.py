"""Централизованные интервалы read-only обновления WebUI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebUIRefreshPolicy:
    fleet_page_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.fleet_page_seconds <= 0:
            raise ValueError("fleet_page_seconds должен быть положительным")


WEBUI_REFRESH_POLICY = WebUIRefreshPolicy()


__all__ = ("WEBUI_REFRESH_POLICY", "WebUIRefreshPolicy")
