"""Dependency-light configuration constants shared by low-level modules."""

from datetime import datetime

DEFAULT_TIME = datetime.fromisoformat("2023-01-01 00:00:00")

__all__ = ["DEFAULT_TIME"]
