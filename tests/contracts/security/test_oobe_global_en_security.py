from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from module.config.server import GLOBAL_PACKAGE
from module.device.connection import Connection
from module.webui.oobe import OOBEWizard


class OobeGlobalEnSecurityTests(unittest.TestCase):
    def test_known_package_detection_uses_exact_allowlist_membership(self) -> None:
        connection = Connection.__new__(Connection)
        connection.list_package = lambda show_log=True: [
            GLOBAL_PACKAGE,
            GLOBAL_PACKAGE.lower(),
            f"prefix.{GLOBAL_PACKAGE}",
            "com.YoStarJP.AzurLane",
        ]
        self.assertEqual(connection.list_known_packages(show_log=False), [GLOBAL_PACKAGE])

    def test_legacy_auto_uses_detected_global_package_only(self) -> None:
        connection = Connection.__new__(Connection)
        connection.serial = "test-device"
        connection.package = "auto"
        connection.config = SimpleNamespace(Emulator_PackageName="auto")
        connection.list_known_packages = lambda: [GLOBAL_PACKAGE]

        with patch("module.device.connection.set_server") as set_server:
            connection.detect_package(set_config=True)

        self.assertEqual(connection.package, GLOBAL_PACKAGE)
        self.assertEqual(connection.config.Emulator_PackageName, GLOBAL_PACKAGE)
        set_server.assert_called_once_with(GLOBAL_PACKAGE)

    def test_oobe_rejects_dom_tampered_package_before_config_write(self) -> None:
        wizard = OOBEWizard.__new__(OOBEWizard)
        wizard.package_name = "com.YoStarJP.AzurLane"
        wizard.emulator_serial = "auto"
        wizard._sync_emulator_pin_values = lambda: None
        with self.assertRaises(ValueError):
            wizard._collect_emulator_and_go(1)

    def test_review_table_escapes_untrusted_values(self) -> None:
        wizard = OOBEWizard.__new__(OOBEWizard)
        wizard.server = "en"
        wizard.config_name = "<script>alert(1)</script>"
        wizard.emulator_serial = "auto"
        wizard.package_name = GLOBAL_PACKAGE
        wizard.server_name = "en-0"
        wizard._render_footer = lambda *args, **kwargs: None
        rendered = []
        with patch("module.webui.oobe.put_html", side_effect=rendered.append), \
                patch("module.webui.oobe.lang.t", side_effect=lambda key: key):
            wizard._step_review()
        html = "".join(str(item) for item in rendered)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


if __name__ == "__main__":
    unittest.main()
