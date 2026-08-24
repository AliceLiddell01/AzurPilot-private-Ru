from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dev_tools import postgresql_cutover
from module.application.errors import StorageConfigurationError


def _ready_report() -> dict[str, object]:
    return {
        "format": "azurpilot-postgresql-migration-report-v1",
        "schema_head": "0002_migration_shapes",
        "source_record_coverage": True,
        "semantic_shadow_parity": True,
        "repeat_import_zero_delta": True,
        "dump_restore_parity": True,
        "target": {
            "host": "127.0.0.1",
            "port": 5432,
            "database": "azurpilot",
            "user": "azurpilot_migrator",
            "sslmode": "disable",
            "runtime_timezone": "Asia/Novosibirsk",
        },
        "cutover_ready": True,
        "reason_codes": [],
    }


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


def _matching_marker_payload(
    report: Path, arguments: argparse.Namespace
) -> dict[str, object]:
    return {
        "backend": "postgresql",
        "version": 1,
        "alembic_head": "0002_migration_shapes",
        "reconciliation_report_sha256": postgresql_cutover.hashlib.sha256(
            report.read_bytes()
        ).hexdigest(),
        "reviewed_head": arguments.reviewed_head,
        "merge_commit": arguments.merge_commit,
        "host": arguments.host,
        "port": arguments.port,
        "database": arguments.database,
        "user": arguments.user,
        "sslmode": arguments.sslmode,
        "runtime_timezone": arguments.runtime_timezone,
    }


def test_activation_requires_ready_report_and_exact_confirmation(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_ready_report()), encoding="utf-8"
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

    report.write_text(
        json.dumps({"cutover_ready": True, "reason_codes": []}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="полный cutover evidence"):
        postgresql_cutover.activate(
            _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
        )


def test_activation_writes_non_secret_marker_atomically(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_ready_report()), encoding="utf-8"
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
        json.dumps(_ready_report()), encoding="utf-8"
    )
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
    arguments.host = "192.0.2.10"

    with pytest.raises(RuntimeError, match="loopback"):
        postgresql_cutover.activate(arguments)

    assert not (tmp_path / "storage_backend.json").exists()


def test_activation_retires_corrupt_legacy_only_with_exact_digest(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_ready_report()), encoding="utf-8"
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

    arguments.retire_invalid_legacy_marker_sha256 = (
        "  "
        + postgresql_cutover.hashlib.sha256(legacy.read_bytes()).hexdigest().upper()
        + "  "
    )
    with patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"):
        assert postgresql_cutover.activate(arguments) is False

    assert marker.is_file()
    assert not legacy.exists()
    assert "password" not in json.loads(marker.read_text(encoding="utf-8"))


def test_activation_reports_valid_legacy_marker_migration(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_ready_report()), encoding="utf-8"
    )
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    marker = tmp_path / "config/state/storage_backend.json"
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
    arguments.marker = str(marker)
    arguments.legacy_marker = str(legacy)
    legacy.write_text(
        json.dumps(_matching_marker_payload(report, arguments)), encoding="utf-8"
    )

    with patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"):
        assert postgresql_cutover.activate(arguments) is True

    assert marker.is_file()
    assert not legacy.exists()


def test_activation_does_not_treat_migration_io_failure_as_corrupt(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_ready_report()), encoding="utf-8"
    )
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
    arguments.legacy_marker = str(legacy)
    legacy.write_text(
        json.dumps(_matching_marker_payload(report, arguments)), encoding="utf-8"
    )

    with (
        patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"),
        patch.object(
            postgresql_cutover,
            "migrate_legacy_backend_marker",
            side_effect=StorageConfigurationError("migration-io-failure"),
        ),
        pytest.raises(StorageConfigurationError, match="migration-io-failure"),
    ):
        postgresql_cutover.activate(arguments)

    assert legacy.is_file()
    assert not Path(arguments.marker).exists()


def test_activation_replaces_valid_legacy_with_stale_provenance(tmp_path: Path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_ready_report()), encoding="utf-8"
    )
    marker = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
    arguments.marker = str(marker)
    arguments.legacy_marker = str(legacy)
    stale = _matching_marker_payload(report, arguments)
    stale["reviewed_head"] = "d" * 40
    legacy.write_text(json.dumps(stale), encoding="utf-8")

    with patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"):
        assert postgresql_cutover.activate(arguments) is False

    assert not legacy.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["reviewed_head"] == "b" * 40


def test_activation_detects_legacy_change_before_marker_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_ready_report()), encoding="utf-8"
    )
    marker = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
    arguments.marker = str(marker)
    arguments.legacy_marker = str(legacy)
    stale = _matching_marker_payload(report, arguments)
    stale["reviewed_head"] = "d" * 40
    legacy.write_text(json.dumps(stale), encoding="utf-8")
    original_read_bytes = Path.read_bytes
    legacy_reads = 0

    def change_legacy_on_precheck(path: Path):
        nonlocal legacy_reads
        if path == legacy:
            legacy_reads += 1
            if legacy_reads == 2:
                legacy.write_text(json.dumps({"changed": True}), encoding="utf-8")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", change_legacy_on_precheck)

    with (
        patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"),
        pytest.raises(RuntimeError, match="до публикации"),
    ):
        postgresql_cutover.activate(arguments)

    assert not marker.exists()


def test_parser_rejects_empty_legacy_marker():
    with pytest.raises(SystemExit):
        postgresql_cutover._parser().parse_args(
            [
                "--confirm",
                postgresql_cutover.CONFIRMATION,
                "--reconciliation-report",
                "report.json",
                "--legacy-marker",
                "",
                "--reviewed-head",
                "b" * 40,
                "--merge-commit",
                "c" * 40,
            ]
        )


def test_help_is_parsed_before_local_environment(monkeypatch):
    loader = patch.object(
        postgresql_cutover,
        "load_local_postgres_environment",
        side_effect=AssertionError("loader must not run"),
    )
    with loader as observed, pytest.raises(SystemExit) as exc_info:
        postgresql_cutover.main(["--help"])

    assert exc_info.value.code == 0
    observed.assert_not_called()


def test_activation_rejects_legacy_symlink_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_ready_report()), encoding="utf-8"
    )
    legacy = tmp_path / "config/storage_backend.json"
    arguments = _arguments(tmp_path, report, postgresql_cutover.CONFIRMATION)
    arguments.legacy_marker = str(legacy)
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: True if candidate == legacy else original_is_symlink(candidate),
    )

    with (
        patch.object(postgresql_cutover.StorageHealthChecker, "require_ready"),
        pytest.raises(RuntimeError, match="небезопасен"),
    ):
        postgresql_cutover.activate(arguments)

    assert not Path(arguments.marker).exists()
