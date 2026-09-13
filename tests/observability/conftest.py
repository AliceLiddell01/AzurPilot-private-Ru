"""Изоляция repository environment для observability-тестов."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolate_repository_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Не даёт локальному корневому .env влиять на unit-тесты telemetry."""

    repository_root = tmp_path / "observability-repository"
    (repository_root / "module").mkdir(parents=True)
    (repository_root / "gui.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("AZURPILOT_REPOSITORY_ROOT", str(repository_root))
