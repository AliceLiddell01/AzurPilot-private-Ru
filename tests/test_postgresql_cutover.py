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
        postgresql_cutover.activate(arguments)

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
