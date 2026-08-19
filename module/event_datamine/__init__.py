"""Безопасная компиляция игровых данных события в локальный EventSpec."""

from module.event_datamine.compiler import EventCompiler
from module.event_datamine.discovery import (
    EventCandidate,
    EventDiscoveryError,
    discover_major_events,
    resolve_current_candidate,
)
from module.event_datamine.model import EventSpec, ValidationFinding
from module.event_datamine.registry import EventArtifactRegistry
from module.event_datamine.source import ShareCfgLoader, SourceSnapshot

__all__ = [
    "EventCompiler",
    "EventCandidate",
    "EventDiscoveryError",
    "EventSpec",
    "EventArtifactRegistry",
    "ShareCfgLoader",
    "SourceSnapshot",
    "ValidationFinding",
    "discover_major_events",
    "resolve_current_candidate",
]
