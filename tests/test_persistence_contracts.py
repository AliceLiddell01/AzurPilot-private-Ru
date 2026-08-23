from __future__ import annotations

import ast
import importlib.util
import os
import pickle
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy.exc import NoResultFound, OperationalError

from module.application import (
    StorageAuthenticationError,
    StorageConfigurationError,
    StorageConflictError,
    StorageInvalidDataError,
    StorageUnavailableError,
)
from module.persistence.config import DatabaseSettings, PoolSettings
from module.persistence.database import (
    LazyEngine,
    StorageHealthChecker,
    translate_database_error,
)
from module.persistence.repositories import PostgresInstanceIdentityRepository
from module.persistence.schema import SCHEMA_NAME, metadata
from module.persistence.unit_of_work import PostgresUnitOfWork

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ROOT = ROOT / "module" / "application"
PERSISTENCE_ROOT = ROOT / "module" / "persistence"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
            imports.update(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if alias.name != "*"
            )
    return imports


def _imports_prefix(path: Path, prefix: str) -> bool:
    return any(
        name == prefix or name.startswith(f"{prefix}.") for name in _imports(path)
    )


class PersistenceArchitectureTests(unittest.TestCase):
    def test_application_boundary_has_no_persistence_imports(self):
        paths = tuple(APPLICATION_ROOT.rglob("*.py"))
        self.assertTrue(paths, APPLICATION_ROOT)
        for path in paths:
            self.assertTrue(
                all(
                    not _imports_prefix(path, prefix)
                    for prefix in ("sqlalchemy", "psycopg", "alembic")
                ),
                path,
            )
            self.assertFalse(_imports_prefix(path, "module.persistence"), path)

    def test_persistence_boundary_never_imports_sqlite(self):
        paths = tuple(PERSISTENCE_ROOT.rglob("*.py"))
        self.assertTrue(paths, PERSISTENCE_ROOT)
        for path in paths:
            source = path.read_text(encoding="utf-8")
            self.assertFalse(_imports_prefix(path, "sqlite3"), path)
            self.assertNotIn("create_all(", source, path)
            if path.name == "repositories.py":
                self.assertNotIn(".commit(", source, path)

    def test_production_consumers_do_not_use_new_adapters(self):
        checked = [ROOT / "alas.py", ROOT / "gui.py", ROOT / "mcp_server_sse.py"]
        checked.extend((ROOT / "module" / "webui").rglob("*.py"))
        checked.extend((ROOT / "module" / "statistics").rglob("*.py"))
        for path in checked:
            self.assertTrue(path.is_file(), path)
            self.assertFalse(_imports_prefix(path, "module.persistence"), path)

    def test_import_has_no_network_or_ddl_side_effect(self):
        script = f"""
import socket
import sys
sys.path.insert(0, {str(ROOT)!r})
def blocked(*args, **kwargs):
    raise AssertionError('network attempted during import')
socket.create_connection = blocked
socket.socket.connect = blocked
import module.persistence
assert 'sqlite3' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_destructive_migration_requires_disposable_opt_in(self):
        migration = next(
            (ROOT / "migrations" / "versions").glob("0001_*.py"), None
        )
        self.assertIsNotNone(migration, "миграция 0001_* не найдена")
        spec = importlib.util.spec_from_file_location(
            "storage_foundation_migration", migration
        )
        if spec is None or spec.loader is None:
            self.fail("не удалось загрузить migration module")
        migration_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration_module)
        with (
            patch.dict(os.environ, {"AZURPILOT_POSTGRES_DISPOSABLE": "0"}),
            patch.object(migration_module.op, "drop_table") as drop_table,
            self.assertRaises(RuntimeError),
        ):
            migration_module.downgrade()
        drop_table.assert_not_called()


class DatabaseConfigurationTests(unittest.TestCase):
    def _settings(self) -> DatabaseSettings:
        return DatabaseSettings(
            host="127.0.0.1",
            port=5432,
            database="stage2",
            user="stage2",
            password="synthetic-value",
        )

    def test_password_is_redacted_from_repr_and_url(self):
        settings = self._settings()
        self.assertNotIn("synthetic-value", repr(settings))
        self.assertNotIn("synthetic-value", str(settings.sqlalchemy_url()))
        self.assertIn("***", str(settings.sqlalchemy_url()))

    def test_empty_optional_environment_values_use_defaults(self):
        values = {
            "AZURPILOT_POSTGRES_HOST": "127.0.0.1",
            "AZURPILOT_POSTGRES_DATABASE": "stage2",
            "AZURPILOT_POSTGRES_USER": "stage2",
            "AZURPILOT_POSTGRES_PASSWORD": "",
            "AZURPILOT_POSTGRES_SSLMODE": "",
        }
        with patch.dict(os.environ, values, clear=True):
            settings = DatabaseSettings.from_environment()
        self.assertIsNone(settings.password)
        self.assertEqual(settings.sslmode, "require")

    def test_pool_limits_fail_closed(self):
        with self.assertRaises(StorageConfigurationError):
            PoolSettings(size=0)
        with self.assertRaises(StorageConfigurationError):
            PoolSettings(max_overflow=9)
        with self.assertRaises(StorageConfigurationError):
            PoolSettings(timeout_seconds=0)

    def test_lazy_engine_is_thread_safe_and_rebuilt_after_pid_change(self):
        created: list[Mock] = []
        pid = [100]

        def factory(*args, **kwargs):
            engine = Mock()
            engine.options = kwargs
            created.append(engine)
            return engine

        lazy = LazyEngine(
            self._settings(), engine_factory=factory, pid_reader=lambda: pid[0]
        )
        results: list[object] = []
        threads = [
            threading.Thread(target=lambda: results.append(lazy.get()))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(created), 1)
        self.assertTrue(all(result is created[0] for result in results))
        self.assertEqual(created[0].options["pool_size"], 2)
        self.assertEqual(created[0].options["max_overflow"], 1)
        self.assertTrue(created[0].options["pool_pre_ping"])

        pid[0] = 101
        rebuilt = lazy.get()
        self.assertEqual(len(created), 2)
        self.assertIs(rebuilt, created[1])
        created[0].dispose.assert_called_once_with(close=False)
        lazy.dispose()
        created[1].dispose.assert_called_once_with()

    def test_after_fork_replaces_locked_state_without_disposing_parent_engine(self):
        lazy = LazyEngine(self._settings())
        parent_engine = Mock()
        inherited_lock = lazy._lock
        inherited_lock.acquire()
        lazy._engine = parent_engine
        lazy._pid = 100

        lazy._after_fork()

        self.assertIsNot(lazy._lock, inherited_lock)
        self.assertTrue(lazy._lock.acquire(blocking=False))
        lazy._lock.release()
        self.assertIsNone(lazy._engine)
        self.assertIsNone(lazy.owner_pid)
        parent_engine.dispose.assert_called_once_with(close=False)
        inherited_lock.release()

    def test_spawned_python_imports_driver_and_boundary(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import psycopg, sqlalchemy, alembic, module.persistence; print(psycopg.__version__)",
            ],
            check=False,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), result.stdout)

    def test_lazy_engine_pickle_resets_runtime_state_for_spawn(self):
        lazy = LazyEngine(self._settings())
        restored = pickle.loads(pickle.dumps(lazy))
        self.assertIsNone(restored.owner_pid)
        self.assertNotIn("synthetic-value", repr(restored.__getstate__()["settings"]))

    def test_database_errors_are_sanitized_and_classified(self):
        auth_orig = RuntimeError("password=do-not-show")
        auth_orig.sqlstate = "28P01"  # type: ignore[attr-defined]
        auth = OperationalError("statement", {"password": "hidden"}, auth_orig)
        mapped = translate_database_error(auth)
        self.assertIsInstance(mapped, StorageAuthenticationError)
        self.assertNotIn("do-not-show", str(mapped))
        self.assertNotIn("hidden", str(mapped))

        conflict_orig = RuntimeError("synthetic unique conflict")
        conflict_orig.sqlstate = "23505"  # type: ignore[attr-defined]
        conflict = translate_database_error(
            OperationalError("statement", {}, conflict_orig)
        )
        self.assertIsInstance(conflict, StorageConflictError)

        invalid_orig = RuntimeError("synthetic check failure")
        invalid_orig.sqlstate = "23514"  # type: ignore[attr-defined]
        invalid = translate_database_error(
            OperationalError("statement", {}, invalid_orig)
        )
        self.assertIsInstance(invalid, StorageInvalidDataError)

        unavailable = translate_database_error(RuntimeError("postgresql://secret"))
        self.assertIsInstance(unavailable, StorageUnavailableError)
        self.assertNotIn("secret", str(unavailable))

    def test_repository_maps_non_dbapi_sqlalchemy_errors(self):
        connection = Mock()
        connection.execute.side_effect = NoResultFound("synthetic missing row")
        repository = PostgresInstanceIdentityRepository(connection)

        with self.assertRaises(StorageUnavailableError):
            repository.resolve(alias_kind="filepath", alias_digest="a" * 64)

    def test_health_fails_closed_when_engine_initialization_fails(self):
        def broken_factory(*args, **kwargs):
            raise RuntimeError("driver initialization exposed a private path")

        database = LazyEngine(self._settings(), engine_factory=broken_factory)
        health = StorageHealthChecker(database).check()

        self.assertEqual(health.state.value, "unavailable")

    def test_unit_of_work_closes_connection_when_begin_fails(self):
        connection = Mock()
        connection.begin.side_effect = ValueError("synthetic setup failure")
        engine = Mock()
        engine.get.return_value.connect.return_value = connection

        with self.assertRaises(ValueError):
            PostgresUnitOfWork(engine).__enter__()

        connection.close.assert_called_once_with()

    def test_unit_of_work_clears_state_when_close_raises_unexpected_error(self):
        connection = Mock()
        connection.in_transaction.return_value = False
        connection.close.side_effect = RuntimeError("synthetic close failure")
        engine = Mock()
        engine.get.return_value.connect.return_value = connection
        unit_of_work = PostgresUnitOfWork(engine)
        unit_of_work.__enter__()

        with self.assertRaises(StorageUnavailableError):
            unit_of_work.__exit__(None, None, None)

        self.assertIsNone(unit_of_work._connection)
        for attribute in ("instances", "statistics", "imports"):
            self.assertFalse(hasattr(unit_of_work, attribute), attribute)


class SchemaMetadataTests(unittest.TestCase):
    def test_schema_v1_is_namespaced_and_bounded(self):
        expected = {
            "app_instance",
            "legacy_instance_alias",
            "import_batch",
            "import_record",
            "monthly_aggregate",
            "resource_snapshot",
            "opsi_item_event",
            "cl1_ap_snapshot",
            "cl1_ap_purchase_event",
            "cl1_currency_snapshot",
            "commission_income_event",
            "commission_income_item",
            "meow_timing_sample",
            "meow_hazard_aggregate",
            "siren_research_device_stat",
            "siren_research_device_event",
            "ap_notification_state",
            "resource_current_state",
        }
        self.assertEqual({table.name for table in metadata.tables.values()}, expected)
        self.assertTrue(
            all(table.schema == SCHEMA_NAME for table in metadata.tables.values())
        )
        self.assertFalse(
            any(
                "event_observation" in table.name
                for table in metadata.tables.values()
            )
        )

    def test_import_counters_cannot_exceed_record_count(self):
        constraints = {
            str(constraint.sqltext)
            for constraint in metadata.tables[f"{SCHEMA_NAME}.import_batch"].constraints
            if hasattr(constraint, "sqltext")
        }
        self.assertIn(
            "imported_count + conflict_count + quarantine_count <= record_count",
            constraints,
        )

    def test_constraints_indexes_and_fk_actions_are_explicit(self):
        for table in metadata.tables.values():
            for constraint in table.constraints:
                self.assertIsNotNone(constraint.name, (table.name, constraint))
                self.assertLessEqual(len(str(constraint.name)), 63)
            for foreign_key in table.foreign_keys:
                self.assertIn(foreign_key.ondelete, {"RESTRICT", "CASCADE"})
            for index in table.indexes:
                self.assertIsNotNone(index.name)
                self.assertLessEqual(len(str(index.name)), 63)

    def test_json_is_limited_to_quarantine_metadata(self):
        json_columns = {
            (table.name, column.name)
            for table in metadata.tables.values()
            for column in table.columns
            if column.type.__class__.__name__ in {"JSON", "JSONB"}
        }
        self.assertEqual(json_columns, {("import_record", "quarantine_metadata")})

    def test_temporal_and_json_constraints_are_semantic(self):
        for table_name in (
            "monthly_aggregate",
            "meow_timing_sample",
            "meow_hazard_aggregate",
            "siren_research_device_stat",
        ):
            constraints = {
                constraint.name: str(constraint.sqltext)
                for constraint in metadata.tables[
                    f"{SCHEMA_NAME}.{table_name}"
                ].constraints
                if hasattr(constraint, "sqltext")
            }
            expression = constraints[f"ck_{table_name}_month_first_day"]
            self.assertIn("EXTRACT(DAY FROM month)", expression)

        import_record_constraints = {
            str(constraint.sqltext)
            for constraint in metadata.tables[
                f"{SCHEMA_NAME}.import_record"
            ].constraints
            if hasattr(constraint, "sqltext")
        }
        self.assertTrue(
            any(
                "octet_length" in expression for expression in import_record_constraints
            )
        )

        checked_indexes = 0
        for table in metadata.tables.values():
            for index in table.indexes:
                if (
                    "observed" in str(index.name)
                    and any(column.name == "observed_at" for column in index.columns)
                    and table.c.observed_at.nullable
                ):
                    expressions = " ".join(str(item) for item in index.expressions)
                    self.assertIn("NULLS LAST", expressions.upper())
                    checked_indexes += 1
        self.assertGreater(checked_indexes, 0)


if __name__ == "__main__":
    unittest.main()
