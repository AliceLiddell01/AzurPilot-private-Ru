from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from module.game_settings.scanner import GameSettingsScanner
from module.ui.ui import UI


class _FakeGameSettingsScanner(GameSettingsScanner[str]):
    def __init__(self) -> None:
        self.initialized_for_test = True

    def _scan_game_settings(self) -> str:
        return "stage-1-contract"


class GameSettingsScannerTests(unittest.TestCase):
    def test_public_entry_point_is_executable_without_screenshot(self) -> None:
        scanner = _FakeGameSettingsScanner()

        self.assertEqual(scanner.scan_game_settings(), "stage-1-contract")

    def test_scanner_reuses_shared_ui_base(self) -> None:
        self.assertTrue(issubclass(GameSettingsScanner, UI))
        self.assertTrue(inspect.isabstract(GameSettingsScanner))

    def test_scanner_has_no_dock_dependency(self) -> None:
        scanner_path = (
            Path(__file__).resolve().parents[1]
            / "module"
            / "game_settings"
            / "scanner.py"
        )
        tree = ast.parse(scanner_path.read_text(encoding="utf-8"))
        imported_modules = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.append(node.module)

        self.assertFalse(
            any("dock" in module.casefold() for module in imported_modules),
            imported_modules,
        )


if __name__ == "__main__":
    unittest.main()
