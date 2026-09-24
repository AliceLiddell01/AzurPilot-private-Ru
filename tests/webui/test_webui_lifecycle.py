import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from module.webui.fake_pil_module import remove_fake_pil_module

remove_fake_pil_module()

from module.webui import app_lifecycle, setting
from module.webui.setting import State


class TestWebUILifecycle(unittest.TestCase):
    def setUp(self):
        self.original_clearup = State._clearup
        State._clearup = False

    def tearDown(self):
        State._clearup = self.original_clearup

    def test_clearup_stops_only_webui_resources_once(self):
        cleared = []

        def mark_state_cleared():
            State._clearup = True
            cleared.append(True)

        with (
            patch.object(app_lifecycle.RemoteAccess, "kill_ssh_process") as stop_remote,
            patch.object(app_lifecycle, "close_discord_rpc") as close_discord,
            patch.object(app_lifecycle, "stop_ocr_server_process") as stop_ocr,
            patch.object(app_lifecycle.task_handler, "stop") as stop_tasks,
            patch.object(State, "clearup", side_effect=mark_state_cleared) as clear_state,
        ):
            self.assertTrue(app_lifecycle.clearup())
            self.assertTrue(app_lifecycle.clearup())

        stop_remote.assert_called_once_with()
        close_discord.assert_called_once_with()
        stop_ocr.assert_called_once_with()
        stop_tasks.assert_called_once_with()
        clear_state.assert_called_once_with()
        self.assertEqual([True], cleared)

    def test_clearup_preserves_state_when_webui_task_handler_does_not_stop(self):
        with (
            patch.object(app_lifecycle.task_handler, "stop", return_value=False),
            patch.object(app_lifecycle.RemoteAccess, "kill_ssh_process"),
            patch.object(app_lifecycle, "close_discord_rpc"),
            patch.object(app_lifecycle, "stop_ocr_server_process"),
            patch.object(State, "clearup") as clear_state,
        ):
            self.assertFalse(app_lifecycle.clearup())

        clear_state.assert_not_called()

    def test_startup_keeps_agent_api_without_starting_notification_dispatcher(self):
        telemetry = object()
        notification_runtime = object()
        desktop_agent_runtime = object()
        startup_order = []
        state = SimpleNamespace(
            init=Mock(),
            deploy_config=SimpleNamespace(
                DiscordRichPresence=False,
                StartOcrServer=False,
                EnableRemoteAccess=False,
            ),
        )
        with (
            patch.object(app_lifecycle, "State", state),
            patch(
                "deploy.language_migration.migrate_deploy_language",
                return_value=SimpleNamespace(changed=False),
            ),
            patch("module.persistence.runtime.bootstrap_runtime_storage") as bootstrap,
            patch(
                "module.persistence.runtime.build_runtime_notification_telemetry",
                return_value=telemetry,
            ),
            patch(
                "module.persistence.runtime.build_runtime_notification_composition",
                return_value=notification_runtime,
            ) as build_notification,
            patch(
                "module.persistence.runtime.build_runtime_desktop_agent_composition",
                return_value=desktop_agent_runtime,
            ),
            patch.object(app_lifecycle.lang, "reload"),
            patch.dict(
                app_lifecycle.os.environ,
                {app_lifecycle._AUTOSTART_CONFIGURED_PROFILES_ENV: "1"},
            ),
            patch.object(
                app_lifecycle.BotRuntimeClient,
                "start_configured_profiles",
                side_effect=lambda: startup_order.append("bot_runtime"),
            ) as start_configured_profiles,
            patch.object(
                app_lifecycle.task_handler,
                "start",
                side_effect=lambda: startup_order.append("task_handler"),
            ),
        ):
            app_lifecycle.startup()

        bootstrap.assert_called_once_with(require_ready=True)
        build_notification.assert_called_once_with(
            telemetry=telemetry,
            dispatcher_enabled=False,
        )
        state.init.assert_called_once_with(
            notification_runtime=notification_runtime,
            desktop_agent_runtime=desktop_agent_runtime,
        )
        start_configured_profiles.assert_called_once_with()
        self.assertEqual(["bot_runtime", "task_handler"], startup_order)

    def test_startup_can_skip_configured_profile_autostart_for_isolated_smoke(self):
        state = SimpleNamespace(
            init=Mock(),
            deploy_config=SimpleNamespace(
                DiscordRichPresence=False,
                StartOcrServer=False,
                EnableRemoteAccess=False,
            ),
        )
        with (
            patch.object(app_lifecycle, "State", state),
            patch(
                "deploy.language_migration.migrate_deploy_language",
                return_value=SimpleNamespace(changed=False),
            ),
            patch("module.persistence.runtime.bootstrap_runtime_storage"),
            patch(
                "module.persistence.runtime.build_runtime_notification_telemetry",
                return_value=object(),
            ),
            patch(
                "module.persistence.runtime.build_runtime_notification_composition",
                return_value=object(),
            ),
            patch(
                "module.persistence.runtime.build_runtime_desktop_agent_composition",
                return_value=object(),
            ),
            patch.object(app_lifecycle.lang, "reload"),
            patch.dict(
                app_lifecycle.os.environ,
                {app_lifecycle._AUTOSTART_CONFIGURED_PROFILES_ENV: "0"},
            ),
            patch.object(
                app_lifecycle.BotRuntimeClient,
                "start_configured_profiles",
            ) as start_configured_profiles,
            patch.object(app_lifecycle.task_handler, "start") as start_tasks,
        ):
            app_lifecycle.startup()

        start_configured_profiles.assert_not_called()
        start_tasks.assert_called_once_with()

    def test_startup_rejects_invalid_autostart_before_runtime_initialization(self):
        state = SimpleNamespace(init=Mock())
        with (
            patch.object(app_lifecycle, "State", state),
            patch(
                "module.persistence.runtime.bootstrap_runtime_storage"
            ) as bootstrap,
            patch(
                "deploy.language_migration.migrate_deploy_language"
            ) as migrate_language,
            patch.object(
                app_lifecycle.BotRuntimeClient,
                "start_configured_profiles",
            ) as start_configured_profiles,
            patch.object(app_lifecycle.task_handler, "start") as start_tasks,
            patch.dict(
                app_lifecycle.os.environ,
                {app_lifecycle._AUTOSTART_CONFIGURED_PROFILES_ENV: "true"},
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "должен иметь значение"):
                app_lifecycle.startup()

        bootstrap.assert_not_called()
        migrate_language.assert_not_called()
        state.init.assert_not_called()
        start_configured_profiles.assert_not_called()
        start_tasks.assert_not_called()


class TestWebUIState(unittest.TestCase):
    def setUp(self):
        self.original_clearup = State._clearup
        self.original_init = State._init
        self.original_notification_runtime = State._notification_runtime
        self.original_desktop_agent_runtime = State._desktop_agent_runtime
        State._clearup = False
        State._notification_runtime = None
        State._desktop_agent_runtime = None

    def tearDown(self):
        for runtime, original in (
            (State._notification_runtime, self.original_notification_runtime),
            (State._desktop_agent_runtime, self.original_desktop_agent_runtime),
        ):
            if runtime is not None and runtime is not original:
                stop = getattr(runtime, "stop", None)
                if callable(stop):
                    stop()
        State._notification_runtime = self.original_notification_runtime
        State._desktop_agent_runtime = self.original_desktop_agent_runtime
        State._clearup = self.original_clearup
        State._init = self.original_init

    def test_clearup_stops_webui_notification_services_only(self):
        notification = Mock()
        desktop_agent = Mock()
        State._notification_runtime = notification
        State._desktop_agent_runtime = desktop_agent

        State.clearup()

        notification.stop.assert_called_once_with()
        desktop_agent.stop.assert_called_once_with()
        self.assertIsNone(State._notification_runtime)
        self.assertIsNone(State._desktop_agent_runtime)
        self.assertFalse(hasattr(State, "manager"))
        self.assertFalse(hasattr(State, "process_registry"))
        self.assertFalse(hasattr(State, "_runtime_control_server"))

    def test_init_reenables_cleanup_without_claiming_runtime_ownership(self):
        State._clearup = True

        State.init()

        self.assertFalse(State._clearup)
        self.assertTrue(State._init)
        self.assertIsNone(State._notification_runtime)
        self.assertFalse(hasattr(State, "manager"))
        self.assertFalse(hasattr(State, "process_registry"))
        self.assertFalse(hasattr(State, "_runtime_control_server"))

    def test_init_injects_and_starts_webui_notification_runtime(self):
        runtime = Mock()

        State.init(notification_runtime=runtime)

        runtime.start.assert_called_once_with()
        self.assertIs(State.get_notification_runtime(), runtime)

    def test_init_injects_and_starts_desktop_agent_runtime(self):
        desktop_agent = Mock()

        State.init(desktop_agent_runtime=desktop_agent)

        desktop_agent.start.assert_called_once_with()
        self.assertIs(State._desktop_agent_runtime, desktop_agent)

    def test_init_rolls_back_both_runtimes_when_notification_start_fails(self):
        notification_runtime = Mock()
        notification_runtime.start.side_effect = RuntimeError("start failed")
        desktop_agent = Mock()

        with self.assertRaisesRegex(RuntimeError, "start failed"):
            State.init(
                notification_runtime=notification_runtime,
                desktop_agent_runtime=desktop_agent,
            )

        self.assertIsNone(State._notification_runtime)
        self.assertIsNone(State._desktop_agent_runtime)
        notification_runtime.stop.assert_called_once_with()
        desktop_agent.stop.assert_called_once_with()
        desktop_agent.start.assert_not_called()

    def test_dependency_sync_pending_marker_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = os.path.join(directory, "dependency-sync-pending")
            with patch.object(setting, "DEPENDENCY_SYNC_PENDING_FILE", marker):
                self.assertFalse(setting.is_dependency_sync_pending())

                setting.mark_dependency_sync_pending()

                self.assertTrue(setting.is_dependency_sync_pending())
                setting.clear_dependency_sync_pending()
                self.assertFalse(setting.is_dependency_sync_pending())
