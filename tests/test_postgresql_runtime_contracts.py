from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.application.canonical_payload import payload_digest
from module.application.errors import StorageConfigurationError, StorageInvalidDataError
from module.application.runtime_storage import RuntimeStorageService
from module.persistence import runtime as persistence_runtime
from module.persistence.config import (
    DEFAULT_BACKEND_MARKER_PATH,
    LEGACY_BACKEND_MARKER_PATH,
    DatabaseSettings,
    migrate_legacy_backend_marker,
)
from module.persistence.local_environment import DEFAULT_LOCAL_ENV_PATH
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD
from module.statistics import postgresql_stats
from tests.import_inspection import imports_for_path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (
    ROOT / "alas.py",
    ROOT / "mcp_server_sse.py",
    ROOT / "module" / "application",
    ROOT / "module" / "persistence",
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
        "alembic_head": EXPECTED_ALEMBIC_HEAD,
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


def test_backend_marker_requires_exact_typed_contract(tmp_path: Path):
    marker = tmp_path / "storage_backend.json"
    payload = _marker_payload()
    payload["password"] = "must-never-be-present"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageConfigurationError, match="contract"):
        DatabaseSettings.from_backend_marker(marker)

    payload = _marker_payload()
    payload["port"] = True
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageConfigurationError, match="неполон"):
        DatabaseSettings.from_backend_marker(marker)


def test_backend_marker_default_uses_runtime_state_namespace():
    assert DEFAULT_BACKEND_MARKER_PATH == Path("config/state/storage_backend.json")
    assert LEGACY_BACKEND_MARKER_PATH == Path("config/storage_backend.json")
    assert persistence_runtime._REPOSITORY_ROOT == ROOT


def test_valid_legacy_marker_migrates_create_only(tmp_path: Path):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    before = legacy.read_bytes()

    assert migrate_legacy_backend_marker(target=target, legacy=legacy)

    assert target.read_bytes() == before
    assert not legacy.exists()
    assert DatabaseSettings.from_backend_marker(target).user == "azurpilot_app"


def test_corrupt_legacy_marker_is_not_migrated(tmp_path: Path):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"Alas": {}}), encoding="utf-8")
    before = legacy.read_bytes()

    with pytest.raises(StorageConfigurationError, match="contract"):
        migrate_legacy_backend_marker(target=target, legacy=legacy)

    assert not target.exists()
    assert legacy.read_bytes() == before


def test_broken_legacy_marker_symlink_is_not_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    original_exists = Path.exists
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "exists",
        lambda candidate: False if candidate == legacy else original_exists(candidate),
    )
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: True if candidate == legacy else original_is_symlink(candidate),
    )

    with pytest.raises(StorageConfigurationError, match="небезопасен"):
        migrate_legacy_backend_marker(target=target, legacy=legacy)


def test_legacy_marker_migration_does_not_clobber_racing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    competing = json.dumps({**_marker_payload(), "port": 6543}).encode()
    original = Path.hardlink_to

    def create_competing_target(path: Path, source: Path):
        path.write_bytes(competing)
        return original(path, source)

    monkeypatch.setattr(Path, "hardlink_to", create_competing_target)

    assert not migrate_legacy_backend_marker(target=target, legacy=legacy)
    assert target.read_bytes() == competing
    assert legacy.is_file()


def test_legacy_marker_migration_finishes_same_inode_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    original = Path.hardlink_to

    def create_same_target_then_report_race(path: Path, source: Path):
        original(path, source)
        raise FileExistsError

    monkeypatch.setattr(Path, "hardlink_to", create_same_target_then_report_race)

    assert migrate_legacy_backend_marker(target=target, legacy=legacy)
    assert target.is_file()
    assert not legacy.exists()


def test_legacy_marker_migration_keeps_target_when_peer_removes_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    expected = legacy.read_bytes()
    original_stat = Path.stat
    legacy_stat_calls = 0

    def remove_legacy_after_target_validation(path: Path, *args, **kwargs):
        nonlocal legacy_stat_calls
        if path == legacy:
            legacy_stat_calls += 1
            if legacy_stat_calls == 3:
                legacy.unlink()
                raise FileNotFoundError
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", remove_legacy_after_target_validation)

    assert migrate_legacy_backend_marker(target=target, legacy=legacy)
    assert target.read_bytes() == expected
    assert not legacy.exists()


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


def test_database_diagnostics_does_not_initialize_engine_or_install_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    marker = tmp_path / DEFAULT_BACKEND_MARKER_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(_marker_payload()), encoding="utf-8")

    class _ReadOnlyEnvironment:
        def require_app_runtime_match(self, _settings: DatabaseSettings) -> None:
            return None

        def install(self, **_kwargs: object) -> None:
            pytest.fail("Диагностический путь не должен изменять process environment.")

    reads: list[Path] = []

    def _record_read(path: object) -> _ReadOnlyEnvironment:
        reads.append(Path(path))
        return _ReadOnlyEnvironment()

    monkeypatch.setattr(
        persistence_runtime,
        "read_local_postgres_environment",
        _record_read,
    )
    monkeypatch.setattr(persistence_runtime, "_engine", None)
    monkeypatch.setattr(persistence_runtime, "_engine_settings", None)

    diagnostics = persistence_runtime.build_runtime_database_diagnostics(
        SimpleNamespace(repository_root=tmp_path),
    )

    assert reads == [tmp_path / DEFAULT_LOCAL_ENV_PATH]
    assert persistence_runtime.runtime_engine() is None
    assert diagnostics.run_check("connectivity", "fixture-target").code == (
        "DEV_DATABASE_CONNECTION_UNAVAILABLE"
    )


def test_runtime_idempotency_key_is_stable_inside_observation_window():
    observed_at = datetime(2026, 8, 23, 12, 30, 15, 100, tzinfo=UTC)

    first = RuntimeStorageService._key(
        "commission", "profile", observed_at, (1, (("Cube", 2),))
    )
    retry = RuntimeStorageService._key(
        "commission",
        "profile",
        observed_at.replace(microsecond=900_000),
        (1, (("Cube", 2),)),
    )

    assert first == retry
    assert len(first) <= 128
    assert first != RuntimeStorageService._key(
        "commission",
        "profile",
        observed_at.replace(second=16),
        (1, (("Cube", 2),)),
    )
    assert first != RuntimeStorageService._key(
        "commission", "other-profile", observed_at, (1, (("Cube", 2),))
    )
    assert first != RuntimeStorageService._key(
        "commission", "profile", observed_at, (1, (("Cube", 3),))
    )

    expected = payload_digest(
        {
            "domain": "commission",
            "instance": "profile",
            "observation_window": observed_at.replace(microsecond=0).isoformat(),
            "payload": (Decimal("1.0"), (("Cube", 2),)),
        }
    )
    assert RuntimeStorageService._key(
        "commission", "profile", observed_at, (Decimal("1.0"), (("Cube", 2),))
    ).endswith(expected)


def test_database_settings_wraps_invalid_timezone_value():
    with pytest.raises(StorageConfigurationError, match="Часовой пояс"):
        DatabaseSettings(
            host="127.0.0.1",
            port=5432,
            database="azurpilot",
            user="azurpilot_app",
            runtime_timezone="bad\x00timezone",
        )


def test_resource_snapshot_rejects_unknown_field_before_storage_access():
    service = RuntimeStorageService(
        lambda: pytest.fail("При некорректном поле Unit of Work не открывается.")
    )

    with pytest.raises(StorageInvalidDataError, match="unexpected_resource"):
        service.record_resource_snapshot(
            "profile",
            {"oil": 100, "unexpected_resource": 200},
        )


def test_meow_projection_keeps_all_observed_hazard_levels(monkeypatch):
    monkeypatch.setattr(
        postgresql_stats,
        "get_runtime_storage",
        lambda: SimpleNamespace(
            current_datetime=lambda: datetime(2026, 8, 23, tzinfo=UTC)
        ),
    )
    monkeypatch.setattr(
        postgresql_stats,
        "get_monthly_stats",
        lambda *_args, **_kwargs: {
            "meow_round_times": [{"duration": 12.0, "hazard_level": 4}],
            "meow_battle_times": [],
            "meow_hazard_stats": {
                "2": {
                    "battle_raw_count": 1,
                    "effective_rounds": 0.5,
                    "battle_times": [],
                }
            },
            "siren_research_devices": {"cl1": 0, "meow": {"6": 1}},
            "meow_battle_count": 0.5,
            "meow_battle_raw_count": 1,
        },
    )

    result = postgresql_stats.get_meow_stats("profile", 2026, 8)

    assert tuple(result["by_hazard"]) == ("2", "3", "4", "5", "6")
    assert result["by_hazard"]["4"]["avg_round_time"] == 12.0
    assert result["by_hazard"]["6"]["siren_research_devices"] == 1


def test_production_modules_do_not_import_sqlite_or_legacy_database():
    violations: list[str] = []
    for root in PRODUCTION_ROOTS:
        paths = (root,) if root.is_file() else root.rglob("*.py")
        for path in paths:
            if (ROOT / "module" / "persistence" / "legacy") in path.parents:
                continue
            names = imports_for_path(ROOT, path)
            if any(
                name == "sqlite3"
                or name.startswith("sqlite3.")
                or name == "module.statistics.cl1_database"
                or name.startswith("module.statistics.cl1_database.")
                for name in names
            ):
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

    assert "'systemctl'\n                'start'\n                'postgresql'" in start
    assert "'--user'\n                'root'" in start
    assert "'pg_isready', '--host', '127.0.0.1'" in start
    assert "dev_tools.postgresql_runtime" in start
    backup_call = update.index("\n        $postgresqlBackupPath = Backup-ProductionPostgreSql\n")
    merge_call = update.index("'merge'", backup_call)
    assert backup_call < merge_call
    assert "Invoke-ProductionPostgreSqlSchemaUpgrade" in update
    assert "Repair не изменяет БД" in repair
    assert "dev_tools.postgresql_security" in repair
    assert "dev_tools.postgresql_runtime" not in build
    assert "Get-Command -Name 'wsl.exe'" in start
    assert "Get-Command -Name 'wsl.exe'" in repair
    assert "Select-Object -First 1" in start
    assert "Select-Object -First 1" in repair


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


def test_webui_validates_all_upload_paths_before_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from module.webui.api import api_import_legacy_upload

    class Upload:
        def __init__(self, filename: str):
            self.filename = filename
            self.read_count = 0

        async def read(self) -> bytes:
            self.read_count += 1
            return b"{}"

    class Form:
        def __init__(self, files: list[Upload]):
            self.files = files

        def getlist(self, _name: str) -> list[Upload]:
            return self.files

    class Request:
        def __init__(self, files: list[Upload]):
            self.files = files

        async def form(self) -> Form:
            return Form(self.files)

    valid = Upload("old/config/profile.json")
    rejected = Upload("old/config/cl1_data.db")
    monkeypatch.chdir(tmp_path)

    response = asyncio.run(api_import_legacy_upload(Request([valid, rejected])))

    assert response.status_code == 400
    assert valid.read_count == 0
    assert rejected.read_count == 0
    assert not (tmp_path / "config" / "profile.json").exists()
