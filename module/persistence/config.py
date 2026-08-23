"""Структурная конфигурация подключения без логируемого raw DSN."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import URL

from module.application.errors import StorageConfigurationError


@dataclass(frozen=True, slots=True)
class PoolSettings:
    size: int = 2
    max_overflow: int = 1
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.size < 1 or self.size > 8:
            raise StorageConfigurationError("Размер пула должен быть от 1 до 8.")
        if self.max_overflow < 0 or self.max_overflow > 8:
            raise StorageConfigurationError("Overflow пула должен быть от 0 до 8.")
        if not 0 < self.timeout_seconds <= 60:
            raise StorageConfigurationError(
                "Timeout пула должен быть от 0 до 60 секунд."
            )


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    host: str
    port: int
    database: str
    user: str
    password: str | None = field(default=None, repr=False)
    connect_timeout_seconds: int = 5
    sslmode: str = "verify-full"
    runtime_timezone: str = "UTC"
    pool: PoolSettings = field(default_factory=PoolSettings)

    def __post_init__(self) -> None:
        for label, value in (
            ("host", self.host),
            ("database", self.database),
            ("user", self.user),
        ):
            if not value or any(char.isspace() for char in value):
                raise StorageConfigurationError(f"Поле {label} некорректно.")
        if not 1 <= self.port <= 65535:
            raise StorageConfigurationError("Порт PostgreSQL некорректен.")
        if not 1 <= self.connect_timeout_seconds <= 60:
            raise StorageConfigurationError("Connect timeout некорректен.")
        if self.sslmode not in {
            "disable",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        }:
            raise StorageConfigurationError("sslmode не поддерживается.")
        try:
            ZoneInfo(self.runtime_timezone)
        except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            raise StorageConfigurationError(
                "Часовой пояс production runtime некорректен."
            ) from exc

    def sqlalchemy_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
        )

    def connect_args(self) -> dict[str, object]:
        args: dict[str, object] = {"connect_timeout": self.connect_timeout_seconds}
        args["sslmode"] = self.sslmode
        return args

    @classmethod
    def from_environment(cls, prefix: str = "AZURPILOT_POSTGRES_") -> DatabaseSettings:
        def required(name: str) -> str:
            value = environ.get(prefix + name)
            if not value:
                raise StorageConfigurationError(
                    f"Переменная {prefix + name} не задана."
                )
            return value

        try:
            port = int(environ.get(prefix + "PORT", "5432"))
        except ValueError as exc:
            raise StorageConfigurationError("Порт PostgreSQL некорректен.") from exc
        return cls(
            host=required("HOST"),
            port=port,
            database=required("DATABASE"),
            user=required("USER"),
            password=environ.get(prefix + "PASSWORD") or None,
            sslmode=environ.get(prefix + "SSLMODE") or "verify-full",
            runtime_timezone=environ.get(prefix + "RUNTIME_TIMEZONE") or "UTC",
        )

    @classmethod
    def from_backend_marker(
        cls, marker_path: str | Path = "config/storage_backend.json"
    ) -> DatabaseSettings:
        """Загрузить единственный production-маркер без секретных значений."""

        path = Path(marker_path)
        try:
            if path.is_symlink() or not path.is_file():
                raise StorageConfigurationError(
                    "Production backend marker отсутствует или небезопасен."
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
        except StorageConfigurationError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StorageConfigurationError(
                "Production backend marker повреждён."
            ) from exc
        if not isinstance(payload, dict):
            raise StorageConfigurationError("Production backend marker некорректен.")
        if payload.get("backend") != "postgresql" or payload.get("version") != 1:
            raise StorageConfigurationError(
                "Production backend marker не разрешает PostgreSQL runtime."
            )
        if payload.get("alembic_head") != "0002_migration_shapes":
            raise StorageConfigurationError(
                "Production backend marker содержит несовместимый schema head."
            )
        manifest = payload.get("migration_manifest_sha256")
        if not isinstance(manifest, str) or len(manifest) != 64 or any(
            character not in "0123456789abcdef" for character in manifest
        ):
            raise StorageConfigurationError(
                "Production backend marker не содержит migration provenance."
            )
        for field_name in ("reviewed_head", "merge_commit"):
            revision = payload.get(field_name)
            if not isinstance(revision, str) or len(revision) != 40 or any(
                character not in "0123456789abcdef" for character in revision
            ):
                raise StorageConfigurationError(
                    "Production backend marker не содержит проверенный Git provenance."
                )
        try:
            port = int(payload["port"])
            host = str(payload["host"])
            database = str(payload["database"])
            user = str(payload["user"])
            sslmode = str(payload["sslmode"])
            runtime_timezone = str(payload["runtime_timezone"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageConfigurationError(
                "Production backend marker неполон."
            ) from exc
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise StorageConfigurationError(
                "Production PostgreSQL должен использовать loopback listener."
            )
        if user != "azurpilot_app":
            raise StorageConfigurationError(
                "Production runtime должен использовать только app-роль PostgreSQL."
            )
        return cls(
            host=host,
            port=port,
            database=database,
            user=user,
            password=None,
            sslmode=sslmode,
            runtime_timezone=runtime_timezone,
        )
