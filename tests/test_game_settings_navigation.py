from __future__ import annotations

import ast
import unittest
from pathlib import Path

import cv2
import imageio.v2 as imageio

from module.exception import GamePageUnknownError, RequestHumanTakeover
from module.game_settings.assets import (
    GAME_SETTINGS_MAIN_GOTO_SETTINGS,
    GAME_SETTINGS_OPTIONS_SELECTED,
    GAME_SETTINGS_OPTIONS_UNSELECTED,
)
from module.game_settings.navigation import page_settings, page_settings_options
from module.game_settings.model import GameSettingsScanResult
from module.game_settings.scanner import GameSettingsScanner
from module.ui.assets import GOTO_MAIN
from module.ui.page import Page, page_campaign, page_main, page_main_white


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "game_settings"
ASSET_DIR = ROOT / "assets" / "en" / "game_settings"


def _button_template(button):
    button.ensure_template()
    return button.image[0] if button.is_gif else button.image


def _button_similarity(left, right) -> float:
    result = cv2.matchTemplate(
        _button_template(left),
        _button_template(right),
        cv2.TM_CCOEFF_NORMED,
    )
    return float(cv2.minMaxLoc(result)[1])


def _fixture_similarity(button, fixture_name: str) -> float:
    fixture = imageio.imread(FIXTURE_DIR / fixture_name)
    fixture = fixture[:, :, :3] if len(fixture.shape) == 3 else fixture
    result = cv2.matchTemplate(
        _button_template(button),
        fixture,
        cv2.TM_CCOEFF_NORMED,
    )
    return float(cv2.minMaxLoc(result)[1])


def _scope_signals(text: str) -> tuple[list[str], set[str]]:
    tree = ast.parse(text)
    imported_modules = []
    imported_call_aliases = {}
    call_names = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.append(alias.name)
                if alias.asname is not None:
                    imported_call_aliases[alias.asname] = alias.name.rsplit(".", 1)[-1]
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_modules.append(node.module)
            for alias in node.names:
                imported_modules.append(alias.name)
                imported_call_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                call_names.add(func.attr)
            elif isinstance(func, ast.Name):
                call_names.add(imported_call_aliases.get(func.id, func.id))

    return imported_modules, call_names


class _FakeNavigationScanner(GameSettingsScanner):
    def __init__(
        self,
        current_page,
        main_result_page=page_main_white,
        options_result_page=page_settings_options,
    ) -> None:
        self.ui_current = current_page
        self.main_result_page = main_result_page
        self.options_result_page = options_result_page
        self.goto_calls = []
        self.goto_main_calls = 0
        self.current_page_calls = 0

    def _scan_game_settings(self) -> GameSettingsScanResult:
        return GameSettingsScanResult()

    def ui_get_current_page(self, skip_first_screenshot=True):
        self.current_page_calls += 1
        return self.ui_current

    def ui_goto(
        self,
        destination,
        get_ship=True,
        offset=(30, 30),
        skip_first_screenshot=True,
    ):
        self.goto_calls.append(destination)
        if destination is page_settings_options:
            self.ui_current = self.options_result_page
        else:
            self.ui_current = destination

    def ui_goto_main(self):
        self.goto_main_calls += 1
        changed = self.ui_current not in (page_main, page_main_white)
        self.ui_current = self.main_result_page
        return changed


class GameSettingsNavigationTests(unittest.TestCase):
    def test_pages_use_shared_page_graph(self) -> None:
        self.assertIs(
            page_main_white.links[page_settings],
            GAME_SETTINGS_MAIN_GOTO_SETTINGS,
        )
        self.assertIs(
            page_settings.links[page_settings_options],
            GAME_SETTINGS_OPTIONS_UNSELECTED,
        )
        self.assertIs(page_settings.links[page_main], GOTO_MAIN)
        self.assertIs(page_settings_options.links[page_main], GOTO_MAIN)

        try:
            Page.init_connection(page_settings_options)
            self.assertIs(page_main_white.parent, page_settings)
            self.assertIs(page_settings.parent, page_settings_options)
            self.assertIsNone(page_settings_options.parent)
        finally:
            Page.clear_connection()

    def test_game_settings_assets_are_decodable_by_project_loader(self) -> None:
        asset_names = (
            "GAME_SETTINGS_MAIN_GOTO_SETTINGS.gif",
            "GAME_SETTINGS_MAIN_GOTO_SETTINGS.BUTTON.gif",
            "GAME_SETTINGS_OPTIONS_SELECTED.gif",
            "GAME_SETTINGS_OPTIONS_UNSELECTED.gif",
        )

        for asset_name in asset_names:
            with self.subTest(asset=asset_name):
                frames = imageio.mimread(ASSET_DIR / asset_name)
                self.assertGreaterEqual(len(frames), 1)
                self.assertEqual(frames[0].shape[:2], (720, 1280))

        fixture = imageio.imread(FIXTURE_DIR / "settings_options_selected_lower.png")
        self.assertEqual(fixture.shape[:2], (91, 101))

    def test_options_detector_distinguishes_settings_shell(self) -> None:
        self.assertLess(
            _button_similarity(
                GAME_SETTINGS_OPTIONS_UNSELECTED,
                GAME_SETTINGS_OPTIONS_SELECTED,
            ),
            0.85,
        )

    def test_options_detector_is_independent_of_vertical_position(self) -> None:
        self.assertGreaterEqual(
            _fixture_similarity(
                GAME_SETTINGS_OPTIONS_SELECTED,
                "settings_options_selected_lower.png",
            ),
            0.95,
        )

    def test_already_open_options_is_idempotent(self) -> None:
        scanner = _FakeNavigationScanner(page_settings_options)

        self.assertFalse(scanner.ensure_options_page())
        self.assertEqual(scanner.goto_calls, [])

    def test_settings_shell_routes_to_options_through_ui_goto(self) -> None:
        scanner = _FakeNavigationScanner(page_settings)

        self.assertTrue(scanner.ensure_options_page())
        self.assertEqual(scanner.goto_calls, [page_settings_options])
        self.assertEqual(scanner.ui_current, page_settings_options)

    def test_current_main_variant_routes_to_options_through_ui_goto(self) -> None:
        scanner = _FakeNavigationScanner(page_main_white)

        self.assertTrue(scanner.ensure_options_page())
        self.assertEqual(scanner.goto_calls, [page_settings_options])
        self.assertEqual(scanner.ui_current, page_settings_options)

    def test_options_navigation_fails_if_options_is_not_confirmed(self) -> None:
        scanner = _FakeNavigationScanner(
            page_settings,
            options_result_page=page_campaign,
        )

        with self.assertRaises(GamePageUnknownError):
            scanner.ensure_options_page()

        self.assertEqual(scanner.goto_calls, [page_settings_options])
        self.assertEqual(scanner.ui_current, page_campaign)

    def test_unverified_legacy_main_fails_closed(self) -> None:
        scanner = _FakeNavigationScanner(page_main)

        with self.assertRaises(RequestHumanTakeover):
            scanner.ensure_options_page()

        self.assertEqual(scanner.goto_calls, [])

    def test_unrelated_recognized_page_is_not_extended_into_scanner_recovery(self) -> None:
        scanner = _FakeNavigationScanner(page_campaign)

        with self.assertRaises(GamePageUnknownError):
            scanner.ensure_options_page()

        self.assertEqual(scanner.goto_calls, [])

    def test_return_to_main_uses_shared_ui_contract(self) -> None:
        scanner = _FakeNavigationScanner(page_settings_options)

        self.assertTrue(scanner.return_to_main())
        self.assertEqual(scanner.goto_main_calls, 1)
        self.assertEqual(scanner.ui_current, page_main_white)

    def test_return_to_main_fails_if_main_is_not_confirmed(self) -> None:
        scanner = _FakeNavigationScanner(
            page_settings_options,
            main_result_page=page_campaign,
        )

        with self.assertRaises(GamePageUnknownError):
            scanner.return_to_main()

        self.assertEqual(scanner.goto_main_calls, 1)
        self.assertEqual(scanner.ui_current, page_campaign)

    def test_scope_checker_resolves_import_aliases(self) -> None:
        imported_modules, call_names = _scope_signals(
            "from module.ui import click as tap\ntap()\n"
        )

        self.assertIn("click", imported_modules)
        self.assertIn("click", call_names)

    def test_navigation_has_no_blind_click_or_stage3_scope(self) -> None:
        files = [
            ROOT / "module" / "game_settings" / "scanner.py",
            ROOT / "module" / "game_settings" / "navigation.py",
        ]
        forbidden_calls = {"click", "sleep", "swipe", "drag"}
        imported_modules = []
        call_names = set()
        source_text = ""

        for file in files:
            text = file.read_text(encoding="utf-8")
            source_text += text
            file_imports, file_calls = _scope_signals(text)
            imported_modules.extend(file_imports)
            call_names.update(file_calls)

        self.assertTrue(forbidden_calls.isdisjoint(call_names), call_names)
        self.assertFalse(
            any("dock" in module.casefold() for module in imported_modules),
            imported_modules,
        )
        self.assertFalse(
            any("scroll" in module.casefold() for module in imported_modules),
            imported_modules,
        )
        self.assertNotIn("Custom Ship Names", source_text)


if __name__ == "__main__":
    unittest.main()
