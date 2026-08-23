from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dev_tools import postgresql_runtime
from module.persistence.config import DatabaseSettings


def _settings() -> DatabaseSettings:
    return DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user="azurpilot_app",
        sslmode="disable",
    )


def test_backup_rejects_repository_target(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(RuntimeError, match="вне репозитория"):
        postgresql_runtime._backup(
            _settings(),
            repository / "backup.dump",
            "Archlinux",
            repository,
        )


def test_backup_is_verified_and_published_create_only(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    output = tmp_path / "backups" / "production.dump"
    calls: list[list[str]] = []

    def run_hidden(arguments: list[str], *, stdout: object = None) -> None:
        calls.append(arguments)
        if hasattr(stdout, "write"):
            stdout.write(b"x" * 2048)

    with (
        patch.object(postgresql_runtime.shutil, "which", side_effect=["pg_dump", "pg_restore"]),
        patch.object(postgresql_runtime, "_run_hidden", side_effect=run_hidden),
    ):
        postgresql_runtime._backup(
            _settings(), output, "Archlinux", repository
        )

    assert output.stat().st_size == 2048
    assert calls[0][0] == "pg_dump"
    assert calls[1][:2] == ["pg_restore", "--list"]
    assert not tuple(output.parent.glob("*.tmp"))

    with pytest.raises(RuntimeError, match="уже существует"):
        postgresql_runtime._backup(
            _settings(), output, "Archlinux", repository
        )
