from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Stage8AExternalContractTests(unittest.TestCase):
    def test_dependency_versions_are_pinned(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"adbutils==0.11.0"', pyproject)
        self.assertIn('"uiautomator2==2.16.17"', pyproject)
        lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
        self.assertRegex(lock, r'name = "adbutils"\s+version = "0\.11\.0"')
        self.assertRegex(lock, r'name = "uiautomator2"\s+version = "2\.16\.17"')

    def test_bundled_scrcpy_server_version_is_explicit(self):
        server = ROOT / "bin/scrcpy/scrcpy-server-v1.20.jar"
        review = (
            ROOT / ".codex/reviews/PR20_STAGE8A_EXTERNAL_CONTRACTS.md"
        ).read_text(encoding="utf-8")
        self.assertIn("bin/scrcpy/scrcpy-server-v1.20.jar", review)
        if server.exists():
            self.assertTrue(server.is_file())
            self.assertGreater(server.stat().st_size, 0)
        options = (ROOT / "module/device/method/scrcpy/options.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("command_v120", options)
        api = (ROOT / "module/webui/api.py").read_text(encoding="utf-8")
        self.assertIn("scrcpy-server 1.20", api)
        self.assertIn("command_v120", api)

    def test_adb_target_selection_is_explicit_in_fork(self):
        connection_attr = (
            ROOT / "module/device/connection_attr.py"
        ).read_text(encoding="utf-8")
        acceptance = (
            ROOT / "dev_tools/stage8a_device_acceptance.py"
        ).read_text(encoding="utf-8")
        self.assertIn("AdbDevice(self.adb_client, self.serial)", connection_attr)
        self.assertIn('[adb, "-s", serial, *args]', acceptance)

    def test_uiautomator2_project_calls_keep_timeout_layers_distinct(self):
        connection_attr = (
            ROOT / "module/device/connection_attr.py"
        ).read_text(encoding="utf-8")
        uia = (
            ROOT / "module/device/method/uiautomator_2.py"
        ).read_text(encoding="utf-8")
        self.assertIn("u2.connect(self.serial)", connection_attr)
        self.assertIn("set_new_command_timeout(604800)", connection_attr)
        self.assertIn("self.u2.http.post", uia)
        self.assertIn("timeout=", uia)
        for function in (
            "click_uiautomator2",
            "long_click_uiautomator2",
            "swipe_uiautomator2",
            "drag_uiautomator2",
            "u2_send_keys",
        ):
            tree = ast.parse(uia)
            self.assertTrue(
                any(
                    isinstance(node, ast.FunctionDef) and node.name == function
                    for node in ast.walk(tree)
                ),
                function,
            )

    def test_external_contract_review_document_covers_all_pins(self):
        review = (
            ROOT / ".codex/reviews/PR20_STAGE8A_EXTERNAL_CONTRACTS.md"
        ).read_text(encoding="utf-8")
        for token in (
            "adbutils `0.11.0`",
            "uiautomator2 `2.16.17`",
            "scrcpy-server `1.20`",
            "Current upstream",
            "Fork evidence",
        ):
            self.assertIn(token, review)


if __name__ == "__main__":
    unittest.main()
