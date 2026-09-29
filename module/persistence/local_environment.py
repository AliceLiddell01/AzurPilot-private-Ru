"""Единый владелец локального файла `.env` для PostgreSQL рабочей среды."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

from module.application.errors import (
    StorageConfigurationError,
    StorageConfigurationUnknownError,
)
from module.persistence.config import DatabaseSettings
from module.persistence.local_environment_schema import (
    INFRASTRUCTURE_ENVIRONMENT_KEYS,
    LOCAL_ENVIRONMENT_KEYS,
    POSTGRES_ENVIRONMENT_KEYS,
    SECRET_ENVIRONMENT_KEYS,
    get_local_environment_key,
)

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
_ALLOWED_KEYS = POSTGRES_ENVIRONMENT_KEYS | frozenset(
    {
        "AZURPILOT_WSL_DISTRO",
        "AZURPILOT_WSL_PGPASSFILE",
    }
)
# Контракт восстановления требует оба секрета в защищённом локальном источнике;
# загрузчик проверяет их различие, но никогда не экспортирует в окружение процесса.
_SECRET_KEYS = SECRET_ENVIRONMENT_KEYS & _ALLOWED_KEYS


@dataclass(frozen=True, slots=True)
class LocalPostgresEnvironment:
    path: Path
    values: dict[str, str] = field(repr=False)
    infrastructure_values: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if frozenset(self.values) != _ALLOWED_KEYS:
            raise StorageConfigurationError(
                "Локальное окружение PostgreSQL не содержит полного производственного контракта."
            )
        if not frozenset(self.infrastructure_values).issubset(
            INFRASTRUCTURE_ENVIRONMENT_KEYS
        ):
            raise StorageConfigurationError(
                "Локальное окружение содержит неизвестный инфраструктурный ключ."
            )

    def install(
        self,
        *,
        role: str = "app",
        environment: MutableMapping[str, str] | None = None,
    ) -> None:
        """Установить метаданные и файл паролей, не экспортируя пароли.

        Для ``role="migrator"`` канонические переменные приложения заменяются
        параметрами мигратора, поэтому последующий вызов
        ``DatabaseSettings.from_environment()`` создаёт подключение мигратора.
        ``PGPASSWORD`` и оба ключа пароля удаляются, а ``PGPASSFILE`` указывает
        на файл паролей выбранной роли. Замена канонических переменных приложения
        необратима для текущего процесса: вызывающий код не должен ожидать
        восстановления прежних значений или повторно использовать эту среду
        для другой роли.
        """

        if role not in {"app", "migrator"}:
            raise StorageConfigurationError("Роль локального окружения PostgreSQL некорректна.")
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
                "Локальное окружение PostgreSQL не совпадает с контрольным маркером рабочей среды."
            )

    @property
    def app_passfile(self) -> str:
        """Вернуть путь к файлу паролей приложения, не раскрывая его содержимое."""

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
            f"Значение локального окружения в строке {line_number} некорректно."
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
    for key in SECRET_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment.pop("PGPASSWORD", None)
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
            return None
        payload = json.loads(completed.stdout)
        current_sid = payload["CurrentSid"]
        rules = payload["Rules"]
        if isinstance(rules, dict):
            rules = [rules]
        if not isinstance(rules, list):
            return None
        if payload["OwnerSid"] != current_sid or payload["Protected"] is not True:
            return False
        allowed_sids = {current_sid, "S-1-5-18"}
        current_full_control = False
        for rule in rules:
            if not isinstance(rule, dict) or not {
                "Sid",
                "Type",
                "Inherited",
                "Rights",
            }.issubset(rule):
                return None
            if not isinstance(rule["Rights"], int):
                return None
            if (
                rule["Sid"] not in allowed_sids
                or rule["Type"] != "Allow"
                or rule["Inherited"] is not False
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
        return None


def _require_secure_permissions(path: Path, metadata: os.stat_result) -> None:
    if os.name == "nt":
        secure = _windows_acl_is_restricted(path)
        if secure is None:
            raise StorageConfigurationUnknownError(
                "Не удалось подтвердить ACL локального окружения."
            )
    else:
        secure = metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    if not secure:
        raise StorageConfigurationError(
            "Файл .env окружения PostgreSQL имеет небезопасные права доступа."
        )


def _read_local_environment_values(path: str | Path) -> dict[str, str] | None:
    """Прочитать `.env` с ключами из реестра, не устанавливая их в окружение процесса."""

    env_path = Path(path)
    try:
        metadata = env_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageConfigurationUnknownError(
            "Не удалось проверить локальное окружение."
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise StorageConfigurationError(
            "Локальное окружение отсутствует или небезопасно."
        )
    try:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65_536:
            raise StorageConfigurationError(
                "Локальное окружение отсутствует или небезопасно."
            )
        _require_secure_permissions(env_path, metadata)
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except UnicodeError as exc:
            raise StorageConfigurationError(
                "Локальное окружение содержит текст неверного формата."
            ) from exc
        final_metadata = env_path.stat()
        if (
            stat.S_ISLNK(env_path.lstat().st_mode)
            or metadata.st_dev != final_metadata.st_dev
            or metadata.st_ino != final_metadata.st_ino
            or metadata.st_size != final_metadata.st_size
            or metadata.st_mtime_ns != final_metadata.st_mtime_ns
        ):
            raise StorageConfigurationUnknownError(
                "Локальное окружение изменилось во время чтения."
            )
    except StorageConfigurationError:
        raise
    except OSError as exc:
        raise StorageConfigurationUnknownError(
            "Не удалось прочитать локальное окружение."
        ) from exc

    values: dict[str, str] = {}
    seen_keys: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise StorageConfigurationError(
                f"Строка {line_number} локального окружения имеет неверный формат."
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key) or key in seen_keys:
            raise StorageConfigurationError(
                f"Ключ локального окружения в строке {line_number} некорректен."
            )
        seen_keys.add(key)
        if key not in LOCAL_ENVIRONMENT_KEYS:
            raise StorageConfigurationError(
                f"Ключ локального окружения в строке {line_number} некорректен."
            )
        values[key] = _parse_value(raw_value, line_number)
    return values


def read_local_environment_subset(
    path: str | Path,
    *,
    keys: Iterable[str],
) -> dict[str, str] | None:
    """Вернуть только заранее разрешённое подмножество локального файла `.env`.

    Для всего файла выполняются проверки по реестру, повторов ключей, ACL и
    изменений во время чтения; вызывающая сторона получает только запрошенные
    ею ключи.
    """

    requested = frozenset(keys)
    if not requested or not requested.issubset(LOCAL_ENVIRONMENT_KEYS):
        raise StorageConfigurationError(
            "Запрошенное подмножество локального окружения не зарегистрировано."
        )
    values = _read_local_environment_values(path)
    if values is None:
        return None
    missing = requested.difference(values)
    if missing:
        raise StorageConfigurationError(
            "В локальном окружении отсутствует обязательный зарегистрированный ключ."
        )
    return {key: values[key] for key in requested}


def write_local_mcp_bridge_caller_token(
    repository_root: str | Path, token: str
) -> None:
    """Атомарно записать отдельный токен вызывающего клиента в защищённый `.env` проекта."""

    from module.mcp_shared.windows_mcp_bridge_contract import (
        BRIDGE_CALLER_TOKEN_ENV_VAR,
    )

    key = get_local_environment_key(BRIDGE_CALLER_TOKEN_ENV_VAR)
    if key is None or key.scope != "mcp" or not key.secret:
        raise StorageConfigurationError(
            "Ключ учётных данных вызывающего клиента моста отсутствует в реестре локального окружения."
        )
    if (
        not isinstance(token, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{32,4096}", token) is None
    ):
        raise StorageConfigurationError(
            "Токен вызывающего клиента моста имеет неверный формат."
        )

    root = Path(repository_root).absolute()
    env_path = root / DEFAULT_LOCAL_ENV_PATH
    if not (
        (root / "pyproject.toml").is_file()
        and (root / ".codex" / "config.toml").is_file()
    ):
        raise StorageConfigurationError(
            "Корень локального окружения проекта не подтверждён."
        )
    try:
        before = env_path.lstat()
    except FileNotFoundError as exc:
        raise StorageConfigurationError(
            "Защищённое локальное окружение проекта ещё не создано."
        ) from exc
    except OSError as exc:
        raise StorageConfigurationUnknownError(
            "Локальное окружение проекта невозможно проверить."
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise StorageConfigurationError("Локальное окружение проекта небезопасно.")
    current_values = _read_local_environment_values(env_path)
    if current_values is None:
        raise StorageConfigurationError(
            "Защищённое локальное окружение проекта ещё не создано."
        )
    internal_keys = (
        "AZURPILOT_DEV_LOCAL_MCP_TOKEN",
        "AZURPILOT_GAME_LOCAL_MCP_TOKEN",
    )
    if any(current_values.get(name) == token for name in internal_keys):
        raise StorageConfigurationError(
            "Токен вызывающего клиента моста должен отличаться от внутренних учётных данных Dev/Game."
        )
    try:
        current_metadata = env_path.stat()
        if (
            stat.S_ISLNK(env_path.lstat().st_mode)
            or before.st_dev != current_metadata.st_dev
            or before.st_ino != current_metadata.st_ino
            or before.st_size != current_metadata.st_size
            or before.st_mtime_ns != current_metadata.st_mtime_ns
        ):
            raise StorageConfigurationUnknownError(
                "Локальное окружение проекта изменилось во время обновления."
            )
        original = env_path.read_bytes()
        if len(original) > 65_536:
            raise StorageConfigurationError(
                "Локальное окружение проекта превышает допустимый размер."
            )
        text = original.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise StorageConfigurationUnknownError(
            "Локальное окружение проекта невозможно безопасно прочитать."
        ) from exc

    lines = text.splitlines(keepends=True)
    replaced = False
    updated_lines: list[str] = []
    for line in lines:
        content = line.rstrip("\r\n")
        if content and not content.lstrip().startswith("#") and "=" in content:
            existing_key = content.split("=", 1)[0].strip()
            if existing_key == BRIDGE_CALLER_TOKEN_ENV_VAR:
                ending = line[len(content) :]
                updated_lines.append(f"{BRIDGE_CALLER_TOKEN_ENV_VAR}={token}{ending}")
                replaced = True
                continue
        updated_lines.append(line)
    if not replaced:
        if updated_lines and not updated_lines[-1].endswith(("\n", "\r")):
            updated_lines[-1] += "\n"
        updated_lines.append(f"{BRIDGE_CALLER_TOKEN_ENV_VAR}={token}\n")
    payload = "".join(updated_lines).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=env_path.name + ".", suffix=".tmp", dir=env_path.parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name == "nt":
            _restrict_windows_environment_file(temporary)
        else:
            os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        current_metadata = env_path.stat()
        if (
            stat.S_ISLNK(env_path.lstat().st_mode)
            or before.st_dev != current_metadata.st_dev
            or before.st_ino != current_metadata.st_ino
            or before.st_size != current_metadata.st_size
            or before.st_mtime_ns != current_metadata.st_mtime_ns
        ):
            raise StorageConfigurationUnknownError(
                "Локальное окружение проекта изменилось до записи учётных данных моста."
            )
        os.replace(temporary, env_path)
        if os.name == "nt":
            _restrict_windows_environment_file(env_path)
        metadata = env_path.stat()
        _require_secure_permissions(env_path, metadata)
        persisted = read_local_environment_subset(
            env_path, keys=(BRIDGE_CALLER_TOKEN_ENV_VAR,)
        )
        if persisted is None or persisted.get(BRIDGE_CALLER_TOKEN_ENV_VAR) != token:
            raise StorageConfigurationUnknownError(
                "Записанные учётные данные вызывающего клиента моста не подтверждены."
            )
    except StorageConfigurationError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise StorageConfigurationUnknownError(
            "Учётные данные вызывающего клиента моста невозможно безопасно записать."
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _restrict_windows_environment_file(path: Path) -> None:
    """Защитить новый файл окружения Windows с помощью ACL владельца и SYSTEM."""

    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    identity = subprocess.run(
        ["whoami.exe"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
        creationflags=flags,
        text=True,
        encoding="utf-8",
    )
    if identity.returncode != 0 or not identity.stdout.strip():
        raise StorageConfigurationUnknownError(
            "Не удалось определить владельца локального окружения Windows."
        )
    result = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{identity.stdout.strip()}:(F)",
            "/grant:r",
            "SYSTEM:(F)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
        creationflags=flags,
    )
    if result.returncode != 0:
        raise StorageConfigurationUnknownError(
            "Не удалось ограничить ACL локального окружения проекта."
        )


def read_local_postgres_environment(
    path: str | Path = DEFAULT_LOCAL_ENV_PATH,
) -> LocalPostgresEnvironment | None:
    env_path = Path(path)
    all_values = _read_local_environment_values(path)
    if all_values is None:
        return None

    values = {key: all_values[key] for key in _ALLOWED_KEYS if key in all_values}
    infrastructure_values = {
        key: value
        for key, value in all_values.items()
        if key in INFRASTRUCTURE_ENVIRONMENT_KEYS
    }

    if _ALLOWED_KEYS.difference(values):
        raise StorageConfigurationError(
            "Локальное окружение PostgreSQL не содержит полного производственного контракта."
        )
    for prefix in (_APP_PREFIX, _MIGRATOR_PREFIX):
        try:
            port = int(values[prefix + "PORT"])
        except ValueError as exc:
            raise StorageConfigurationError(
                "Порт в локальном окружении PostgreSQL некорректен."
            ) from exc
        if not 1 <= port <= 65535:
            raise StorageConfigurationError(
                "Порт в локальном окружении PostgreSQL некорректен."
            )
    if values[_APP_PREFIX + "PASSWORD"] == values[_MIGRATOR_PREFIX + "PASSWORD"]:
        raise StorageConfigurationError(
            "Роли приложения и миграции должны использовать разные секреты PostgreSQL."
        )
    for prefix, expected_user in (
        (_APP_PREFIX, "azurpilot_app"),
        (_MIGRATOR_PREFIX, "azurpilot_migrator"),
    ):
        if values[prefix + "USER"] != expected_user:
            raise StorageConfigurationError(
                "Роль в локальном окружении PostgreSQL не соответствует производственному контракту."
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
                "Общие параметры подключения PostgreSQL у приложения и роли миграции должны совпадать."
            )
    return LocalPostgresEnvironment(
        path=env_path,
        values=values,
        infrastructure_values=infrastructure_values,
    )


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
