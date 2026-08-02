import unittest
from unittest.mock import Mock, patch

import gui


class TestWebUiNoReloadRecovery(unittest.TestCase):
    def test_no_reload_starts_only_after_orphan_recovery(self):
        call_order = []

        def recover():
            call_order.append("recover")
            return True

        def run_webui(*args):
            call_order.append(("func", args))

        with (
            patch.object(gui, "_recover_orphaned_workers", side_effect=recover) as recovery,
            patch.object(gui, "func", side_effect=run_webui) as func,
        ):
            self.assertTrue(gui._run_webui_without_reload())

        recovery.assert_called_once_with()
        func.assert_called_once_with(None, None)
        self.assertEqual(["recover", ("func", (None, None))], call_order)

    def test_no_reload_refuses_start_when_orphan_recovery_fails(self):
        with (
            patch.object(gui, "_recover_orphaned_workers", return_value=False) as recovery,
            patch.object(gui, "func", new=Mock()) as func,
        ):
            self.assertFalse(gui._run_webui_without_reload())

        recovery.assert_called_once_with()
        func.assert_not_called()


if __name__ == "__main__":
    unittest.main()
