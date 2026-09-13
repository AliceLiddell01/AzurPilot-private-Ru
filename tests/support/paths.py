"""Стабильные пути тестового пространства без зависимости от глубины файла."""

from __future__ import annotations

from tools.paths import REPOSITORY_ROOT

TESTS_ROOT = REPOSITORY_ROOT / "tests"
FIXTURES_ROOT = TESTS_ROOT / "fixtures"

__all__ = ["FIXTURES_ROOT", "REPOSITORY_ROOT", "TESTS_ROOT"]
