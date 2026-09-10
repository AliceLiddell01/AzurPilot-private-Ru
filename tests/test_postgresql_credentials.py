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


def test_passfile_round_trips_escaped_password_fields():
    app_secret = r"app:value\\suffix"
    migrator_secret = r"migrator:value\\suffix"
    payload = postgresql_credentials._passfile(
        "azurpilot", app_secret, migrator_secret
    )

    assert (
        postgresql_credentials._password_for(payload, "azurpilot", "azurpilot_app")
        == app_secret
    )
    assert (
        postgresql_credentials._password_for(
            payload, "azurpilot", "azurpilot_migrator"
        )
        == migrator_secret
    )


def test_windows_passfile_merge_preserves_unrelated_entries():
    previous = (
        b"remote.example:5432:wikidb:alice:keep\n"
        b"remote.example:5432:azurpilot:azurpilot_app:remote-keep\n"
        b"127.0.0.1:5432:other:azurpilot_migrator:other-keep\n"
        b"127.0.0.1:5432:azurpilot:azurpilot_app:old\n"
    )

    merged = postgresql_credentials._merge_windows_passfile(
        previous, "azurpilot", "new-app", "new-migrator"
    ).decode()

    assert "remote.example:5432:wikidb:alice:keep" in merged
    assert "azurpilot_app:remote-keep" in merged
    assert "azurpilot_migrator:other-keep" in merged
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

    with pytest.raises(RuntimeError, match="env некорректно"):
        postgresql_credentials._env_document(
            tmp_path,
            tmp_path / "pgpass.conf",
            "/etc/azurpilot/pgpass",
            "bad\ndistro",
            "azurpilot",
            "new-app",
            "new-migrator",
        )


def test_env_reader_rejects_symlink_and_non_file(tmp_path: Path, monkeypatch):
    missing = tmp_path / "missing.env"
    assert postgresql_credentials._read_env_document(missing) is None

    regular = tmp_path / "regular.env"
    regular.write_bytes(b"KEY=value\n")
    assert postgresql_credentials._read_env_document(regular) == b"KEY=value\n"

    directory = tmp_path / "directory.env"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="небезопасен"):
        postgresql_credentials._read_env_document(directory)

    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: True
        if candidate == regular
        else original_is_symlink(candidate),
    )
    with pytest.raises(RuntimeError, match="небезопасен"):
        postgresql_credentials._read_env_document(regular)


def test_env_merge_preserves_unrelated_namespace_and_replaces_postgres(
    tmp_path: Path,
):
    generated = postgresql_credentials._env_document(
        tmp_path,
        tmp_path / "pgpass.conf",
        "/etc/azurpilot/pgpass",
        "archlinux",
        "azurpilot",
        "new-app",
        "new-migrator",
    )
    previous = (
        b"# shared local env\n"
        b"AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER=admin\n"
        b"AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=keep-secret\n"
        b"AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_EMAIL=operator@example.test\n"
        b"AZURPILOT_OBSERVABILITY_PGADMIN_PORT=5051\n"
        b"AZURPILOT_POSTGRES_DOCKER_BOOTSTRAP_PASSWORD=keep-bootstrap\n"
        b"AZURPILOT_POSTGRES_HOST=old-host\n"
        b"AZURPILOT_WSL_DISTRO=old-distro\n"
    )

    merged = postgresql_credentials._merge_env_document(previous, generated).decode()

    assert "# shared local env" in merged
    assert "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER=admin" in merged
    assert "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=keep-secret" in merged
    assert "AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_EMAIL=operator@example.test" in merged
    assert "AZURPILOT_OBSERVABILITY_PGADMIN_PORT=5051" in merged
    assert "AZURPILOT_POSTGRES_DOCKER_BOOTSTRAP_PASSWORD=keep-bootstrap" in merged
    assert "AZURPILOT_POSTGRES_HOST=127.0.0.1" in merged
    assert "AZURPILOT_WSL_DISTRO=archlinux" in merged
    assert "old-host" not in merged
    assert "old-distro" not in merged


def test_env_merge_rejects_duplicate_and_unknown_owned_keys(tmp_path: Path):
    generated = postgresql_credentials._env_document(
        tmp_path,
        tmp_path / "pgpass.conf",
        "/etc/azurpilot/pgpass",
        "archlinux",
        "azurpilot",
        "new-app",
        "new-migrator",
    )

    with pytest.raises(RuntimeError, match="дублирующийся"):
        postgresql_credentials._merge_env_document(
            b"SHARED_KEY=one\nSHARED_KEY=two\n",
            generated,
        )

    for key in ("AZURPILOT_POSTGRES_UNUSED", "AZURPILOT_WSL_UNUSED"):
        with pytest.raises(RuntimeError, match="неизвестный"):
            postgresql_credentials._merge_env_document(
                f"{key}=value\n".encode(),
                generated,
            )

    for key in (
        "AZURPILOT_POSTGRES_DOCKER_BOOTSTRP_PASSWORD",
        "AZURPILOT_OBSERVABILITY_PGADMIN_PORTX",
    ):
        with pytest.raises(RuntimeError, match="неизвестный"):
            postgresql_credentials._merge_env_document(
                f"{key}=value\n".encode(),
                generated,
            )


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

    with pytest.raises(RuntimeError, match="локальный путь"):
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
    assert "sync" in calls[2][0]


def test_wsl_passfile_requires_safe_path_owner_and_mode(monkeypatch):
    monkeypatch.setattr(
        postgresql_credentials,
        "_run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=b"81a0|600|kykla\n"
            )
        ),
    )
    postgresql_credentials._require_wsl_passfile(
        "archlinux", "/etc/azurpilot/pgpass", "kykla"
    )

    with pytest.raises(RuntimeError, match="небезопасный путь"):
        postgresql_credentials._require_wsl_passfile(
            "archlinux", "/tmp/pgpass", "kykla"
        )


def test_wsl_private_tempdir_requires_random_path_and_mode(monkeypatch):
    calls: list[list[str]] = []

    def observe(arguments, **_kwargs):
        calls.append(arguments)
        if "mktemp" in arguments:
            return subprocess.CompletedProcess(
                arguments, 0, stdout=b"/tmp/azurpilot-credentials.A1b2C3d4E5\n"
            )
        return subprocess.CompletedProcess(arguments, 0, stdout=b"700:kykla\n")

    monkeypatch.setattr(postgresql_credentials, "_run", observe)

    assert (
        postgresql_credentials._create_wsl_private_tempdir("archlinux", "kykla")
        == "/tmp/azurpilot-credentials.A1b2C3d4E5"
    )
    assert "mktemp" in calls[0]
    assert "stat" in calls[1]


def test_rotation_rejects_relative_or_repository_passfile(tmp_path: Path):
    arguments = SimpleNamespace(
        confirm=postgresql_credentials.CONFIRMATION,
        database="azurpilot",
        repository_root=str(tmp_path),
        windows_passfile="pgpass.conf",
        verified_backup=str(tmp_path / "backup.dump"),
        distro="archlinux",
        wsl_passfile="/etc/azurpilot/pgpass",
    )

    with pytest.raises(RuntimeError, match="абсолютный"):
        postgresql_credentials.rotate(arguments)

    arguments.windows_passfile = str(tmp_path / "pgpass.conf")
    with pytest.raises(RuntimeError, match="вне репозитория"):
        postgresql_credentials.rotate(arguments)


def test_rotation_requires_appdata_for_default_windows_passfile(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("APPDATA", raising=False)
    arguments = SimpleNamespace(
        confirm=postgresql_credentials.CONFIRMATION,
        database="azurpilot",
        repository_root=str(tmp_path),
        windows_passfile=None,
        verified_backup=str(tmp_path / "backup.dump"),
        distro="archlinux",
        wsl_passfile="/etc/azurpilot/pgpass",
    )

    with pytest.raises(RuntimeError, match="APPDATA"):
        postgresql_credentials.rotate(arguments)
