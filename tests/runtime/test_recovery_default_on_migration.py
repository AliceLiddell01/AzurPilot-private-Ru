from __future__ import annotations

from copy import deepcopy

from module.config.deep import deep_get, deep_set
from module.config.recovery_default_on_migration import (
    RECOVERY_DEFAULT_ON_MIGRATION_KEY,
    RECOVERY_DEFAULT_ON_MIGRATION_VERSION,
    RECOVERY_DEFAULT_ON_STORAGE_PATH,
    apply_recovery_default_on_migration,
    recovery_default_on_migration_pending,
)


def _legacy_profile(game_stuck=False, adb_offline=False, storage=None):
    if storage is None:
        storage = {}
    return {
        "Alas": {
            "Error": {
                "GameStuckRestart": game_stuck,
                "AdbOfflineRestart": adb_offline,
            },
            "Storage": {
                "Storage": storage,
            },
        },
    }


def test_old_false_defaults_are_enabled_once():
    data = _legacy_profile()

    assert recovery_default_on_migration_pending(data) is True
    assert apply_recovery_default_on_migration(data) is True
    assert deep_get(data, "Alas.Error.GameStuckRestart") is True
    assert deep_get(data, "Alas.Error.AdbOfflineRestart") is True
    assert (
        deep_get(data, RECOVERY_DEFAULT_ON_STORAGE_PATH)[RECOVERY_DEFAULT_ON_MIGRATION_KEY]
        == RECOVERY_DEFAULT_ON_MIGRATION_VERSION
    )


def test_migration_is_idempotent():
    data = _legacy_profile()
    assert apply_recovery_default_on_migration(data) is True
    first = deepcopy(data)

    assert apply_recovery_default_on_migration(data) is False
    assert data == first


def test_user_opt_out_after_migration_is_preserved():
    data = _legacy_profile()
    assert apply_recovery_default_on_migration(data) is True
    deep_set(data, "Alas.Error.GameStuckRestart", False)
    deep_set(data, "Alas.Error.AdbOfflineRestart", False)

    assert recovery_default_on_migration_pending(data) is False
    assert apply_recovery_default_on_migration(data) is False
    assert deep_get(data, "Alas.Error.GameStuckRestart") is False
    assert deep_get(data, "Alas.Error.AdbOfflineRestart") is False


def test_missing_recovery_keys_are_populated_for_legacy_profile():
    data = {"Alas": {"Storage": {"Storage": {}}}}

    assert apply_recovery_default_on_migration(data) is True
    assert deep_get(data, "Alas.Error.GameStuckRestart") is True
    assert deep_get(data, "Alas.Error.AdbOfflineRestart") is True


def test_corrupt_storage_is_repaired_without_losing_default_on_rollout():
    data = _legacy_profile(storage="corrupt")

    assert apply_recovery_default_on_migration(data) is True
    storage = deep_get(data, RECOVERY_DEFAULT_ON_STORAGE_PATH)
    assert isinstance(storage, dict)
    assert storage[RECOVERY_DEFAULT_ON_MIGRATION_KEY] == RECOVERY_DEFAULT_ON_MIGRATION_VERSION


def test_empty_or_template_data_is_not_migrated():
    assert recovery_default_on_migration_pending({}) is False
    assert apply_recovery_default_on_migration({}) is False

    template = _legacy_profile()
    assert recovery_default_on_migration_pending(template, is_template=True) is False
    assert apply_recovery_default_on_migration(template, is_template=True) is False
