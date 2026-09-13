"""Изоляция repository environment для observability-тестов."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_repository_environment(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Не даёт локальному корневому .env влиять на unit-тесты telemetry."""

    module_name = request.module.__name__.rsplit(".", 1)[-1]
    if module_name not in {
        "test_observability_logging",
        "test_observability_metrics",
        "test_observability_tracing",
    }:
        return

    repository_root = tmp_path / "observability-repository"
    (repository_root / "module").mkdir(parents=True)
    (repository_root / "gui.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("AZURPILOT_REPOSITORY_ROOT", str(repository_root))
