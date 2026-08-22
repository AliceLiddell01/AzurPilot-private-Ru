from __future__ import annotations

import ast
import os
import pickle
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from sqlalchemy.exc import OperationalError

from module.application import (
    StorageAuthenticationError,
    StorageConfigurationError,
    StorageUnavailableError,
)
from module.persistence.config import DatabaseSettings, PoolSettings
from module.persistence.database import LazyEngine, translate_database_error
from module.persistence.schema import SCHEMA_NAME, metadata

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ROOT = ROOT / "module" / "application"
PERSISTENCE_ROOT = ROOT / "module" / "persistence"


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


class PersistenceArchitectureTests(unittest.TestCase):
    def test_application_boundary_has_no_persistence_imports(self):
        for path in APPLICATION_ROOT.rglob("*.py"):
            self.assertTrue(
                _import_roots(path).isdisjoint({"sqlalchemy", "psycopg", "alembic"}),
                path,
            )

    def test_persistence_boundary_never_imports_sqlite(self):
        for path in PERSISTENCE_ROOT.rglob("*.py"):
            self.assertNotIn("sqlite3", _import_roots(path), path)
            self.assertNotIn("create_all(", path.read_text(encoding="utf-8"), path)
            if path.name == "repositories.py":
                self.assertNotIn(".commit(", path.read_text(encoding="utf-8"), path)

    def test_production_consumers_do_not_use_new_adapters(self):
        checked = [ROOT / "alas.py", ROOT / "gui.py", ROOT / "mcp_server_sse.py"]
        checked.extend((ROOT / "module" / "webui").rglob("*.py"))
        checked.extend((ROOT / "module" / "statistics").rglob("*.py"))
        for path in checked:
            self.assertNotIn(
                "module.persistence", path.read_text(encoding="utf-8"), path
            )

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
        self.assertIs(lazy.get(), created[1])
        created[0].dispose.assert_called_once_with(close=False)
        lazy.dispose()
        created[1].dispose.assert_called_once_with()

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

        unavailable = translate_database_error(RuntimeError("postgresql://secret"))
        self.assertIsInstance(unavailable, StorageUnavailableError)
        self.assertNotIn("secret", str(unavailable))


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
        self.assertFalse(any("event_observation" in name for name in expected))

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


if __name__ == "__main__":
    unittest.main()
