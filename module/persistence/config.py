"""Структурная конфигурация подключения без логируемого raw DSN."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import URL

from module.application.errors import StorageConfigurationError
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD

DEFAULT_BACKEND_MARKER_PATH = Path("config/state/storage_backend.json")
LEGACY_BACKEND_MARKER_PATH = Path("config/storage_backend.json")


def _read_backend_marker(path: Path) -> tuple[bytes, dict[str, object]]:
    try:
        if path.is_symlink() or not path.is_file():
            raise StorageConfigurationError(
                "Production backend marker отсутствует или небезопасен."
            )
        raw = path.read_bytes()
        payload = json.loads(raw)
    except StorageConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageConfigurationError(
            "Production backend marker повреждён."
        ) from exc
    if not isinstance(payload, dict):
        raise StorageConfigurationError("Production backend marker некорректен.")
    return raw, payload


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
        cls, marker_path: str | Path = DEFAULT_BACKEND_MARKER_PATH
    ) -> DatabaseSettings:
        """Загрузить единственный production-маркер без секретных значений."""

        _, payload = _read_backend_marker(Path(marker_path))
        return cls.from_backend_marker_payload(payload)

    @classmethod
    def from_backend_marker_payload(
        cls, payload: dict[str, object]
    ) -> DatabaseSettings:
        if payload.get("backend") != "postgresql" or payload.get("version") != 1:
            raise StorageConfigurationError(
                "Production backend marker не разрешает PostgreSQL runtime."
            )
        if payload.get("alembic_head") != EXPECTED_ALEMBIC_HEAD:
            raise StorageConfigurationError(
                "Production backend marker содержит несовместимый schema head."
            )
        reconciliation_report = payload.get("reconciliation_report_sha256")
        if (
            not isinstance(reconciliation_report, str)
            or len(reconciliation_report) != 64
            or any(
                character not in "0123456789abcdef"
                for character in reconciliation_report
            )
        ):
            raise StorageConfigurationError(
                "Маркер боевого хранилища не содержит происхождение отчёта сверки."
            )
        for field_name in ("reviewed_head", "merge_commit"):
            revision = payload.get(field_name)
            if (
                not isinstance(revision, str)
                or len(revision) != 40
                or any(character not in "0123456789abcdef" for character in revision)
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


def migrate_legacy_backend_marker(
    *,
    target: str | Path = DEFAULT_BACKEND_MARKER_PATH,
    legacy: str | Path = LEGACY_BACKEND_MARKER_PATH,
) -> bool:
    """Перенести только валидный legacy marker без перезаписи canonical state."""

    target_path = Path(target)
    legacy_path = Path(legacy)
    if not legacy_path.exists() and not legacy_path.is_symlink():
        return False

    legacy_raw, legacy_payload = _read_backend_marker(legacy_path)
    DatabaseSettings.from_backend_marker_payload(legacy_payload)

    def finish_existing_target(*, remove_on_failure: bool) -> bool:
        try:
            target_raw, target_payload = _read_backend_marker(target_path)
            DatabaseSettings.from_backend_marker_payload(target_payload)
            target_stat = target_path.stat()
            try:
                legacy_stat = legacy_path.stat()
            except FileNotFoundError:
                if target_raw == legacy_raw:
                    return True
                if remove_on_failure:
                    target_path.unlink(missing_ok=True)
                    raise StorageConfigurationError(
                        "Production backend marker изменился во время переноса."
                    )
                return False
            if (
                target_raw != legacy_raw
                or target_stat.st_dev != legacy_stat.st_dev
                or target_stat.st_ino != legacy_stat.st_ino
            ):
                if remove_on_failure:
                    target_path.unlink(missing_ok=True)
                    raise StorageConfigurationError(
                        "Production backend marker изменился во время переноса."
                    )
                return False
            legacy_path.unlink(missing_ok=True)
        except (OSError, StorageConfigurationError) as exc:
            if remove_on_failure:
                target_path.unlink(missing_ok=True)
            if isinstance(exc, StorageConfigurationError):
                raise
            raise StorageConfigurationError(
                "Не удалось завершить перенос production backend marker."
            ) from exc
        return True

    if target_path.exists() or target_path.is_symlink():
        return finish_existing_target(remove_on_failure=False)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_path.hardlink_to(legacy_path)
    except FileExistsError:
        return finish_existing_target(remove_on_failure=False)
    except OSError as exc:
        raise StorageConfigurationError(
            "Не удалось безопасно перенести production backend marker."
        ) from exc

    return finish_existing_target(remove_on_failure=True)
