from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dev_tools import postgresql_cutover


def _arguments(tmp_path: Path, report: Path, confirmation: str) -> argparse.Namespace:
    return argparse.Namespace(
        confirm=confirmation,
        reconciliation_report=str(report),
        marker=str(tmp_path / "storage_backend.json"),
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user="azurpilot_app",
        sslmode="disable",
        runtime_timezone="Asia/Novosibirsk",
        reviewed_head="b" * 40,
        merge_commit="c" * 40,
        legacy_marker=None,
        retire_invalid_legacy_marker_sha256=None,
    )


def test_activation_requires_ready_report_and_exact_confirmation(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"cutover_ready": True, "reason_codes": []}), encoding="utf-8"
    )

    with pytest.raises(RuntimeError):
        postgresql_cutover.activate(_arguments(tmp_path, report, "нет"))

    report.write_text(
        json.dumps({"cutover_ready": False, "reason_codes": ["BLOCKED"]}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError):
        postgresql_cutover.activate(
            _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
        )


def test_activation_writes_non_secret_marker_atomically(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"cutover_ready": True, "reason_codes": []}), encoding="utf-8"
    )
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)

    with patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"):
        assert postgresql_cutover.activate(arguments) is False

    marker = json.loads((tmp_path / "storage_backend.json").read_text(encoding="utf-8"))
    assert marker["backend"] == "postgresql"
    assert marker["runtime_timezone"] == "Asia/Novosibirsk"
    assert marker["reviewed_head"] == "b" * 40
    assert marker["merge_commit"] == "c" * 40
    assert "password" not in marker
    assert not tuple(tmp_path.glob("*.tmp"))

    with (
        patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"),
        pytest.raises(RuntimeError),
    ):
        postgresql_cutover.activate(arguments)


def test_activation_rejects_non_loopback_before_marker_creation(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"cutover_ready": True, "reason_codes": []}), encoding="utf-8"
    )
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
    arguments.host = "192.0.2.10"

    with pytest.raises(RuntimeError, match="loopback"):
        postgresql_cutover.activate(arguments)

    assert not (tmp_path / "storage_backend.json").exists()


def test_activation_retires_corrupt_legacy_only_with_exact_digest(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"cutover_ready": True, "reason_codes": []}), encoding="utf-8"
    )
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"Alas": {}}), encoding="utf-8")
    marker = tmp_path / "config/state/storage_backend.json"
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
    arguments.marker = str(marker)
    arguments.legacy_marker = str(legacy)

    with (
        patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"),
        pytest.raises(RuntimeError, match="exact SHA-256"),
    ):
        postgresql_cutover.activate(arguments)

    assert legacy.is_file()
    assert not marker.exists()

    arguments.retire_invalid_legacy_marker_sha256 = postgresql_cutover.hashlib.sha256(
        legacy.read_bytes()
    ).hexdigest()
    with patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"):
        assert postgresql_cutover.activate(arguments) is False

    assert marker.is_file()
    assert not legacy.exists()
    assert "password" not in json.loads(marker.read_text(encoding="utf-8"))


def test_activation_reports_valid_legacy_marker_migration(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"cutover_ready": True, "reason_codes": []}), encoding="utf-8"
    )
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(
        json.dumps(
            {
                "backend": "postgresql",
                "version": 1,
                "alembic_head": "0002_migration_shapes",
                "reconciliation_report_sha256": "a" * 64,
                "reviewed_head": "b" * 40,
                "merge_commit": "c" * 40,
                "host": "127.0.0.1",
                "port": 5432,
                "database": "azurpilot",
                "user": "azurpilot_app",
                "sslmode": "disable",
                "runtime_timezone": "Asia/Novosibirsk",
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "config/state/storage_backend.json"
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
    arguments.marker = str(marker)
    arguments.legacy_marker = str(legacy)

    with patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"):
        assert postgresql_cutover.activate(arguments) is True

    assert marker.is_file()
    assert not legacy.exists()
