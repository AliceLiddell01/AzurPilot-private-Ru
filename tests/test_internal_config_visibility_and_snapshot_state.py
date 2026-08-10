from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from module.game_settings.model import GameSettingsScanResult
from module.game_settings.registry import GAME_SETTINGS_OPTIONS_REGISTRY
from module.game_settings.snapshot import (
    DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
    LEGACY_GAME_SETTINGS_SNAPSHOT_PATH,
    GameSettingsSnapshotStatus,
    load_game_settings_snapshot,
    save_game_settings_snapshot,
)
from module.webui.instance_visibility import (
    WEBUI_HIDDEN_INSTANCE_NAMES,
    is_webui_hidden_instance,
    visible_webui_instances,
)


ROOT = Path(__file__).resolve().parents[1]


def _canonical_result() -> GameSettingsScanResult:
    results = []
    for entry in GAME_SETTINGS_OPTIONS_REGISTRY:
        requirement = entry.requirement
        if requirement is None:
            raise AssertionError(f"Production requirement отсутствует: {entry.key}")
        results.append(entry.make_result(requirement.expected_value))
    return GameSettingsScanResult(results)


class SnapshotStateNamespaceTests(unittest.TestCase):
    def test_default_snapshot_path_uses_nested_runtime_state_namespace(self):
        self.assertEqual(
            DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
            Path("config/state/game_settings_snapshot.json"),
        )
        self.assertEqual(
            LEGACY_GAME_SETTINGS_SNAPSHOT_PATH,
            Path("config/game_settings_snapshot.json"),
        )

    def test_default_save_writes_only_new_runtime_state_path(self):
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            try:
                os.chdir(temp)
                save_game_settings_snapshot(_canonical_result())
                self.assertTrue(DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH.is_file())
                self.assertFalse(LEGACY_GAME_SETTINGS_SNAPSHOT_PATH.exists())
                loaded = load_game_settings_snapshot()
                self.assertIs(loaded.status, GameSettingsSnapshotStatus.VALID)
                self.assertEqual(loaded.path, DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH)
            finally:
                os.chdir(previous_cwd)

    def test_default_load_migrates_legacy_snapshot_before_reading(self):
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            try:
                os.chdir(temp)
                save_game_settings_snapshot(
                    _canonical_result(),
                    path=LEGACY_GAME_SETTINGS_SNAPSHOT_PATH,
                )
                before = LEGACY_GAME_SETTINGS_SNAPSHOT_PATH.read_bytes()

                loaded = load_game_settings_snapshot()

                self.assertIs(loaded.status, GameSettingsSnapshotStatus.VALID)
                self.assertTrue(DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH.is_file())
                self.assertEqual(DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH.read_bytes(), before)
                self.assertFalse(LEGACY_GAME_SETTINGS_SNAPSHOT_PATH.exists())
            finally:
                os.chdir(previous_cwd)

    def test_existing_new_snapshot_wins_over_legacy_file(self):
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            try:
                os.chdir(temp)
                save_game_settings_snapshot(
                    _canonical_result(),
                    path=DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
                )
                current = DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH.read_bytes()
                save_game_settings_snapshot(
                    _canonical_result(),
                    path=LEGACY_GAME_SETTINGS_SNAPSHOT_PATH,
                )

                loaded = load_game_settings_snapshot()

                self.assertIs(loaded.status, GameSettingsSnapshotStatus.VALID)
                self.assertEqual(DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH.read_bytes(), current)
                self.assertTrue(LEGACY_GAME_SETTINGS_SNAPSHOT_PATH.is_file())
            finally:
                os.chdir(previous_cwd)

    def test_migration_failure_preserves_legacy_file_and_fails_as_missing(self):
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            try:
                os.chdir(temp)
                save_game_settings_snapshot(
                    _canonical_result(),
                    path=LEGACY_GAME_SETTINGS_SNAPSHOT_PATH,
                )
                before = LEGACY_GAME_SETTINGS_SNAPSHOT_PATH.read_bytes()

                with patch(
                    "module.game_settings.snapshot.atomic_replace",
                    side_effect=OSError("replace failed"),
                ):
                    loaded = load_game_settings_snapshot()

                self.assertIs(loaded.status, GameSettingsSnapshotStatus.MISSING)
                self.assertFalse(DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH.exists())
                self.assertEqual(LEGACY_GAME_SETTINGS_SNAPSHOT_PATH.read_bytes(), before)
            finally:
                os.chdir(previous_cwd)

    def test_nested_runtime_state_path_is_gitignored(self):
        completed = subprocess.run(
            [
                "git",
                "check-ignore",
                "--quiet",
                "--",
                "config/state/game_settings_snapshot.json",
            ],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)


class WebUIInternalInstanceTests(unittest.TestCase):
    def test_ap_and_legacy_snapshot_names_are_hidden_but_not_deleted(self):
        self.assertEqual(
            WEBUI_HIDDEN_INSTANCE_NAMES,
            frozenset({"ap", "game_settings_snapshot"}),
        )
        self.assertTrue(is_webui_hidden_instance("ap"))
        self.assertTrue(is_webui_hidden_instance("AP"))
        self.assertTrue(is_webui_hidden_instance("game_settings_snapshot"))
        self.assertTrue(is_webui_hidden_instance("Game_Settings_Snapshot"))
        self.assertFalse(is_webui_hidden_instance("alas"))
        self.assertEqual(
            visible_webui_instances(
                ["alas", "AP", "game_settings_snapshot", "alas2"]
            ),
            ["alas", "alas2"],
        )

    def test_webui_dependency_boundary_filters_core_instances(self):
        import module.webui.app_dependencies as dependencies

        with patch.object(
            dependencies,
            "_all_alas_instances",
            return_value=["alas", "ap", "game_settings_snapshot", "alas2"],
        ):
            self.assertEqual(dependencies.alas_instance(), ["alas", "alas2"])
            self.assertFalse(dependencies.is_oobe_needed())

        with patch.object(dependencies, "_all_alas_instances", return_value=["ap"]):
            self.assertEqual(dependencies.alas_instance(), [])
            self.assertTrue(dependencies.is_oobe_needed())

    def test_personal_oobe_creates_user_visible_alas_not_smoke_ap(self):
        from module.webui.oobe import OOBEWizard

        wizard = OOBEWizard(object())
        self.assertEqual(wizard.config_name, "alas")


if __name__ == "__main__":
    unittest.main()
