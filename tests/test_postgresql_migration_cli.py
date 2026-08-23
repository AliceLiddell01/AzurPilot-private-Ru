from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
