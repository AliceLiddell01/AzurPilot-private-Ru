import threading
import unittest
from unittest.mock import Mock, patch

from module.webui.fake_pil_module import remove_fake_pil_module

remove_fake_pil_module()

from module.webui.app_developer_tools import prepare_webui_restart, request_webui_restart
from module.webui.setting import State


class TestDeveloperToolsRestart(unittest.TestCase):
    def setUp(self):
        self.original_restart_event = State.restart_event
        self.original_restart_requested = State._restart_requested
        State.restart_event = Mock()
        State._restart_requested = False

    def tearDown(self):
        State.restart_event = self.original_restart_event
        State._restart_requested = self.original_restart_requested

    def test_prepare_restart_does_not_enumerate_or_mutate_bot_runtime(self):
        with (
            patch(
                "module.webui.app_developer_tools.BotRuntimeClient.running_instances",
                side_effect=AssertionError("WebUI restart must not inspect Bot Runtime workers"),
            ) as running_instances,
            patch("module.webui.app_developer_tools.BotRuntimeClient.get_manager") as get_manager,
        ):
            self.assertTrue(prepare_webui_restart())

        running_instances.assert_not_called()
        get_manager.assert_not_called()

    def test_manual_restart_does_not_interrupt_active_update_transaction(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_update_transaction():
            with State.restart_lock:
                entered.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold_update_transaction)
        holder.start()
        self.assertTrue(entered.wait(timeout=2))
        try:
            with (
                patch(
                    "module.webui.app_developer_tools.prepare_webui_restart"
                ) as prepare_restart,
                patch("module.webui.app_developer_tools.clearup") as clearup,
            ):
                self.assertFalse(request_webui_restart())

            prepare_restart.assert_not_called()
            clearup.assert_not_called()
            State.restart_event.set.assert_not_called()
            self.assertFalse(State._restart_requested)
        finally:
            release.set()
            holder.join(timeout=2)

    def test_manual_restart_notifies_parent_after_cleanup(self):
        order = []

        with (
            patch(
                "module.webui.app_developer_tools.prepare_webui_restart",
                side_effect=lambda: order.append("prepare") or True,
            ),
            patch(
                "module.webui.app_developer_tools.clearup",
                side_effect=lambda: order.append("clearup") or True,
            ),
        ):
            State.restart_event.set.side_effect = lambda: order.append("reload")
            self.assertTrue(request_webui_restart())

        self.assertEqual(["prepare", "clearup", "reload"], order)
        self.assertTrue(State._restart_requested)
