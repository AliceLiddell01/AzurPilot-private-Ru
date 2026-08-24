from __future__ import annotations

from pathlib import Path

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
