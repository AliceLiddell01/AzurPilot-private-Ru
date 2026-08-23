from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dev_tools import postgresql_migration
from module.persistence import DatabaseSettings
from module.persistence.legacy.reader import LegacySourceError

ROOT = Path(__file__).resolve().parents[1]


def _run(
    root: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
    report: Path | None = None,
):
    report = report or root.parent / f"{root.name}-migration-report.json"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "dev_tools.postgresql_migration",
            "--source-root",
            str(root),
            "--legacy-timezone",
            "Asia/Novosibirsk",
            "--report",
            str(report),
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_inspect_supports_non_ascii_path_and_redacts_decryption_identity(tmp_path):
    root = tmp_path / ("данные-" + "я" * 60)
    (root / "log").mkdir(parents=True)
    raw_identity = "synthetic-private-device-value"
    (root / "log" / "device_id.json").write_text(
        json.dumps({"device_id": raw_identity}), encoding="utf-8"
    )

    report = tmp_path / "inspection.json"
    result = _run(root, "inspect", report=report)

    assert result.returncode == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["format"] == "azurpilot-postgresql-source-inspection-v1"
    assert result.stdout.strip() == "STATUS:INSPECTED"
    assert raw_identity not in result.stdout + report.read_text(encoding="utf-8")
    assert str(root) not in result.stdout + report.read_text(encoding="utf-8")
    assert result.stderr == ""


def test_missing_source_path_returns_bounded_error_without_traceback(tmp_path):
    missing = tmp_path / "missing-private-path"

    result = _run(missing, "inspect")

    assert result.returncode == 2
    assert result.stdout.strip() == "ERROR:FILESYSTEM_OPERATION_FAILED"
    assert str(missing) not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_report_is_create_only(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    report = tmp_path / "report.json"
    report.write_text("preserve", encoding="utf-8")

    result = _run(source, "inspect", report=report)

    assert result.returncode == 2
    assert result.stdout.strip() == "ERROR:REPORT_TARGET_UNSAFE"
    assert report.read_text(encoding="utf-8") == "preserve"


def test_full_rehearsal_requires_exact_disposable_guard_before_network(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    environment = os.environ.copy()
    environment.update(
        AZURPILOT_POSTGRES_HOST="127.0.0.1",
        AZURPILOT_POSTGRES_PORT="65432",
        AZURPILOT_POSTGRES_DATABASE="synthetic_stage3",
        AZURPILOT_POSTGRES_USER="synthetic_stage3",
        AZURPILOT_POSTGRES_SSLMODE="disable",
    )
    for name in tuple(environment):
        if name.startswith("AZURPILOT_POSTGRES_DISPOSABLE"):
            del environment[name]

    result = _run(
        source,
        "full-rehearsal",
        "--scratch-database",
        "synthetic_restore",
        environment=environment,
    )

    assert result.returncode == 2
    assert result.stdout.strip() == "ERROR:DISPOSABLE_TARGET_NOT_CONFIRMED"
    assert "Traceback" not in result.stdout + result.stderr


def test_dump_restore_cleans_existing_scratch_schema(monkeypatch, tmp_path):
    calls: list[tuple[str, list[str]]] = []
    settings = DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="stage3_source",
        user="stage3",
        password="disposable-test-value",
        sslmode="disable",
    )
    monkeypatch.setattr(postgresql_migration, "_pg_tool", lambda name: name)
    monkeypatch.setattr(
        postgresql_migration,
        "_run_pg",
        lambda executable, arguments, _settings: calls.append(
            (executable, arguments)
        ),
    )

    restored = postgresql_migration._dump_restore(
        settings, "stage3_restore", tmp_path / "stage3.dump"
    )

    restore_arguments = calls[-1][1]
    assert calls[-1][0] == "pg_restore"
    assert "--clean" in restore_arguments
    assert "--if-exists" in restore_arguments
    assert restore_arguments.index("--clean") < restore_arguments.index("--dbname")
    assert restored.database == "stage3_restore"


def test_help_explains_diagnostic_and_readiness_commands():
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    help_result = subprocess.run(
        [sys.executable, "-m", "dev_tools.postgresql_migration", "--help"],
        cwd=ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert help_result.returncode == 0
    assert "всегда NOT_READY" in help_result.stdout
    assert "итогом готовности" in help_result.stdout
    assert "full-cutover" in help_result.stdout


def test_production_cutover_requires_exact_environment_guard(monkeypatch):
    settings = DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user="azurpilot_migrator",
        sslmode="disable",
    )
    monkeypatch.delenv("AZURPILOT_POSTGRES_CUTOVER", raising=False)

    with pytest.raises(
        LegacySourceError, match="PRODUCTION_CUTOVER_TARGET_NOT_CONFIRMED"
    ):
        postgresql_migration._require_production_cutover(
            settings, "azurpilot_restore", "FINAL-PRODUCTION-CUTOVER"
        )
