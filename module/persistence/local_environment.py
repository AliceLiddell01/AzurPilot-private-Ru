"""Единый owner локального `.env` для production PostgreSQL."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

from module.application.errors import StorageConfigurationError
from module.persistence.config import DatabaseSettings

DEFAULT_LOCAL_ENV_PATH = Path(".env")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_APP_PREFIX = "AZURPILOT_POSTGRES_"
_MIGRATOR_PREFIX = "AZURPILOT_POSTGRES_MIGRATOR_"
_OBSERVABILITY_PREFIX = "AZURPILOT_OBSERVABILITY_"
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
# Приложенный recovery contract требует оба секрета в защищённом local source;
# loader валидирует их различие, но никогда не экспортирует в process environment.
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

    def __post_init__(self) -> None:
        if frozenset(self.values) != _ALLOWED_KEYS:
            raise StorageConfigurationError(
                "Локальный PostgreSQL env не содержит полный production contract."
            )

    def install(
        self,
        *,
        role: str = "app",
        environment: MutableMapping[str, str] | None = None,
    ) -> None:
        """Установить metadata и passfile без экспорта паролей.

        Для ``role="migrator"`` канонические app-переменные заменяются
        migrator-контрактом, поэтому последующий ``DatabaseSettings.from_environment()``
        создаёт migrator-подключение. ``PGPASSWORD`` и оба password-ключа удаляются,
        а ``PGPASSFILE`` указывает на passfile выбранной роли.
        Замена канонических app-переменных необратима для текущего процесса:
        вызывающий код не должен ожидать восстановления прежних значений или
        повторно использовать это environment для другой роли.
        """

        if role not in {"app", "migrator"}:
            raise StorageConfigurationError("Роль локального PostgreSQL env некорректна.")
        target = os.environ if environment is None else environment
        for key, value in self.values.items():
            if key not in _SECRET_KEYS:
                target[key] = value
        source_prefix = _APP_PREFIX if role == "app" else _MIGRATOR_PREFIX
        for field_name in _CONNECTION_FIELDS:
            if field_name == "PASSWORD":
                continue
            target[_APP_PREFIX + field_name] = self.values[source_prefix + field_name]
        target["PGPASSFILE"] = self.values[source_prefix + "PGPASSFILE"]
        target.pop("PGPASSWORD", None)
        target.pop(_APP_PREFIX + "PASSWORD", None)
        target.pop(_MIGRATOR_PREFIX + "PASSWORD", None)

    def require_app_runtime_match(self, settings: DatabaseSettings) -> None:
        if (
            settings.host != self.values[_APP_PREFIX + "HOST"]
            or settings.port != int(self.values[_APP_PREFIX + "PORT"])
            or settings.database != self.values[_APP_PREFIX + "DATABASE"]
            or settings.user != self.values[_APP_PREFIX + "USER"]
            or settings.sslmode != self.values[_APP_PREFIX + "SSLMODE"]
            or settings.runtime_timezone
            != self.values[_APP_PREFIX + "RUNTIME_TIMEZONE"]
        ):
            raise StorageConfigurationError(
                "Локальный PostgreSQL env не совпадает с production marker."
            )

    @property
    def app_passfile(self) -> str:
        """Вернуть путь app passfile без раскрытия содержимого секрета."""

        return self.values[_APP_PREFIX + "PGPASSFILE"]


def _parse_value(raw: str, line_number: int) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if (
        not value
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or " #" in value
    ):
        raise StorageConfigurationError(
            f"Значение PostgreSQL env в строке {line_number} некорректно."
        )
    return value


def _windows_acl_is_restricted(path: Path) -> bool | None:
    shell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if shell is None:
        return None
    script = """
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:AZURPILOT_ENV_ACL_PATH
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$payload = [pscustomobject]@{
    CurrentSid = $current
    OwnerSid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    Protected = $acl.AreAccessRulesProtected
    Rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) | ForEach-Object {
        [pscustomobject]@{
            Sid = $_.IdentityReference.Value
            Rights = [int]$_.FileSystemRights
            Type = $_.AccessControlType.ToString()
            Inherited = $_.IsInherited
        }
    })
}
$payload | ConvertTo-Json -Compress -Depth 4
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    environment = os.environ.copy()
    environment.pop("PGPASSWORD", None)
    environment.pop(_APP_PREFIX + "PASSWORD", None)
    environment.pop(_MIGRATOR_PREFIX + "PASSWORD", None)
    environment["AZURPILOT_ENV_ACL_PATH"] = str(path)
    try:
        completed = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
            text=True,
            encoding="utf-8-sig",
        )
        if completed.returncode != 0:
            return False
        payload = json.loads(completed.stdout)
        current_sid = payload["CurrentSid"]
        rules = payload["Rules"]
        if isinstance(rules, dict):
            rules = [rules]
        if (
            payload["OwnerSid"] != current_sid
            or payload["Protected"] is not True
            or not isinstance(rules, list)
        ):
            return False
        allowed_sids = {current_sid, "S-1-5-18"}
        current_full_control = False
        for rule in rules:
            if (
                not isinstance(rule, dict)
                or rule.get("Sid") not in allowed_sids
                or rule.get("Type") != "Allow"
                or rule.get("Inherited") is not False
                or not isinstance(rule.get("Rights"), int)
            ):
                return False
            if rule["Sid"] == current_sid and rule["Rights"] & 0x1F01FF == 0x1F01FF:
                current_full_control = True
        return current_full_control
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        return False


def _require_secure_permissions(path: Path, metadata: os.stat_result) -> None:
    if os.name == "nt":
        secure = _windows_acl_is_restricted(path)
        if secure is None:
            raise StorageConfigurationError(
                "ACL локального PostgreSQL env невозможно проверить: "
                "установите или включите PowerShell."
            )
    else:
        secure = metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    if not secure:
        raise StorageConfigurationError(
            "Локальный PostgreSQL env имеет небезопасные права доступа."
        )


def read_local_postgres_environment(
    path: str | Path = DEFAULT_LOCAL_ENV_PATH,
) -> LocalPostgresEnvironment | None:
    env_path = Path(path)
    if not env_path.exists():
        if env_path.is_symlink():
            raise StorageConfigurationError(
                "Локальный PostgreSQL env отсутствует или небезопасен."
            )
        return None
    try:
        metadata = env_path.stat()
        if env_path.is_symlink() or not env_path.is_file() or metadata.st_size > 65_536:
            raise StorageConfigurationError(
                "Локальный PostgreSQL env отсутствует или небезопасен."
            )
        _require_secure_permissions(env_path, metadata)
        lines = env_path.read_text(encoding="utf-8").splitlines()
        final_metadata = env_path.stat()
        if (
            env_path.is_symlink()
            or metadata.st_dev != final_metadata.st_dev
            or metadata.st_ino != final_metadata.st_ino
            or metadata.st_size != final_metadata.st_size
            or metadata.st_mtime_ns != final_metadata.st_mtime_ns
        ):
            raise StorageConfigurationError(
                "Локальный PostgreSQL env изменился во время чтения."
            )
    except StorageConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise StorageConfigurationError(
            "Локальный PostgreSQL env невозможно прочитать."
        ) from exc

    values: dict[str, str] = {}
    seen_keys: set[str] = set()
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
        if not _KEY_RE.fullmatch(key) or key in seen_keys:
            raise StorageConfigurationError(
                f"Ключ локального PostgreSQL env в строке {line_number} некорректен."
            )
        seen_keys.add(key)
        if key in _ALLOWED_KEYS:
            values[key] = _parse_value(raw_value, line_number)
        elif key.startswith(_OBSERVABILITY_PREFIX) and len(key) > len(
            _OBSERVABILITY_PREFIX
        ):
            # Compose и production PostgreSQL используют один защищённый local
            # env. Чужой observability namespace валидируем синтаксически, но
            # не включаем в PostgreSQL contract и не экспортируем приложению.
            _parse_value(raw_value, line_number)
        else:
            raise StorageConfigurationError(
                f"Ключ локального PostgreSQL env в строке {line_number} некорректен."
            )

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
    for prefix, expected_user in (
        (_APP_PREFIX, "azurpilot_app"),
        (_MIGRATOR_PREFIX, "azurpilot_migrator"),
    ):
        if values[prefix + "USER"] != expected_user:
            raise StorageConfigurationError(
                "Роль в локальном PostgreSQL env не соответствует production contract."
            )
    for field_name in (
        "HOST",
        "PORT",
        "DATABASE",
        "SSLMODE",
        "RUNTIME_TIMEZONE",
        "PGPASSFILE",
    ):
        if values[_APP_PREFIX + field_name] != values[_MIGRATOR_PREFIX + field_name]:
            raise StorageConfigurationError(
                "App и migrator используют разные PostgreSQL endpoints."
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
