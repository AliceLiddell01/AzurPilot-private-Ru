from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from module.config.config import AzurLaneConfig
from module.config.deep import deep_get
from module.config.recovery_default_on_migration import (
    RECOVERY_DEFAULT_ON_MIGRATION_KEY,
    RECOVERY_DEFAULT_ON_MIGRATION_VERSION,
    RECOVERY_DEFAULT_ON_STORAGE_PATH,
)


def _make_config(data):
    config = object.__new__(AzurLaneConfig)
    object.__setattr__(config, 'bound', {})
    object.__setattr__(config, 'modified', {})
    object.__setattr__(config, 'overridden', {})
    object.__setattr__(config, 'config_name', 'migration-test')
    object.__setattr__(config, 'is_template_config', False)
    object.__setattr__(config, 'read_file', Mock(return_value=data))
    object.__setattr__(config, 'write_file', Mock())
    object.__setattr__(config, 'config_override', Mock())
    return config


def _legacy_data(*, marker=False, game_stuck=False, adb_offline=False):
    storage = {}
    if marker:
        storage[RECOVERY_DEFAULT_ON_MIGRATION_KEY] = RECOVERY_DEFAULT_ON_MIGRATION_VERSION
    return {
        'Alas': {
            'Error': {
                'GameStuckRestart': game_stuck,
                'AdbOfflineRestart': adb_offline,
            },
            'Storage': {'Storage': storage},
        },
    }


def test_existing_profile_is_persisted_once_with_marker_and_default_on_values():
    config = _make_config(_legacy_data())

    with tempfile.TemporaryDirectory() as tmp:
        existing = Path(tmp) / 'migration-test.json'
        existing.write_text('{}', encoding='utf-8')
        with patch('module.config.config.filepath_config', return_value=str(existing)):
            config.load()

    assert deep_get(config.data, 'Alas.Error.GameStuckRestart') is True
    assert deep_get(config.data, 'Alas.Error.AdbOfflineRestart') is True
    storage = deep_get(config.data, RECOVERY_DEFAULT_ON_STORAGE_PATH)
    assert storage[RECOVERY_DEFAULT_ON_MIGRATION_KEY] == RECOVERY_DEFAULT_ON_MIGRATION_VERSION
    config.write_file.assert_called_once_with('migration-test', data=config.data)


def test_post_migration_user_opt_out_does_not_trigger_another_write():
    config = _make_config(_legacy_data(marker=True, game_stuck=False, adb_offline=False))

    with tempfile.TemporaryDirectory() as tmp:
        existing = Path(tmp) / 'migration-test.json'
        existing.write_text('{}', encoding='utf-8')
        with patch('module.config.config.filepath_config', return_value=str(existing)):
            config.load()

    assert deep_get(config.data, 'Alas.Error.GameStuckRestart') is False
    assert deep_get(config.data, 'Alas.Error.AdbOfflineRestart') is False
    config.write_file.assert_not_called()


def test_missing_profile_is_not_created_by_migration_load_boundary():
    config = _make_config(_legacy_data())

    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / 'missing.json'
        with patch('module.config.config.filepath_config', return_value=str(missing)):
            config.load()

    config.write_file.assert_not_called()


def test_legacy_emotion_scan_runs_only_once_per_config_instance():
    config = _make_config(_legacy_data(marker=True))

    with tempfile.TemporaryDirectory() as tmp:
        existing = Path(tmp) / 'migration-test.json'
        existing.write_text('{}', encoding='utf-8')
        with (
            patch('module.config.config.filepath_config', return_value=str(existing)),
            patch(
                'module.config.config.legacy_emotion_state_present',
                wraps=lambda data: False,
            ) as legacy_scan,
        ):
            config.load()
            config.load()

    legacy_scan.assert_called_once_with({})
