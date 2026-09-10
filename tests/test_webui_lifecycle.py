import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from module.webui.fake_pil_module import remove_fake_pil_module

remove_fake_pil_module()

from module.webui import app_lifecycle, setting
from module.webui.setting import State, _close_runtime_control_server


class TestWebUILifecycle(unittest.TestCase):
    def setUp(self):
        self.original_clearup = State._clearup
        State._clearup = False

    def tearDown(self):
        State._clearup = self.original_clearup

    def test_clearup_stops_all_running_instances_once(self):
        worker = Mock()

        def mark_state_cleared():
            State._clearup = True

        with (
            patch.object(
                app_lifecycle.ProcessManager,
                "running_instances",
                return_value=[worker],
            ) as running_instances,
            patch.object(app_lifecycle.RemoteAccess, "kill_ssh_process") as stop_remote,
            patch.object(app_lifecycle, "close_discord_rpc") as close_discord,
            patch.object(app_lifecycle, "stop_ocr_server_process") as stop_ocr,
            patch.object(app_lifecycle.task_handler, "stop") as stop_tasks,
            patch.object(State, "clearup", side_effect=mark_state_cleared) as clear_state,
        ):
            app_lifecycle.clearup()
            app_lifecycle.clearup()

        running_instances.assert_called_once_with()
        worker.stop.assert_called_once_with()
        stop_remote.assert_called_once_with()
        close_discord.assert_called_once_with()
        stop_ocr.assert_called_once_with()
        stop_tasks.assert_called_once_with()
        clear_state.assert_called_once_with()

    def test_clearup_keeps_manager_when_task_handler_does_not_stop(self):
        with (
            patch.object(app_lifecycle.task_handler, "stop", return_value=False),
            patch.object(
                app_lifecycle.ProcessManager,
                "running_instances",
                return_value=[],
            ),
            patch.object(app_lifecycle.RemoteAccess, "kill_ssh_process"),
            patch.object(app_lifecycle, "close_discord_rpc"),
            patch.object(app_lifecycle, "stop_ocr_server_process"),
            patch.object(State, "clearup") as clear_state,
        ):
            self.assertFalse(app_lifecycle.clearup())

        clear_state.assert_not_called()


class TestWebUIState(unittest.TestCase):
    def setUp(self):
        self.original_clearup = State._clearup
        self.original_manager = State.manager
        self.original_registry = State.process_registry
        self.original_runtime_control_server = State._runtime_control_server
        self.original_notification_runtime = State._notification_runtime
        self.original_desktop_agent_runtime = State._desktop_agent_runtime
        State._clearup = False

    def tearDown(self):
        current_server = State._runtime_control_server
        if current_server is not self.original_runtime_control_server:
            _close_runtime_control_server(current_server)
        State._runtime_control_server = self.original_runtime_control_server
        current_runtime = State._notification_runtime
        try:
            if current_runtime is not self.original_notification_runtime:
                stop = getattr(current_runtime, "stop", None)
                if callable(stop):
                    stop()
        finally:
            State._notification_runtime = self.original_notification_runtime
        current_desktop_agent_runtime = State._desktop_agent_runtime
        try:
            if current_desktop_agent_runtime is not self.original_desktop_agent_runtime:
                stop = getattr(current_desktop_agent_runtime, "stop", None)
                if callable(stop):
                    stop()
        finally:
            State._desktop_agent_runtime = self.original_desktop_agent_runtime
        State._clearup = self.original_clearup
        State.manager = self.original_manager
        State.process_registry = self.original_registry

    def test_clearup_is_idempotent(self):
        manager = Mock()
        State.manager = manager
        State.process_registry = {"alas": 12345}

        with (
            patch("module.webui.worker_registry.get_workers", return_value={}),
            patch("module.webui.worker_registry.clear_owner"),
        ):
            State.clearup()
            State.clearup()

        manager.shutdown.assert_called_once_with()
        self.assertTrue(State._clearup)
        self.assertIsNone(State.manager)
        self.assertIsNone(State.process_registry)

    def test_clearup_preserves_worker_registry_until_parent_reaps_it(self):
        manager = Mock()
        State.manager = manager
        State.process_registry = {"alas": 12345}

        with patch(
            "module.webui.worker_registry.get_workers",
            return_value={"alas": {"pid": 12345, "created_at": 1}},
        ):
            with self.assertRaises(RuntimeError):
                State.clearup()

        manager.shutdown.assert_not_called()
        self.assertFalse(State._clearup)

    def test_init_reenables_cleanup_after_previous_shutdown(self):
        manager = Mock()
        manager.dict.return_value = {}
        State._clearup = True

        with (
            patch("module.webui.setting.multiprocessing.Manager", return_value=manager),
            patch("module.webui.worker_registry.claim_owner"),
        ):
            State.init()

        self.assertFalse(State._clearup)
        self.assertIs(manager, State.manager)
        self.assertEqual({}, State.process_registry)

    def test_init_injects_and_starts_notification_runtime(self):
        manager = Mock()
        manager.dict.return_value = {}
        server = Mock()
        owner = Mock()
        owner.start_server.return_value = server
        runtime = Mock()

        with (
            patch("module.webui.setting.multiprocessing.Manager", return_value=manager),
            patch("module.webui.worker_registry.claim_owner"),
            patch(
                "module.webui.runtime_control_owner.WebUIRuntimeControlOwner",
                return_value=owner,
            ) as owner_factory,
        ):
            State.init(notification_runtime=runtime)

        owner_factory.assert_called_once()
        self.assertIs(owner_factory.call_args.kwargs["notification_service"], runtime)
        runtime.start.assert_called_once_with()
        self.assertIs(State._notification_runtime, runtime)
        self.assertIs(State.get_notification_runtime(), runtime)

    def test_init_injects_and_starts_desktop_agent_runtime(self):
        manager = Mock()
        manager.dict.return_value = {}
        server = Mock()
        owner = Mock()
        owner.start_server.return_value = server
        desktop_agent = Mock()

        with (
            patch("module.webui.setting.multiprocessing.Manager", return_value=manager),
            patch("module.webui.worker_registry.claim_owner"),
            patch(
                "module.webui.runtime_control_owner.WebUIRuntimeControlOwner",
                return_value=owner,
            ),
        ):
            State.init(desktop_agent_runtime=desktop_agent)

        desktop_agent.start.assert_called_once_with()
        self.assertIs(State._desktop_agent_runtime, desktop_agent)

    def test_dependency_sync_pending_marker_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = os.path.join(directory, "dependency-sync-pending")
            with patch.object(setting, "DEPENDENCY_SYNC_PENDING_FILE", marker):
                self.assertFalse(setting.is_dependency_sync_pending())

                setting.mark_dependency_sync_pending()

                self.assertTrue(setting.is_dependency_sync_pending())
                setting.clear_dependency_sync_pending()
                self.assertFalse(setting.is_dependency_sync_pending())

                setting.clear_dependency_sync_pending()
