from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from module.game_settings.model import GameSettingsScanResult
from module.game_settings.scanner import GameSettingsScanner
from module.ui.ui import UI


class _FakeGameSettingsScanner(GameSettingsScanner):
    def __init__(self) -> None:
        self.initialized_for_test = True

    def _scan_game_settings(self) -> GameSettingsScanResult:
        return GameSettingsScanResult()


class GameSettingsScannerTests(unittest.TestCase):
    def test_public_entry_point_is_executable_without_screenshot(self) -> None:
        scanner = _FakeGameSettingsScanner()

        self.assertEqual(scanner.scan_game_settings(), GameSettingsScanResult())

    def test_scanner_reuses_shared_ui_base(self) -> None:
        self.assertTrue(issubclass(GameSettingsScanner, UI))
        self.assertTrue(inspect.isabstract(GameSettingsScanner))
        self.assertEqual(getattr(GameSettingsScanner, "__parameters__", ()), ())

    def test_scanner_uses_concrete_result_contract(self) -> None:
        public_signature = inspect.signature(GameSettingsScanner.scan_game_settings)
        implementation_signature = inspect.signature(
            GameSettingsScanner._scan_game_settings
        )

        self.assertIs(public_signature.return_annotation, GameSettingsScanResult)
        self.assertIs(
            implementation_signature.return_annotation,
            GameSettingsScanResult,
        )

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
