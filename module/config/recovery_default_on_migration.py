"""Одноразовая миграция политики unattended emulator recovery Stage 3.

До Stage 3 оба recovery-switch хранились как обычные bool без provenance, поэтому
невозможно отличить старый default ``false`` от явного пользовательского opt-out.
Миграция один раз включает обе политики в существующем профиле и сохраняет
version marker во внутреннем ``Alas.Storage.Storage``. После marker обычный
config lifecycle больше не меняет пользовательские значения.
"""

from __future__ import annotations

from module.config.deep import deep_get, deep_set


RECOVERY_DEFAULT_ON_MIGRATION_VERSION = 1
RECOVERY_DEFAULT_ON_MIGRATION_KEY = "RecoveryDefaultOnMigrationVersion"
RECOVERY_DEFAULT_ON_STORAGE_PATH = "Alas.Storage.Storage"


def recovery_default_on_migration_pending(data: dict, *, is_template: bool = False) -> bool:
    """Вернуть ``True`` только для существующего профиля до Stage 3 migration."""
    if is_template or not isinstance(data, dict) or not data:
        return False

    storage = deep_get(data, RECOVERY_DEFAULT_ON_STORAGE_PATH, default={})
    if not isinstance(storage, dict):
        return True

    raw_version = storage.get(RECOVERY_DEFAULT_ON_MIGRATION_KEY, 0)
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        version = 0
    return version < RECOVERY_DEFAULT_ON_MIGRATION_VERSION


def apply_recovery_default_on_migration(data: dict, *, is_template: bool = False) -> bool:
    """Один раз включить Stage 3 recovery и сохранить idempotency marker.

    Возвращает ``True``, когда данные были изменены. Marker хранится в уже
    существующем внутреннем storage namespace и не создаёт пользовательскую
    настройку WebUI.
    """
    if not recovery_default_on_migration_pending(data, is_template=is_template):
        return False

    deep_set(data, "Alas.Error.GameStuckRestart", True)
    deep_set(data, "Alas.Error.AdbOfflineRestart", True)

    storage = deep_get(data, RECOVERY_DEFAULT_ON_STORAGE_PATH, default={})
    storage = dict(storage) if isinstance(storage, dict) else {}
    storage[RECOVERY_DEFAULT_ON_MIGRATION_KEY] = RECOVERY_DEFAULT_ON_MIGRATION_VERSION
    deep_set(data, RECOVERY_DEFAULT_ON_STORAGE_PATH, storage)
    return True
