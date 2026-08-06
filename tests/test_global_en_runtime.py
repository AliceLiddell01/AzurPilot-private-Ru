from __future__ import annotations

import ast
import unittest
from pathlib import Path

import module.config.server as server
from module.config.utils import SERVER_TO_TIMEZONE, server_timezone
from module.webui.oobe import OOBEWizard

ROOT = Path(__file__).resolve().parents[1]
GLOBAL_PACKAGE = "com.YoStarEN.AzurLane"


class GlobalEnRuntimeTests(unittest.TestCase):
    def test_server_and_package_contract(self) -> None:
        self.assertEqual(server.server, "en")
        self.assertEqual(tuple(server.VALID_SERVER), ("en",))
        self.assertEqual(server.VALID_PACKAGE, {GLOBAL_PACKAGE: "en"})
        self.assertEqual(server.VALID_CHANNEL_PACKAGE, {})
        self.assertEqual(server.to_server("en"), "en")
        self.assertEqual(server.to_server(GLOBAL_PACKAGE), "en")
        self.assertEqual(server.to_package("en"), GLOBAL_PACKAGE)
        self.assertEqual(server.to_package(GLOBAL_PACKAGE), GLOBAL_PACKAGE)
        with self.assertRaises(ValueError):
            server.to_server("auto")
        with self.assertRaises(ValueError):
            server.to_package("auto")

    def test_foreign_and_unknown_inputs_are_rejected(self) -> None:
        for value in (
            "cn", "jp", "tw", "com.bilibili.azurlane",
            "com.YoStarJP.AzurLane", "com.hkmanjuu.azurlane.gp", "unknown",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    server.to_server(value)
                with self.assertRaises(ValueError):
                    server.to_package(value)

    def test_validation_precedes_release_side_effect(self) -> None:
        source = (ROOT / "module/config/server.py").read_text(encoding="utf-8")
        function = next(
            node for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "set_server"
        )
        calls = [
            node.func.id
            for statement in function.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertLess(calls.index("to_server"), calls.index("release_resources"))

    def test_timezone_is_global_only_and_fail_closed(self) -> None:
        self.assertEqual(set(SERVER_TO_TIMEZONE), {"en"})
        self.assertEqual(server_timezone(), SERVER_TO_TIMEZONE["en"])
        old = server.server
        try:
            server.server = "cn"
            with self.assertRaises(ValueError):
                server_timezone()
        finally:
            server.server = old

    def test_oobe_exposes_only_global_package(self) -> None:
        wizard = OOBEWizard.__new__(OOBEWizard)
        wizard.package_name = GLOBAL_PACKAGE
        values = [item["value"] for item in wizard._package_options()]
        self.assertEqual(values, [GLOBAL_PACKAGE])


if __name__ == "__main__":
    unittest.main()
