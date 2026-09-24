import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from module.application import runtime_worker_registry as worker_registry


class TestWorkerRegistry(unittest.TestCase):
    def test_new_registry_is_only_written_to_bot_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"

            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=current_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=current_file,
            ), patch.object(worker_registry, "_process_created_at", return_value=10.5):
                worker_registry.claim_owner(100)

            self.assertTrue(current_file.exists())
            self.assertFalse(legacy_file.exists())

    def test_default_registry_locks_legacy_path_before_current_path(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            lock_files = []

            @contextmanager
            def capture_lock(lock_file):
                lock_files.append(lock_file)
                yield

            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=current_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=current_file,
            ), patch.object(worker_registry, "_locked_file", side_effect=capture_lock):
                with worker_registry._locked_registry() as registry_file:
                    self.assertEqual(current_file, registry_file)

            self.assertEqual(
                [
                    worker_registry._registry_lock_file(legacy_file),
                    worker_registry._registry_lock_file(current_file),
                ],
                lock_files,
            )

    def test_legacy_registry_is_migrated_out_of_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": 10.5,
                        "owner_pid": 100,
                        "workers": {"alas": {"created_at": 11.5, "pid": 200}},
                    }
                ),
                encoding="utf-8",
            )

            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=current_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=current_file,
            ), patch.object(worker_registry, "process_matches", return_value=None):
                self.assertIsNone(worker_registry.get_owner())

            self.assertFalse(legacy_file.exists())
            self.assertEqual(
                {"owner_created_at": None, "owner_pid": None, "workers": {}},
                json.loads(current_file.read_text(encoding="utf-8")),
            )

    def test_active_legacy_owner_remains_authoritative_until_it_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": 10.5,
                        "owner_pid": 100,
                        "workers": {},
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.multiple(
                    worker_registry,
                    WORKER_REGISTRY_FILE=current_file,
                    LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                    LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                    DEFAULT_WORKER_REGISTRY_FILE=current_file,
                ),
                patch.object(worker_registry, "process_matches", return_value=True),
                patch.object(worker_registry, "_process_created_at", return_value=20.5),
            ):
                self.assertEqual(
                    {"pid": 100, "created_at": 10.5},
                    worker_registry.get_owner_record_read_only(),
                )
                with self.assertRaises(worker_registry.WorkerRegistryOwnershipError):
                    worker_registry.claim_owner(200)

            self.assertTrue(legacy_file.exists())
            self.assertFalse(current_file.exists())

    def test_registry_records_worker_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            with patch.object(worker_registry, "WORKER_REGISTRY_FILE", registry_file):
                with patch.object(worker_registry, "_process_created_at", return_value=10.5):
                    worker_registry.claim_owner(100)
                    worker_registry.register_worker(100, "alas", 200)

                self.assertEqual(
                    {"alas": {"created_at": 10.5, "pid": 200}},
                    worker_registry.get_workers(100),
                )
                self.assertEqual(100, worker_registry.get_owner())
                self.assertEqual(
                    {"created_at": 10.5, "pid": 100},
                    worker_registry.get_owner_record(),
                )
                self.assertEqual(
                    {
                        "owner_created_at": 10.5,
                        "owner_pid": 100,
                        "workers": {"alas": {"created_at": 10.5, "pid": 200}},
                    },
                    json.loads(registry_file.read_text(encoding="utf-8")),
                )

    def test_unregister_worker_requires_expected_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            with patch.object(worker_registry, "WORKER_REGISTRY_FILE", registry_file):
                with patch.object(worker_registry, "_process_created_at", return_value=10.5):
                    worker_registry.claim_owner(100)
                    worker_registry.register_worker(100, "alas", 200)

                self.assertFalse(
                    worker_registry.unregister_worker(
                        100,
                        "alas",
                        expected_worker=None,
                    )
                )
                self.assertEqual(
                    {"alas": {"created_at": 10.5, "pid": 200}},
                    worker_registry.get_workers(100),
                )

    def test_owner_claim_does_not_overwrite_live_or_unknown_orphan_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            registry_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": None,
                        "owner_pid": None,
                        "workers": {"alas": {"created_at": 11.5, "pid": 200}},
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(worker_registry, "WORKER_REGISTRY_FILE", registry_file),
                patch.object(worker_registry, "_process_created_at", return_value=10.5),
            ):
                registry_before = registry_file.read_bytes()
                with (
                    patch.object(worker_registry, "process_matches", return_value=True),
                    self.assertRaises(worker_registry.WorkerRegistryOwnershipError),
                ):
                    worker_registry.claim_owner(100)
                self.assertEqual(registry_before, registry_file.read_bytes())
                self.assertEqual(
                    "alas",
                    next(iter(json.loads(registry_file.read_text(encoding="utf-8"))["workers"])),
                )

                registry_before = registry_file.read_bytes()
                with (
                    patch.object(
                        worker_registry,
                        "process_matches",
                        side_effect=RuntimeError("identity unavailable"),
                    ),
                    self.assertRaisesRegex(
                        worker_registry.WorkerRegistryOwnershipError,
                        "Нельзя подтвердить identity orphan worker",
                    ),
                ):
                    worker_registry.claim_owner(100)
                self.assertEqual(registry_before, registry_file.read_bytes())

    def test_owner_claim_rejects_orphan_worker_without_creation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            registry_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": None,
                        "owner_pid": None,
                        "workers": {"alas": {"pid": 200}},
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(worker_registry, "WORKER_REGISTRY_FILE", registry_file),
                patch.object(worker_registry, "_process_created_at", return_value=10.5),
                patch.object(worker_registry, "_pid_exists", return_value=False),
            ):
                with self.assertRaises(worker_registry.WorkerRegistryOwnershipError):
                    worker_registry.claim_owner(100)
                self.assertIsNone(worker_registry.get_owner())
                self.assertEqual(
                    {"owner_created_at": None, "owner_pid": None, "workers": {"alas": {"pid": 200}}},
                    json.loads(registry_file.read_text(encoding="utf-8")),
                )

    def test_read_only_worker_snapshot_does_not_migrate_or_create_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": 10.5,
                        "owner_pid": 100,
                        "workers": {"alas": {"created_at": 11.5, "pid": 200}},
                    }
                ),
                encoding="utf-8",
            )
            legacy_before = legacy_file.read_bytes()

            with (
                patch.multiple(
                    worker_registry,
                    WORKER_REGISTRY_FILE=current_file,
                    LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                    LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                    DEFAULT_WORKER_REGISTRY_FILE=current_file,
                ),
                patch.object(worker_registry, "_locked_file") as locked_file,
                patch.object(worker_registry, "process_matches", return_value=None),
                patch.object(
                    worker_registry,
                    "_read_registry",
                    wraps=worker_registry._read_registry,
                ) as read_registry,
            ):
                self.assertEqual(
                    {"created_at": 11.5, "pid": 200},
                    worker_registry.get_worker_read_only("alas"),
                )
                locked_file.assert_not_called()
                self.assertEqual(1, read_registry.call_count)

            self.assertFalse(current_file.exists())
            self.assertTrue(legacy_file.exists())
            self.assertEqual(legacy_before, legacy_file.read_bytes())
            self.assertFalse(
                worker_registry._registry_lock_file(legacy_file).exists()
            )
            self.assertFalse(
                worker_registry._registry_lock_file(current_file).exists()
            )

    def test_typed_read_only_worker_snapshot_distinguishes_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            legacy_file = Path(directory) / "legacy.json"
            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=registry_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=registry_file,
            ):
                result = worker_registry.read_worker_read_only("alas")

            self.assertEqual(
                worker_registry.ReadOnlyWorkerStatus.ABSENT,
                result.status,
            )
            self.assertIsNone(result.record)

    def test_typed_read_only_worker_snapshot_distinguishes_corrupt_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            registry_file.write_text("{not-json", encoding="utf-8")
            legacy_file = Path(directory) / "legacy.json"

            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=registry_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=registry_file,
            ):
                result = worker_registry.read_worker_read_only("alas")

            self.assertEqual(
                worker_registry.ReadOnlyWorkerStatus.UNKNOWN,
                result.status,
            )
            self.assertIsNone(result.record)
            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=registry_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=registry_file,
            ):
                with self.assertRaises(RuntimeError):
                    worker_registry.get_worker_read_only("alas")

    def test_corrupt_current_registry_with_clean_legacy_without_worker_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            current_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            current_file.write_text("{not-json", encoding="utf-8")
            legacy_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": None,
                        "owner_pid": None,
                        "workers": {},
                    }
                ),
                encoding="utf-8",
            )

            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=current_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=current_file,
            ):
                result = worker_registry.read_worker_read_only("alas")

            self.assertEqual(
                worker_registry.ReadOnlyWorkerStatus.UNKNOWN,
                result.status,
            )
            self.assertIsNone(result.record)

    def test_typed_read_only_worker_snapshot_rejects_non_object_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            registry_file.write_text("[]", encoding="utf-8")
            legacy_file = Path(directory) / "legacy.json"

            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=registry_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=registry_file,
            ):
                result = worker_registry.read_worker_read_only("alas")

            self.assertEqual(
                worker_registry.ReadOnlyWorkerStatus.UNKNOWN,
                result.status,
            )
            self.assertIsNone(result.record)

    def test_typed_read_only_worker_snapshot_rejects_invalid_record(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            registry_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": None,
                        "owner_pid": None,
                        "workers": {"alas": {"pid": 200, "created_at": "not-a-number"}},
                    }
                ),
                encoding="utf-8",
            )
            legacy_file = Path(directory) / "legacy.json"

            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=registry_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=registry_file,
            ):
                result = worker_registry.read_worker_read_only("alas")

            self.assertEqual(
                worker_registry.ReadOnlyWorkerStatus.UNKNOWN,
                result.status,
            )
            self.assertIsNone(result.record)

    def test_typed_read_only_worker_snapshot_rejects_nonpositive_identity(self):
        for pid, created_at in ((0, 10.5), (-1, 10.5), (200, 0), (200, -1)):
            with self.subTest(pid=pid, created_at=created_at), tempfile.TemporaryDirectory() as directory:
                registry_file = Path(directory) / "workers.json"
                registry_file.write_text(
                    json.dumps(
                        {
                            "owner_created_at": None,
                            "owner_pid": None,
                            "workers": {"alas": {"pid": pid, "created_at": created_at}},
                        }
                    ),
                    encoding="utf-8",
                )
                legacy_file = Path(directory) / "legacy.json"

                with patch.multiple(
                    worker_registry,
                    WORKER_REGISTRY_FILE=registry_file,
                    LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                    LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                    DEFAULT_WORKER_REGISTRY_FILE=registry_file,
                ):
                    result = worker_registry.read_worker_read_only("alas")

                self.assertEqual(
                    worker_registry.ReadOnlyWorkerStatus.UNKNOWN,
                    result.status,
                )
                self.assertIsNone(result.record)

    def test_read_only_owner_snapshot_does_not_migrate_or_create_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": 10.5,
                        "owner_pid": 100,
                        "workers": {},
                    }
                ),
                encoding="utf-8",
            )
            legacy_before = legacy_file.read_bytes()

            with patch.multiple(
                worker_registry,
                WORKER_REGISTRY_FILE=current_file,
                LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                DEFAULT_WORKER_REGISTRY_FILE=current_file,
            ), patch.object(
                worker_registry, "_locked_file"
            ) as locked_file, patch.object(
                worker_registry, "process_matches", return_value=True
            ):
                self.assertEqual(
                    {"created_at": 10.5, "pid": 100},
                    worker_registry.get_owner_record_read_only(),
                )

            locked_file.assert_not_called()
            self.assertFalse(current_file.exists())
            self.assertTrue(legacy_file.exists())
            self.assertEqual(legacy_before, legacy_file.read_bytes())
            self.assertFalse(
                worker_registry._registry_lock_file(legacy_file).exists()
            )

    def test_malformed_legacy_registry_makes_snapshot_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            current_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            current_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": 20.5,
                        "owner_pid": 300,
                        "workers": {"alas": {"created_at": 21.5, "pid": 400}},
                    }
                ),
                encoding="utf-8",
            )
            legacy_file.write_text("{not-json", encoding="utf-8")

            with (
                patch.multiple(
                    worker_registry,
                    WORKER_REGISTRY_FILE=current_file,
                    LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                    LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                    DEFAULT_WORKER_REGISTRY_FILE=current_file,
                ),
                patch.object(worker_registry, "_locked_file") as locked_file,
            ):
                result = worker_registry.read_worker_read_only("alas")

            locked_file.assert_not_called()
            self.assertIs(worker_registry.ReadOnlyWorkerStatus.UNKNOWN, result.status)
            self.assertTrue(current_file.exists())
            self.assertTrue(legacy_file.exists())

    def test_malformed_current_registry_makes_snapshot_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            current_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            current_file.write_text("{not-json", encoding="utf-8")
            legacy_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": 10.5,
                        "owner_pid": 100,
                        "workers": {"alas": {"created_at": 11.5, "pid": 200}},
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.multiple(
                    worker_registry,
                    WORKER_REGISTRY_FILE=current_file,
                    LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                    LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                    DEFAULT_WORKER_REGISTRY_FILE=current_file,
                ),
            ):
                result = worker_registry.read_worker_read_only("alas")

            self.assertEqual(current_file.read_text(encoding="utf-8"), "{not-json")
            self.assertTrue(legacy_file.exists())
            self.assertIs(worker_registry.ReadOnlyWorkerStatus.UNKNOWN, result.status)

    def test_read_only_snapshot_rejects_conflicting_canonical_and_legacy_registries(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            current_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            current_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": 20.5,
                        "owner_pid": 300,
                        "workers": {"alas": {"created_at": 21.5, "pid": 400}},
                    }
                ),
                encoding="utf-8",
            )
            legacy_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": 10.5,
                        "owner_pid": 100,
                        "workers": {"alas": {"created_at": 11.5, "pid": 200}},
                    }
                ),
                encoding="utf-8",
            )
            current_before = current_file.read_bytes()
            legacy_before = legacy_file.read_bytes()

            with (
                patch.multiple(
                    worker_registry,
                    WORKER_REGISTRY_FILE=current_file,
                    LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                    LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                    DEFAULT_WORKER_REGISTRY_FILE=current_file,
                ),
            ):
                result = worker_registry.read_worker_read_only("alas")

            self.assertEqual(current_before, current_file.read_bytes())
            self.assertEqual(legacy_before, legacy_file.read_bytes())
            self.assertTrue(current_file.exists())
            self.assertTrue(legacy_file.exists())
            self.assertIs(worker_registry.ReadOnlyWorkerStatus.UNKNOWN, result.status)

    def test_repeated_owner_claim_preserves_registered_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            with patch.object(worker_registry, "WORKER_REGISTRY_FILE", registry_file):
                with patch.object(worker_registry, "_process_created_at", return_value=10.5):
                    worker_registry.claim_owner(100)
                    worker_registry.register_worker(100, "alas", 200)
                    worker_registry.claim_owner(100)

                self.assertEqual(
                    {"alas": {"created_at": 10.5, "pid": 200}},
                    worker_registry.get_workers(100),
                )

    def test_active_owner_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            with patch.object(worker_registry, "WORKER_REGISTRY_FILE", registry_file):
                with patch.object(worker_registry, "_process_created_at", return_value=10.5):
                    worker_registry.claim_owner(100)
                    with patch.object(worker_registry, "process_matches", return_value=True):
                        with self.assertRaises(
                            worker_registry.WorkerRegistryOwnershipError
                        ):
                            worker_registry.claim_owner(200)

                self.assertEqual(100, worker_registry.get_owner())

    def test_stale_owner_with_exited_workers_can_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            with patch.object(worker_registry, "WORKER_REGISTRY_FILE", registry_file):
                with patch.object(worker_registry, "_process_created_at", return_value=10.5):
                    worker_registry.claim_owner(100)
                    worker_registry.register_worker(100, "alas", 200)
                    with patch.object(worker_registry, "process_matches", return_value=None):
                        worker_registry.claim_owner(300)

                self.assertEqual(300, worker_registry.get_owner())
                self.assertEqual({}, worker_registry.get_workers(300))

    def test_legacy_registry_inspection_error_blocks_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            current_file = Path(directory) / "config" / "state" / "bot-runtime" / "workers.json"
            legacy_file = Path(directory) / "config" / "webui-workers.json"
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.write_text(
                json.dumps(
                    {
                        "owner_created_at": 10.5,
                        "owner_pid": 100,
                        "workers": {},
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.multiple(
                    worker_registry,
                    WORKER_REGISTRY_FILE=current_file,
                    LEGACY_WORKER_REGISTRY_FILE=legacy_file,
                    LEGACY_WORKER_REGISTRY_FILES=(legacy_file,),
                    DEFAULT_WORKER_REGISTRY_FILE=current_file,
                ),
                patch.object(
                    worker_registry,
                    "process_matches",
                    side_effect=RuntimeError("identity unavailable"),
                ),
                self.assertRaises(worker_registry.WorkerRegistryOwnershipError),
            ):
                worker_registry.get_owner()

            self.assertTrue(legacy_file.exists())
            self.assertFalse(current_file.exists())

    def test_concurrent_owner_claim_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_file = Path(directory) / "workers.json"
            work_dir = Path(directory) / "claim"
            work_dir.mkdir()
            start_file = work_dir / "start"
            release_file = work_dir / "release"
            script = """
import os
import sys
import time
from pathlib import Path

sys.argv[0] = f"registryclaim{os.getpid()}.py"
from module.application import runtime_worker_registry as worker_registry

work_dir = Path(os.environ["WORKER_REGISTRY_TEST_DIR"])
pid = os.getpid()
Path(os.environ["WORKER_REGISTRY_TEST_REGISTRY"]).parent.mkdir(parents=True, exist_ok=True)
worker_registry.WORKER_REGISTRY_FILE = Path(os.environ["WORKER_REGISTRY_TEST_REGISTRY"])
(work_dir / f"{pid}.ready").write_text("", encoding="utf-8")
while not (work_dir / "start").exists():
    time.sleep(0.01)
try:
    worker_registry.claim_owner(pid)
except worker_registry.WorkerRegistryOwnershipError:
    (work_dir / f"{pid}.result").write_text("conflict", encoding="utf-8")
except Exception as exc:
    (work_dir / f"{pid}.result").write_text(
        f"error:{type(exc).__name__}", encoding="utf-8"
    )
else:
    (work_dir / f"{pid}.result").write_text("claimed", encoding="utf-8")
    while not (work_dir / "release").exists():
        time.sleep(0.01)
"""
            environment = os.environ.copy()
            environment.update(
                {
                    "WORKER_REGISTRY_TEST_DIR": str(work_dir),
                    "WORKER_REGISTRY_TEST_REGISTRY": str(registry_file),
                }
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script],
                    cwd=Path.cwd(),
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(2)
            ]
            try:
                deadline = time.monotonic() + 15
                while len(list(work_dir.glob("*.ready"))) < len(processes):
                    if time.monotonic() >= deadline:
                        self.fail("并发 owner 认领子进程未就绪")
                    time.sleep(0.05)
                start_file.touch()

                while len(list(work_dir.glob("*.result"))) < len(processes):
                    if time.monotonic() >= deadline:
                        self.fail("并发 owner 认领子进程未返回结果")
                    time.sleep(0.05)
                results = [
                    result_file.read_text(encoding="utf-8")
                    for result_file in work_dir.glob("*.result")
                ]
                self.assertCountEqual(["claimed", "conflict"], results)
                with patch.object(worker_registry, "WORKER_REGISTRY_FILE", registry_file):
                    self.assertIsNotNone(worker_registry.get_owner())
            finally:
                release_file.touch()
                for process in processes:
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=3)
