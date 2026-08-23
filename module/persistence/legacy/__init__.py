"""Строго read-only adapters legacy-источников для offline-миграции."""

from module.persistence.legacy.reader import LegacySourceReader
from module.persistence.legacy.snapshot import create_consistent_snapshot

__all__ = ("LegacySourceReader", "create_consistent_snapshot")
