from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from module.application.errors import StorageConfigurationError
from module.persistence.config import DatabaseSettings

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (
    ROOT / "alas.py",
    ROOT / "mcp_server_sse.py",
    ROOT / "module" / "statistics",
    ROOT / "module" / "webui",
    ROOT / "module" / "commission",
    ROOT / "module" / "log_res",
    ROOT / "module" / "os",
    ROOT / "module" / "os_handler",
    ROOT / "module" / "os_shop",
    ROOT / "module" / "os_simulator",
)


def _marker_payload() -> dict[str, object]:
    return {
        "backend": "postgresql",
        "version": 1,
        "alembic_head": "0002_migration_shapes",
        "migration_manifest_sha256": "a" * 64,
        "reviewed_head": "b" * 40,
        "merge_commit": "c" * 40,
        "host": "127.0.0.1",
        "port": 5432,
        "database": "azurpilot",
        "user": "azurpilot_app",
        "sslmode": "disable",
        "runtime_timezone": "Asia/Novosibirsk",
    }


def test_backend_marker_is_required_and_rejects_sqlite(tmp_path: Path):
    marker = tmp_path / "storage_backend.json"

    with pytest.raises(StorageConfigurationError):
        DatabaseSettings.from_backend_marker(marker)

    marker.write_text("{not-json", encoding="utf-8")
    with pytest.raises(StorageConfigurationError):
        DatabaseSettings.from_backend_marker(marker)

    payload = _marker_payload()
    payload["backend"] = "sqlite"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageConfigurationError):
        DatabaseSettings.from_backend_marker(marker)

    payload = _marker_payload()
    payload["user"] = "azurpilot_migrator"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageConfigurationError):
        DatabaseSettings.from_backend_marker(marker)


def test_backend_marker_has_explicit_identity_time_and_provenance(tmp_path: Path):
    marker = tmp_path / "storage_backend.json"
    marker.write_text(json.dumps(_marker_payload()), encoding="utf-8")

    settings = DatabaseSettings.from_backend_marker(marker)

    assert settings.host == "127.0.0.1"
    assert settings.user == "azurpilot_app"
    assert settings.runtime_timezone == "Asia/Novosibirsk"
    assert settings.password is None


def test_runtime_bootstrap_is_lazy_without_health_request(tmp_path: Path):
    marker = tmp_path / "storage_backend.json"
    marker.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from module.persistence.runtime import bootstrap_runtime_storage, dispose_runtime_storage
service = bootstrap_runtime_storage({str(marker)!r}, require_ready=False)
assert service is not None
dispose_runtime_storage()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_production_modules_do_not_import_sqlite_or_legacy_database():
    violations: list[str] = []
    for root in PRODUCTION_ROOTS:
        paths = (root,) if root.is_file() else root.rglob("*.py")
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = {node.module}
                else:
                    continue
                if "sqlite3" in names or "module.statistics.cl1_database" in names:
                    violations.append(str(path.relative_to(ROOT)))
    assert not violations, violations

    azurstats = (ROOT / "module" / "statistics" / "azurstats.py").read_text(
        encoding="utf-8"
    )
    assert "load_meowofficer_farming" not in azurstats
    assert "np.loadtxt" not in azurstats


def test_lifecycle_scripts_encode_postgresql_ownership():
    start = (ROOT / "scripts" / "Start-AzurPilot.ps1").read_text(encoding="utf-8")
    update = (ROOT / "scripts" / "Update-AzurPilot.ps1").read_text(encoding="utf-8")
    repair = (ROOT / "scripts" / "Repair-AzurPilot.ps1").read_text(encoding="utf-8")
    build = (ROOT / "scripts" / "Build-AzurPilot.ps1").read_text(encoding="utf-8")

    assert "systemctl', 'start', 'postgresql'" in start
    assert "'pg_isready', '--host', '127.0.0.1'" in start
    assert "dev_tools.postgresql_runtime" in start
    backup_call = update.index("\n        Backup-ProductionPostgreSql\n")
    merge_call = update.index("'merge'", backup_call)
    assert backup_call < merge_call
    assert "Invoke-ProductionPostgreSqlSchemaUpgrade" in update
    assert "Repair не изменяет БД" in repair
    assert "dev_tools.postgresql_security" in repair
    assert "dev_tools.postgresql_runtime" not in build


def test_webui_rejects_database_upload_before_read():
    source = (ROOT / "module" / "webui" / "api.py").read_text(encoding="utf-8")
    rejection = source.index('raise ValueError("LEGACY_DB_UPLOAD_REJECTED")')
    first_read = source.index("await file.read()")
    assert rejection < first_read


def test_webui_legacy_upload_path_is_confined_before_read(tmp_path: Path):
    from module.webui.api import _legacy_upload_target

    (tmp_path / "config").mkdir()
    target, relative = _legacy_upload_target(tmp_path, "old/config/profile.json")
    assert target == tmp_path / "config" / "profile.json"
    assert relative == "config/profile.json"

    with pytest.raises(ValueError, match="LEGACY_DB_UPLOAD_REJECTED"):
        _legacy_upload_target(tmp_path, "old/config/cl1_data.db")
    with pytest.raises(ValueError, match="LEGACY_UPLOAD_PATH_REJECTED"):
        _legacy_upload_target(tmp_path, "old/config/../../outside.json")
