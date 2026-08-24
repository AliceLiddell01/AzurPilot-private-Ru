"""Единый owner локального `.env` для production PostgreSQL."""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

from module.application.errors import StorageConfigurationError

DEFAULT_LOCAL_ENV_PATH = Path(".env")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_APP_PREFIX = "AZURPILOT_POSTGRES_"
_MIGRATOR_PREFIX = "AZURPILOT_POSTGRES_MIGRATOR_"
_CONNECTION_FIELDS = (
    "HOST",
    "PORT",
    "DATABASE",
    "USER",
    "PASSWORD",
    "SSLMODE",
    "RUNTIME_TIMEZONE",
    "PGPASSFILE",
)
_ALLOWED_KEYS = frozenset(
    {
        *(f"{_APP_PREFIX}{name}" for name in _CONNECTION_FIELDS),
        *(f"{_MIGRATOR_PREFIX}{name}" for name in _CONNECTION_FIELDS),
        "AZURPILOT_WSL_DISTRO",
        "AZURPILOT_WSL_PGPASSFILE",
    }
)
_SECRET_KEYS = frozenset(
    {
        f"{_APP_PREFIX}PASSWORD",
        f"{_MIGRATOR_PREFIX}PASSWORD",
    }
)


@dataclass(frozen=True, slots=True)
class LocalPostgresEnvironment:
    path: Path
    values: dict[str, str] = field(repr=False)

    def install(
        self,
        *,
        role: str = "app",
        environment: MutableMapping[str, str] | None = None,
    ) -> None:
        if role not in {"app", "migrator"}:
            raise StorageConfigurationError("Роль локального PostgreSQL env некорректна.")
        target = os.environ if environment is None else environment
        for key, value in self.values.items():
            if key not in _SECRET_KEYS:
                target[key] = value
        source_prefix = _APP_PREFIX if role == "app" else _MIGRATOR_PREFIX
        for field_name in _CONNECTION_FIELDS:
            if field_name in {"PASSWORD", "PGPASSFILE"}:
                continue
            target[_APP_PREFIX + field_name] = self.values[source_prefix + field_name]
        target["PGPASSFILE"] = self.values[source_prefix + "PGPASSFILE"]
        target.pop("PGPASSWORD", None)
        target.pop(_APP_PREFIX + "PASSWORD", None)
        target.pop(_MIGRATOR_PREFIX + "PASSWORD", None)

    def require_runtime_match(self, settings: object) -> None:
        expected = {
            "host": self.values[_APP_PREFIX + "HOST"],
            "port": int(self.values[_APP_PREFIX + "PORT"]),
            "database": self.values[_APP_PREFIX + "DATABASE"],
            "user": self.values[_APP_PREFIX + "USER"],
            "sslmode": self.values[_APP_PREFIX + "SSLMODE"],
            "runtime_timezone": self.values[_APP_PREFIX + "RUNTIME_TIMEZONE"],
        }
        if any(getattr(settings, key, None) != value for key, value in expected.items()):
            raise StorageConfigurationError(
                "Локальный PostgreSQL env не совпадает с production marker."
            )


def _parse_value(raw: str, line_number: int) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise StorageConfigurationError(
            f"Значение PostgreSQL env в строке {line_number} некорректно."
        )
    return value


def read_local_postgres_environment(
    path: str | Path = DEFAULT_LOCAL_ENV_PATH,
) -> LocalPostgresEnvironment | None:
    env_path = Path(path)
    if not env_path.exists():
        return None
    try:
        if env_path.is_symlink() or not env_path.is_file() or env_path.stat().st_size > 65_536:
            raise StorageConfigurationError(
                "Локальный PostgreSQL env отсутствует или небезопасен."
            )
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except StorageConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise StorageConfigurationError(
            "Локальный PostgreSQL env невозможно прочитать."
        ) from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise StorageConfigurationError(
                f"Строка {line_number} локального PostgreSQL env некорректна."
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key) or key not in _ALLOWED_KEYS or key in values:
            raise StorageConfigurationError(
                f"Ключ локального PostgreSQL env в строке {line_number} некорректен."
            )
        values[key] = _parse_value(raw_value, line_number)

    if _ALLOWED_KEYS.difference(values):
        raise StorageConfigurationError(
            "Локальный PostgreSQL env не содержит полный production contract."
        )
    for prefix in (_APP_PREFIX, _MIGRATOR_PREFIX):
        try:
            port = int(values[prefix + "PORT"])
        except ValueError as exc:
            raise StorageConfigurationError(
                "Порт в локальном PostgreSQL env некорректен."
            ) from exc
        if not 1 <= port <= 65535:
            raise StorageConfigurationError(
                "Порт в локальном PostgreSQL env некорректен."
            )
    if values[_APP_PREFIX + "PASSWORD"] == values[_MIGRATOR_PREFIX + "PASSWORD"]:
        raise StorageConfigurationError(
            "App и migrator должны использовать разные PostgreSQL secrets."
        )
    return LocalPostgresEnvironment(path=env_path, values=values)


def load_local_postgres_environment(
    path: str | Path = DEFAULT_LOCAL_ENV_PATH,
    *,
    role: str = "app",
    environment: MutableMapping[str, str] | None = None,
) -> LocalPostgresEnvironment | None:
    local = read_local_postgres_environment(path)
    if local is not None:
        local.install(role=role, environment=environment)
    return local
