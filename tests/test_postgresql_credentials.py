from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dev_tools import postgresql_credentials


def test_passfile_uses_distinct_least_privilege_roles():
    payload = postgresql_credentials._passfile(
        "azurpilot", "application-value", "migrator-value"
    ).decode()

    assert "azurpilot_app:application-value" in payload
    assert "azurpilot_migrator:migrator-value" in payload
    assert "postgres:" not in payload
    assert "azurpilot_owner:" not in payload


def test_windows_passfile_merge_preserves_unrelated_entries():
    previous = (
        b"remote.example:5432:wikidb:alice:keep\n"
        b"127.0.0.1:5432:azurpilot:azurpilot_app:old\n"
    )

    merged = postgresql_credentials._merge_windows_passfile(
        previous, "azurpilot", "new-app", "new-migrator"
    ).decode()

    assert "remote.example:5432:wikidb:alice:keep" in merged
    assert ":old" not in merged
    assert "azurpilot_app:new-app" in merged
    assert "azurpilot_migrator:new-migrator" in merged


def test_env_document_contains_full_contract_without_pgpassword(tmp_path: Path):
    document = postgresql_credentials._env_document(
        tmp_path,
        tmp_path / "pgpass.conf",
        "/etc/azurpilot/pgpass",
        "archlinux",
        "azurpilot",
        "new-app",
        "new-migrator",
    ).decode()

    assert "AZURPILOT_POSTGRES_PASSWORD=new-app" in document
    assert "AZURPILOT_POSTGRES_MIGRATOR_PASSWORD=new-migrator" in document
    assert "AZURPILOT_POSTGRES_PGPASSFILE=" in document
    assert "AZURPILOT_WSL_PGPASSFILE=/etc/azurpilot/pgpass" in document
    assert "PGPASSWORD=" not in document


def test_sql_secret_escapes_quotes_and_rejects_line_breaks():
    assert postgresql_credentials._sql_secret("value'quoted") == "value''quoted"
    with pytest.raises(RuntimeError):
        postgresql_credentials._sql_secret("bad\nvalue")


def test_verify_backup_rejects_drive_less_path(tmp_path: Path, monkeypatch):
    backup = tmp_path / "production.dump"
    backup.write_bytes(b"x" * 2048)
    resolved = Mock()
    resolved.is_file.return_value = True
    resolved.stat.return_value = SimpleNamespace(st_size=2048)
    resolved.drive = ""
    monkeypatch.setattr(Path, "resolve", lambda *_args, **_kwargs: resolved)

    with pytest.raises(RuntimeError, match="букву диска"):
        postgresql_credentials._verify_backup("archlinux", backup)


def test_negative_auth_accepts_only_password_rejection(monkeypatch):
    completed = subprocess.CompletedProcess(
        [], 2, stderr=b'password authentication failed for user "azurpilot_app"'
    )
    run = Mock(return_value=completed)
    monkeypatch.setattr(postgresql_credentials, "_run", run)

    postgresql_credentials._auth(
        "archlinux",
        "/tmp/old-pgpass",
        "azurpilot",
        "azurpilot_app",
        should_succeed=False,
    )

    assert run.call_args.kwargs["expected"] == frozenset({2})
    assert run.call_args.kwargs["capture_stderr"] is True


def test_negative_auth_rejects_server_unavailable(monkeypatch):
    monkeypatch.setattr(
        postgresql_credentials,
        "_run",
        Mock(return_value=subprocess.CompletedProcess([], 2, stderr=b"connection refused")),
    )

    with pytest.raises(RuntimeError, match="auth test"):
        postgresql_credentials._auth(
            "archlinux",
            "/tmp/old-pgpass",
            "azurpilot",
            "azurpilot_app",
            should_succeed=False,
        )


def test_wsl_secret_file_is_restricted_before_write(monkeypatch):
    calls: list[tuple[list[str], bytes | None]] = []

    def observe(arguments, *, input_bytes=None, **_kwargs):
        calls.append((arguments, input_bytes))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(postgresql_credentials, "_run", observe)

    postgresql_credentials._write_wsl_file(
        "archlinux", "/etc/azurpilot/pgpass", "kykla", b"secret-content"
    )

    assert "install" in calls[0][0]
    assert "600" in calls[0][0]
    assert calls[0][1] is None
    assert "tee" in calls[1][0]
    assert calls[1][1] == b"secret-content"
