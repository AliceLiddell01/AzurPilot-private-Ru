import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, PropertyMock, patch

from module.webui.process_manager import ProcessManager
from module.webui.setting import State


class TestProcessManagerRegistry(unittest.TestCase):
    def setUp(self):
        self.original_manager = State.manager
        self.original_registry = State.process_registry
        self.original_clearup = State._clearup
        self.original_restart_requested = State._restart_requested
        self.original_processes = ProcessManager._processes
        self.original_lifecycle_locks = ProcessManager._lifecycle_locks
        State.manager = Mock()
        State.manager.Queue.return_value = Mock()
        State.process_registry = {}
        State._clearup = False
        State._restart_requested = False
        ProcessManager._processes = {}
        ProcessManager._lifecycle_locks = {}

    def tearDown(self):
        State.manager = self.original_manager
        State.process_registry = self.original_registry
        State._clearup = self.original_clearup
        State._restart_requested = self.original_restart_requested
        ProcessManager._processes = self.original_processes
        ProcessManager._lifecycle_locks = self.original_lifecycle_locks

    def test_second_session_uses_registered_worker_pid(self):
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")

        with (
            patch(
                "module.webui.process_manager.is_current_owner", return_value=True
            ),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 12345, "created_at": 1}},
            ),
            patch("module.webui.process_manager.process_matches", return_value=True),
        ):
            self.assertTrue(manager.alive)

    def test_stop_uses_registered_worker_pid_without_local_process(self):
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")

        with (
            patch.object(ProcessManager, "_kill_process_tree", return_value=True) as kill,
            patch(
                "module.webui.process_manager.is_current_owner", return_value=True
            ),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 12345, "created_at": 1}},
            ),
            patch("module.webui.process_manager.process_matches", return_value=True),
            patch("module.webui.process_manager.unregister_worker"),
        ):
            self.assertTrue(manager.stop())

        kill.assert_called_once_with(12345)
        self.assertNotIn("alas", State.process_registry)

    def test_stale_manager_does_not_report_success_or_unregister_replacement_worker(self):
        State.process_registry["alas"] = 23456
        manager = ProcessManager.get_manager("alas")
        old_process = Mock()
        old_process.pid = 12345
        old_process.is_alive.return_value = False
        manager._process = old_process

        with (
            patch("module.webui.process_manager.is_current_owner", return_value=True),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 23456, "created_at": 2.0}},
            ),
        ):
            self.assertFalse(manager.stop())

        self.assertEqual(23456, State.process_registry["alas"])

    def test_registration_readback_failure_rolls_back_registered_identity(self):
        manager = ProcessManager.get_manager("alas")
        registered_record = {"pid": 12345, "created_at": 10.5}

        with (
            patch(
                "module.webui.process_manager.register_worker",
                return_value=registered_record,
            ),
            patch(
                "module.webui.process_manager.get_workers",
                side_effect=RuntimeError("readback failed"),
            ),
            patch.object(manager, "_unregister_process") as unregister,
        ):
            with self.assertRaises(RuntimeError):
                manager._register_process(12345)

        unregister.assert_called_once_with(expected_worker=registered_record)

    def test_local_registry_is_published_after_runtime_state_confirmation(self):
        manager = ProcessManager.get_manager("alas")
        registered_record = {"pid": 12345, "created_at": 10.5}
        observed_registry: list[dict[str, int]] = []
        state_store = Mock()
        state_store.read.return_value = None

        def mark_worker_started(*_args: object, **_kwargs: object) -> None:
            observed_registry.append(dict(State.process_registry))

        state_store.mark_worker_started.side_effect = mark_worker_started

        with (
            patch("module.webui.process_manager.register_worker", return_value=registered_record),
            patch("module.webui.process_manager.get_workers", return_value={"alas": registered_record}),
            patch("module.application.runtime_state.RuntimeStateStore", return_value=state_store),
        ):
            manager._register_process(12345)

        self.assertEqual(observed_registry, [{}])
        self.assertEqual(State.process_registry["alas"], 12345)

    def test_worker_body_waits_for_delayed_runtime_registration(self):
        import psutil

        from module.application.runtime_state import RuntimeStateStore

        with TemporaryDirectory() as root:
            root_path = Path(root)
            worker_created_at = float(psutil.Process(os.getpid()).create_time())
            store = RuntimeStateStore(root_path)
            body_called = threading.Event()

            def body(*_args: object, **_kwargs: object) -> None:
                body_called.set()

            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(ProcessManager, "_run_process_body", side_effect=body),
            ):
                worker = threading.Thread(
                    target=ProcessManager.run_process,
                    args=(
                        "alas",
                        "alas",
                        Mock(),
                        None,
                        str(root_path),
                        "operation-1",
                        "session-1",
                    ),
                )
                worker.start()
                self.assertFalse(body_called.wait(timeout=0.2))

                store.mark_worker_started(
                    "alas",
                    worker_pid=os.getpid(),
                    worker_created_at=worker_created_at,
                    operation_id="operation-1",
                    session_id="session-1",
                )

                self.assertTrue(body_called.wait(timeout=5))
                worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            snapshot = store.read("alas")
            self.assertIsNotNone(snapshot)
            self.assertFalse(snapshot.worker_running)

    def test_startup_gate_does_not_accept_different_worker_identity(self):
        import psutil

        from module.application.runtime_state import RuntimeStateStore

        with TemporaryDirectory() as root:
            root_path = Path(root)
            store = RuntimeStateStore(root_path)
            store.mark_worker_started(
                "alas",
                worker_pid=12345,
                worker_created_at=54321.0,
            )
            current_created_at = float(psutil.Process(os.getpid()).create_time())

            self.assertFalse(
                store.wait_for_worker_started(
                    "alas",
                    worker_pid=os.getpid(),
                    worker_created_at=current_created_at,
                    timeout_seconds=0.05,
                )
            )

    def test_start_reconciles_dead_worker_before_claiming_new_worker(self):
        from module.application.runtime_state import RuntimeStateStore

        with TemporaryDirectory() as root:
            root_path = Path(root)
            store = RuntimeStateStore(root_path)
            store.mark_worker_started(
                "alas",
                worker_pid=12345,
                worker_created_at=54321.0,
                operation_id="old-start",
            )
            store.mark_task_started(
                "alas",
                "OldTask",
                expected_worker_pid=12345,
                expected_worker_created_at=54321.0,
                operation_id="old-task",
            )

            manager = ProcessManager("alas")
            new_process = Mock()
            new_process.pid = 23456

            def register_new_worker(_pid: int) -> None:
                store.mark_worker_started(
                    "alas",
                    worker_pid=23456,
                    worker_created_at=65432.0,
                    operation_id="new-start",
                )

            with (
                patch("module.webui.process_manager._REPOSITORY_ROOT", root_path),
                patch("module.webui.process_manager.get_workers", return_value={}),
                patch("module.webui.process_manager.process_matches", return_value=None),
                patch("module.webui.process_manager.Process", return_value=new_process),
                patch.object(manager, "_register_process", side_effect=register_new_worker),
                patch.object(manager, "start_log_queue_handler"),
                patch.object(
                    ProcessManager,
                    "alive",
                    new_callable=PropertyMock,
                    return_value=False,
                ),
            ):
                manager.start("alas", operation_id="new-start")

            current = store.read("alas")
            self.assertIsNotNone(current)
            self.assertEqual(current.worker_pid, 23456)
            self.assertFalse(current.busy)
            store.mark_task_started(
                "alas",
                "NewTask",
                expected_worker_pid=23456,
                expected_worker_created_at=65432.0,
                operation_id="new-task",
            )
            with self.assertRaisesRegex(RuntimeError, "устаревшей identity"):
                store.mark_task_finished(
                    "alas",
                    "OldTask",
                    expected_worker_pid=12345,
                    expected_worker_created_at=54321.0,
                    operation_id="old-task",
                )

    def test_start_reconciles_runtime_state_with_stale_local_registry_cache(self):
        from module.application.runtime_state import RuntimeStateStore

        with TemporaryDirectory() as root:
            root_path = Path(root)
            store = RuntimeStateStore(root_path)
            store.mark_worker_started(
                "alas",
                worker_pid=12345,
                worker_created_at=54321.0,
                operation_id="old-start",
            )
            store.mark_task_started(
                "alas",
                "OldTask",
                expected_worker_pid=12345,
                expected_worker_created_at=54321.0,
                operation_id="old-task",
            )
            State.process_registry["alas"] = 12345
            manager = ProcessManager("alas")
            new_process = Mock()
            new_process.pid = 23456

            def register_new_worker(_pid: int) -> None:
                store.mark_worker_started(
                    "alas",
                    worker_pid=23456,
                    worker_created_at=65432.0,
                    operation_id="new-start",
                )
                State.process_registry["alas"] = 23456

            with (
                patch("module.webui.process_manager._REPOSITORY_ROOT", root_path),
                patch("module.webui.process_manager.get_workers", return_value={}),
                patch("module.webui.process_manager.process_matches", return_value=None),
                patch("module.webui.process_manager.Process", return_value=new_process),
                patch.object(manager, "_register_process", side_effect=register_new_worker),
                patch.object(manager, "start_log_queue_handler"),
                patch.object(
                    ProcessManager,
                    "alive",
                    new_callable=PropertyMock,
                    return_value=False,
                ),
            ):
                manager.start("alas", operation_id="new-start")

            current = store.read("alas")
            self.assertIsNotNone(current)
            self.assertEqual(current.worker_pid, 23456)
            self.assertEqual(State.process_registry["alas"], 23456)

    def test_start_does_not_reconcile_live_orphan_worker(self):
        from module.application.runtime_state import RuntimeStateError, RuntimeStateStore

        with TemporaryDirectory() as root:
            root_path = Path(root)
            store = RuntimeStateStore(root_path)
            store.mark_worker_started(
                "alas",
                worker_pid=12345,
                worker_created_at=54321.0,
                operation_id="old-start",
            )
            before = store.read("alas")
            manager = ProcessManager("alas")
            with (
                patch("module.webui.process_manager._REPOSITORY_ROOT", root_path),
                patch("module.webui.process_manager.get_workers", return_value={}),
                patch("module.webui.process_manager.process_matches", return_value=True),
                patch("module.webui.process_manager.Process") as process,
                patch.object(ProcessManager, "alive", new_callable=PropertyMock, return_value=False),
            ):
                with self.assertRaises(RuntimeStateError):
                    manager.start("alas", operation_id="new-start")

            process.assert_not_called()
            self.assertEqual(store.read("alas"), before)

    def test_stop_uses_local_process_handle_before_tree_kill(self):
        """При живом локальном Process сначала использовать terminate/kill, а не taskkill."""
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")
        process = Mock()
        process.pid = 12345
        # _is_process_alive: начальная и две синхронизационные проверки дают True;
        # _stop_local_process: после terminate ещё True, после kill — False.
        process.is_alive.side_effect = [True, True, True, True, True, False]
        manager._process = process

        with (
            patch.object(ProcessManager, "_kill_process_tree") as kill,
            patch(
                "module.webui.process_manager.is_current_owner", return_value=True
            ),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 12345, "created_at": 1}},
            ),
            patch("module.webui.process_manager.process_matches", return_value=True),
            patch("module.webui.process_manager.unregister_worker"),
        ):
            self.assertTrue(manager.stop())

        # Локальный процесс успешно остановлен, поэтому fallback на taskkill не нужен.
        kill.assert_not_called()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertNotIn("alas", State.process_registry)

    def test_stop_falls_back_to_tree_kill_when_local_fails(self):
        """Если локальные terminate/kill не помогли, завершить дерево через taskkill."""
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")
        process = Mock()
        process.pid = 12345
        # _is_process_alive: начальная и две синхронизационные проверки дают True.
        # _stop_local_process: после terminate и kill процесс остаётся жив.
        # После fallback _kill_process_tree и join(3) итоговая проверка даёт False.
        process.is_alive.side_effect = [True, True, True, True, True, True, False]
        manager._process = process

        with (
            patch.object(ProcessManager, "_kill_process_tree", return_value=True) as kill,
            patch(
                "module.webui.process_manager.is_current_owner", return_value=True
            ),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 12345, "created_at": 1}},
            ),
            patch("module.webui.process_manager.process_matches", return_value=True),
            patch("module.webui.process_manager.unregister_worker"),
        ):
            self.assertTrue(manager.stop())

        # Локальная остановка не удалась, поэтому требуется fallback на taskkill.
        kill.assert_called_once_with(12345)
        process.kill.assert_called()  # Вызов выполняется внутри _stop_local_process.
        self.assertNotIn("alas", State.process_registry)

    def test_failed_cross_session_stop_keeps_worker_registered(self):
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")

        with patch.object(ProcessManager, "_kill_process_tree", return_value=False):
            with (
                patch(
                    "module.webui.process_manager.is_current_owner", return_value=True
                ),
                patch(
                    "module.webui.process_manager.get_workers",
                    return_value={"alas": {"pid": 12345, "created_at": 1}},
                ),
                patch("module.webui.process_manager.process_matches", return_value=True),
            ):
                self.assertFalse(manager.stop())

        self.assertEqual(12345, State.process_registry["alas"])

    def test_pid_reuse_clears_stale_registration_without_terminating_unknown_process(self):
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")

        with (
            patch.object(ProcessManager, "_kill_process_tree") as kill,
            patch(
                "module.webui.process_manager.is_current_owner", return_value=True
            ),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 12345, "created_at": 1}},
            ),
            patch("module.webui.process_manager.process_matches", return_value=False),
            patch("module.webui.process_manager.unregister_worker"),
        ):
            self.assertTrue(manager.stop())

        kill.assert_not_called()
        self.assertNotIn("alas", State.process_registry)

    def test_unowned_cross_session_worker_is_not_terminated(self):
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")

        with (
            patch.object(ProcessManager, "_kill_process_tree") as kill,
            patch(
                "module.webui.process_manager.is_current_owner", return_value=False
            ),
        ):
            self.assertFalse(manager.stop())

        kill.assert_not_called()
        self.assertEqual(12345, State.process_registry["alas"])

    def test_local_process_pid_reuse_is_not_terminated(self):
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")
        process = Mock()
        process.pid = 12345
        process.is_alive.return_value = True
        manager._process = process

        with (
            patch.object(ProcessManager, "_kill_process_tree") as kill,
            patch(
                "module.webui.process_manager.is_current_owner", return_value=True
            ),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 12345, "created_at": 1}},
            ),
            patch("module.webui.process_manager.process_matches", return_value=False),
            patch("module.webui.process_manager.unregister_worker"),
        ):
            self.assertFalse(manager.stop())

        kill.assert_not_called()
        # join(timeout=0) — неблокирующая проверка zombie-процесса, а не обычный join.
        join_calls = [c.kwargs.get("timeout") for c in process.join.call_args_list]
        self.assertTrue(join_calls)
        self.assertTrue(
            all(timeout == 0 for timeout in join_calls),
            f"Ожидался только join(timeout=0): {join_calls}",
        )
        self.assertIs(manager._process, process)
        self.assertNotIn("alas", State.process_registry)

    def test_stop_revalidates_identity_before_terminating_process_tree(self):
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")
        process = Mock()
        process.pid = 12345
        process.is_alive.return_value = True
        manager._process = process

        with (
            patch.object(ProcessManager, "_kill_process_tree") as kill,
            patch(
                "module.webui.process_manager.is_current_owner", return_value=True
            ),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 12345, "created_at": 1}},
            ),
            patch(
                "module.webui.process_manager.process_matches",
                side_effect=[True, False],
            ) as matches,
        ):
            self.assertFalse(manager.stop())

        self.assertEqual(2, matches.call_count)
        kill.assert_not_called()

    def test_start_waits_for_stop_lifecycle_lock(self):
        State.process_registry["alas"] = 12345
        manager = ProcessManager.get_manager("alas")
        starter_manager = ProcessManager("alas")
        old_process = Mock()
        old_process.pid = 12345
        old_process.is_alive.side_effect = [True, False, False]
        manager._process = old_process

        stop_entered = threading.Event()
        release_stop = threading.Event()
        new_process_started = threading.Event()
        new_process = Mock()
        new_process.pid = 23456
        new_process.start.side_effect = new_process_started.set

        def kill_process_tree(_):
            stop_entered.set()
            release_stop.wait(timeout=2)
            return True

        with (
            patch.object(
                ProcessManager, "_kill_process_tree", side_effect=kill_process_tree
            ),
            patch(
                "module.webui.process_manager.is_current_owner", return_value=True
            ),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 12345, "created_at": 1}},
            ),
            patch("module.webui.process_manager.process_matches", return_value=True),
            patch("module.webui.process_manager.unregister_worker"),
            patch("module.webui.process_manager.Process", return_value=new_process),
            patch.object(starter_manager, "_register_process"),
            patch.object(starter_manager, "start_log_queue_handler"),
            patch.object(
                ProcessManager,
                "alive",
                new_callable=PropertyMock,
                return_value=False,
            ),
        ):
            stopper = threading.Thread(target=manager.stop)
            starter = threading.Thread(target=lambda: starter_manager.start("alas"))
            stopper.start()
            self.assertTrue(stop_entered.wait(timeout=2))
            starter.start()
            self.assertFalse(new_process_started.wait(timeout=0.2))

            release_stop.set()
            stopper.join(timeout=2)
            starter.join(timeout=2)

        self.assertFalse(stopper.is_alive())
        self.assertFalse(starter.is_alive())
        self.assertTrue(new_process_started.is_set())

    def test_cooperative_stop_persists_state_before_signaling_event(self):
        from module.application.runtime_state import RuntimeStateStore

        with TemporaryDirectory() as root:
            root_path = Path(root)
            RuntimeStateStore(root_path).mark_worker_started(
                "ap",
                worker_pid=12345,
                worker_created_at=1.0,
            )
            manager = ProcessManager("ap")
            observed: list[bool | None] = []

            class Event:
                def set(self) -> None:
                    snapshot = RuntimeStateStore(root_path).read("ap")
                    observed.append(snapshot.stop_requested if snapshot is not None else None)

            manager._stop_event = Event()
            with (
                patch("module.webui.process_manager._REPOSITORY_ROOT", root_path),
                patch.object(ProcessManager, "alive", new_callable=PropertyMock, return_value=True),
            ):
                self.assertTrue(
                    manager.request_cooperative_stop(
                        operation_id="operation-1",
                        session_id="session-1",
                    )
                )

            self.assertEqual(observed, [True])
            snapshot = RuntimeStateStore(root_path).read("ap")
            self.assertIsNotNone(snapshot)
            self.assertTrue(snapshot.stop_requested)

    def test_cooperative_stop_does_not_signal_when_state_persistence_fails(self):
        from module.application.runtime_state import RuntimeStateStore

        manager = ProcessManager("ap")
        stop_event = Mock()
        manager._stop_event = stop_event
        with (
            patch.object(ProcessManager, "alive", new_callable=PropertyMock, return_value=True),
            patch.object(
                RuntimeStateStore,
                "request_quiesce",
                side_effect=RuntimeError("synthetic state failure"),
            ),
        ):
            self.assertFalse(
                manager.request_cooperative_stop(
                    operation_id="operation-1",
                    session_id="session-1",
                )
            )

        stop_event.set.assert_not_called()

    def test_unregister_without_expected_worker_rejects_existing_registry_record(self):
        manager = ProcessManager("alas")
        stop_event = Mock()
        manager._stop_event = stop_event

        with (
            patch("module.webui.process_manager.is_current_owner", return_value=True),
            patch(
                "module.webui.process_manager.get_workers",
                return_value={"alas": {"pid": 12345, "created_at": 1.0}},
            ),
        ):
            self.assertFalse(manager._unregister_process())

        self.assertIsNotNone(manager._stop_event)
        self.assertIs(stop_event, manager._stop_event)

    def test_start_rejects_during_update_transaction(self):
        manager = ProcessManager.get_manager("alas")
        process_started = threading.Event()
        process = Mock()
        process.pid = 12345
        process.start.side_effect = process_started.set

        with (
            patch("module.webui.process_manager.Process", return_value=process),
            patch.object(manager, "_register_process"),
            patch.object(manager, "start_log_queue_handler"),
            patch.object(
                ProcessManager,
                "alive",
                new_callable=PropertyMock,
                return_value=False,
            ),
        ):
            State.restart_lock.acquire()
            try:
                starter = threading.Thread(target=lambda: manager.start("alas"))
                starter.start()
                starter.join(timeout=2)
            finally:
                State.restart_lock.release()

        self.assertFalse(starter.is_alive())
        self.assertFalse(process_started.is_set())

    def test_start_rejects_during_webui_cleanup(self):
        manager = ProcessManager.get_manager("alas")
        process_started = threading.Event()
        process = Mock()
        process.pid = 12345
        process.start.side_effect = process_started.set

        with (
            patch("module.webui.process_manager.Process", return_value=process),
            patch.object(manager, "_register_process"),
            patch.object(manager, "start_log_queue_handler"),
            patch.object(
                ProcessManager,
                "alive",
                new_callable=PropertyMock,
                return_value=False,
            ),
        ):
            State.cleanup_lock.acquire()
            try:
                starter = threading.Thread(target=lambda: manager.start("alas"))
                starter.start()
                starter.join(timeout=2)
            finally:
                State.cleanup_lock.release()

        self.assertFalse(starter.is_alive())
        self.assertFalse(process_started.is_set())

    def test_start_allows_reentrant_update_recovery(self):
        manager = ProcessManager.get_manager("alas")
        process_started = threading.Event()
        process = Mock()
        process.pid = 12345
        process.start.side_effect = process_started.set

        with (
            patch("module.webui.process_manager.Process", return_value=process),
            patch.object(manager, "_register_process"),
            patch.object(manager, "start_log_queue_handler"),
            patch.object(
                ProcessManager,
                "alive",
                new_callable=PropertyMock,
                return_value=False,
            ),
        ):
            with State.restart_lock:
                manager.start("alas")

        self.assertTrue(process_started.is_set())

    def test_start_registration_failure_does_not_kill_exited_pid(self):
        manager = ProcessManager.get_manager("alas")
        process = Mock()
        process.pid = 12345
        process.is_alive.return_value = False

        with (
            patch("module.webui.process_manager.Process", return_value=process),
            patch.object(manager, "_register_process", side_effect=RuntimeError("deny")),
            patch.object(ProcessManager, "_kill_process_tree") as kill,
        ):
            with self.assertRaises(RuntimeError):
                manager.start(func="alas")

        kill.assert_not_called()
        process.join.assert_called_once_with(timeout=0)
        self.assertIsNone(manager._process)
