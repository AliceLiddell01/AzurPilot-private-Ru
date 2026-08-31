"""Каноническая конфигурация development target для Dev Runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from deploy.atomic import file_write, replace_tmp, to_tmp_file
from module.config.profile import ProfileDiscoveryError, discover_profile_configs
from module.dev_runtime.bounded_io import BoundedReadTooLarge, read_bounded_bytes

DEV_TARGET_SCHEMA_VERSION = 1
DEV_TARGET_FILE_NAME = "dev-runtime-target.json"
DEV_TARGET_POLICY_SCHEMA_VERSION = 1
DEV_TARGET_POLICY_FILE_NAME = "target_policy.json"
_MAX_TARGET_BYTES = 16 * 1024
_MAX_TARGET_POLICY_BYTES = 4 * 1024
_SAFE_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class DevTargetError(ValueError):
    """Безопасная машиночитаемая ошибка разрешения development target."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _is_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        )
    except OSError as exc:
        raise DevTargetError(
            "DEV_TARGET_UNSAFE_PATH", "Путь development target невозможно безопасно проверить"
        ) from exc


def _target_file(repository_root: Path) -> Path:
    root = Path(repository_root).resolve()
    config_dir = root / "config"
    state_dir = config_dir / "state"
    if _is_reparse_point(config_dir) or _is_reparse_point(state_dir):
        raise DevTargetError(
            "DEV_TARGET_UNSAFE_PATH",
            "Каталог конфигурации development target не должен быть ссылкой или junction",
        )
    return state_dir / DEV_TARGET_FILE_NAME


def _raise_invalid(message: str) -> NoReturn:
    raise DevTargetError("DEV_TARGET_INVALID", message)


def _raise_policy_invalid(message: str) -> NoReturn:
    raise DevTargetError("DEV_TARGET_POLICY_INVALID", message)


def _policy_file() -> Path:
    path = Path(__file__).parent / DEV_TARGET_POLICY_FILE_NAME
    if _is_reparse_point(path.parent) or _is_reparse_point(path):
        _raise_policy_invalid("Файл политики development target не должен быть ссылкой или junction")
    return path


@dataclass(frozen=True, slots=True)
class DevTargetPolicy:
    """Проверяемая политика выбора и смены development target."""

    default_profile_name: str
    profile_change_requires_explicit_consent: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.default_profile_name, str)
            or not _SAFE_PROFILE_NAME.fullmatch(self.default_profile_name)
            or self.default_profile_name.casefold().startswith("template")
        ):
            _raise_policy_invalid("Политика содержит небезопасное имя профиля по умолчанию")
        if self.profile_change_requires_explicit_consent is not True:
            _raise_policy_invalid("Политика обязана требовать явного согласия при смене target")

    @classmethod
    def load(cls) -> DevTargetPolicy:
        path = _policy_file()
        try:
            raw = read_bounded_bytes(path, max_bytes=_MAX_TARGET_POLICY_BYTES)
        except FileNotFoundError as exc:
            raise DevTargetError(
                "DEV_TARGET_POLICY_MISSING",
                "Политика development target отсутствует в составе runtime",
            ) from exc
        except BoundedReadTooLarge as exc:
            raise DevTargetError(
                "DEV_TARGET_POLICY_INVALID",
                "Политика development target превышает безопасный размер",
            ) from exc
        except OSError as exc:
            raise DevTargetError(
                "DEV_TARGET_POLICY_INVALID",
                "Политику development target невозможно прочитать",
            ) from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise DevTargetError(
                "DEV_TARGET_POLICY_INVALID",
                "Политика development target содержит некорректный JSON",
            ) from exc

        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "default_profile_name",
            "profile_change_requires_explicit_consent",
        }:
            _raise_policy_invalid("Политика development target имеет неподдерживаемую структуру")
        if payload.get("schema_version") != DEV_TARGET_POLICY_SCHEMA_VERSION:
            _raise_policy_invalid("Политика development target имеет неподдерживаемую схему")
        try:
            return cls(
                default_profile_name=payload["default_profile_name"],
                profile_change_requires_explicit_consent=payload[
                    "profile_change_requires_explicit_consent"
                ],
            )
        except DevTargetError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise DevTargetError(
                "DEV_TARGET_POLICY_INVALID",
                "Политика development target содержит некорректные значения",
            ) from exc


@dataclass(frozen=True, slots=True)
class DevTarget:
    """Канонически разрешённый профиль единственной development-среды."""

    profile_name: str
    mod_name: str = "alas"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile_name, str)
            or not _SAFE_PROFILE_NAME.fullmatch(self.profile_name)
            or self.profile_name.casefold().startswith("template")
        ):
            _raise_invalid("Имя development target имеет небезопасный формат")
        if self.mod_name != "alas":
            _raise_invalid("Development Runtime поддерживает только обычный Alas-профиль")

    def profile_file(self, repository_root: Path) -> Path:
        return Path(repository_root).resolve() / "config" / f"{self.profile_name}.json"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": DEV_TARGET_SCHEMA_VERSION,
            "profile_name": self.profile_name,
            "mod_name": self.mod_name,
        }


def target_identity(target: DevTarget) -> str:
    """Получить стабильную непрозрачную identity канонического target."""

    canonical = json.dumps(
        target.as_dict(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class DevTargetRegistry:
    """Разрешает target и атомарно записывает repository-scoped marker."""

    @staticmethod
    def _resolve(
        repository_root: Path,
        target: DevTarget,
        *,
        missing_code: str = "DEV_TARGET_PROFILE_MISSING",
        missing_message: str = "Назначенный development target отсутствует или структурно недопустим",
    ) -> DevTarget:
        config_dir = Path(repository_root).resolve() / "config"
        try:
            profiles = discover_profile_configs(config_dir, strict=True)
        except ProfileDiscoveryError as exc:
            raise DevTargetError(
                "DEV_TARGET_PROFILE_DISCOVERY_FAILED",
                "Профили AzurPilot невозможно безопасно проверить",
            ) from exc
        matches = [
            profile
            for profile in profiles
            if profile.mod_name == target.mod_name
            and profile.name.casefold() == target.profile_name.casefold()
        ]
        if len(matches) != 1:
            if len(matches) > 1:
                raise DevTargetError(
                    "DEV_TARGET_AMBIGUOUS",
                    "Development target соответствует нескольким профилям",
                )
            raise DevTargetError(missing_code, missing_message)
        return DevTarget(profile_name=matches[0].name, mod_name=matches[0].mod_name)

    @classmethod
    def _load_default(cls, repository_root: Path) -> DevTarget:
        policy = DevTargetPolicy.load()
        target = DevTarget(profile_name=policy.default_profile_name)
        return cls._resolve(
            repository_root,
            target,
            missing_code="DEV_TARGET_DEFAULT_PROFILE_MISSING",
            missing_message="Профиль development target по умолчанию отсутствует или структурно недопустим",
        )

    @classmethod
    def load(cls, repository_root: Path) -> DevTarget:
        path = _target_file(repository_root)
        if _is_reparse_point(path.parent) or _is_reparse_point(path):
            raise DevTargetError(
                "DEV_TARGET_UNSAFE_PATH",
                "Маркер development target не должен быть ссылкой или junction",
            )
        try:
            raw = read_bounded_bytes(path, max_bytes=_MAX_TARGET_BYTES)
        except FileNotFoundError:
            # Разрешение только для чтения намеренно не создаёт маркер.
            return cls._load_default(repository_root)
        except BoundedReadTooLarge as exc:
            raise DevTargetError(
                "DEV_TARGET_STATE_TOO_LARGE",
                "Маркер development target превышает безопасный размер",
            ) from exc
        except OSError as exc:
            raise DevTargetError(
                "DEV_TARGET_STATE_UNREADABLE",
                "Маркер development target невозможно прочитать",
            ) from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise DevTargetError(
                "DEV_TARGET_STATE_CORRUPT",
                "Маркер development target содержит некорректный JSON",
            ) from exc

        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "profile_name",
            "mod_name",
        }:
            raise DevTargetError(
                "DEV_TARGET_STATE_CORRUPT",
                "Маркер development target имеет неподдерживаемую структуру",
            )
        if payload.get("schema_version") != DEV_TARGET_SCHEMA_VERSION:
            raise DevTargetError(
                "DEV_TARGET_STATE_CORRUPT",
                "Маркер development target имеет неподдерживаемую схему",
            )
        try:
            target = DevTarget(
                profile_name=payload["profile_name"],
                mod_name=payload["mod_name"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, DevTargetError):
                raise
            raise DevTargetError(
                "DEV_TARGET_INVALID",
                "Маркер development target содержит некорректное назначение",
            ) from exc

        return cls._resolve(repository_root, target)

    @classmethod
    def load_for_environment(
        cls,
        repository_root: Path,
        *,
        fallback: DevTarget | None = None,
    ) -> DevTarget:
        """Разрешить marker target, сохранив явно внедрённый target без marker.

        ``DevEnvironment`` допускает явный target для изолированных тестов и
        composition roots. Если repository-scoped marker отсутствует, такой
        target уже является назначением среды. Как только marker появляется,
        канонический registry имеет приоритет и его ошибки не скрываются.
        """

        path = _target_file(repository_root)
        if fallback is not None and not os.path.lexists(path):
            return fallback
        return cls.load(repository_root)

    @classmethod
    def configure(
        cls,
        repository_root: Path,
        *,
        profile_name: str | None = None,
        mod_name: str = "alas",
        explicit_consent: bool = False,
    ) -> DevTarget:
        """Назначить target после проверки профиля и явного согласия на смену."""

        if type(explicit_consent) is not bool:
            raise DevTargetError(
                "DEV_TARGET_CONSENT_INVALID",
                "Флаг явного согласия development target должен быть boolean",
            )
        repository_root = Path(repository_root).resolve()
        policy = DevTargetPolicy.load()
        requested_profile_name = (
            policy.default_profile_name if profile_name is None else profile_name
        )
        target = DevTarget(profile_name=requested_profile_name, mod_name=mod_name)
        canonical_target = cls._resolve(
            repository_root,
            target,
            missing_message="Нельзя назначить отсутствующий или структурно недопустимый профиль",
        )

        try:
            current_target = cls.load(repository_root)
        except DevTargetError as exc:
            if exc.code not in {
                "DEV_TARGET_DEFAULT_PROFILE_MISSING",
                "DEV_TARGET_STATE_CORRUPT",
                "DEV_TARGET_STATE_TOO_LARGE",
                "DEV_TARGET_STATE_UNREADABLE",
                "DEV_TARGET_PROFILE_MISSING",
            }:
                raise
            current_target = None

        current_profile_name = (
            current_target.profile_name
            if current_target is not None
            else policy.default_profile_name
        )
        if (
            canonical_target.profile_name.casefold() != current_profile_name.casefold()
            and not explicit_consent
        ):
            raise DevTargetError(
                "DEV_TARGET_CHANGE_REQUIRES_CONSENT",
                "Смена development target требует явного согласия пользователя "
                "(--confirm-profile-change)",
            )

        path = _target_file(repository_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(path.parent) or _is_reparse_point(path):
            raise DevTargetError(
                "DEV_TARGET_UNSAFE_PATH",
                "Маркер development target не должен быть ссылкой или junction",
            )
        temp = to_tmp_file(str(path))
        try:
            file_write(temp, json.dumps(canonical_target.as_dict(), ensure_ascii=True, sort_keys=True) + "\n")
            replace_tmp(temp, str(path))
        finally:
            try:
                Path(temp).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return canonical_target


def main() -> None:
    parser = argparse.ArgumentParser(description="Назначить development target AzurPilot")
    parser.add_argument(
        "profile_name",
        nargs="?",
        default=None,
        help="Имя существующего профиля; по умолчанию используется target policy",
    )
    parser.add_argument(
        "--confirm-profile-change",
        action="store_true",
        help="Явно подтвердить смену development target",
    )
    args = parser.parse_args()
    try:
        DevTargetRegistry.configure(
            Path(__file__).resolve().parents[2],
            profile_name=args.profile_name,
            explicit_consent=args.confirm_profile_change,
        )
    except DevTargetError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": exc.message}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps({"ok": True, "code": "DEV_TARGET_CONFIGURED"}, ensure_ascii=False))


__all__ = [
    "DEV_TARGET_FILE_NAME",
    "DEV_TARGET_POLICY_FILE_NAME",
    "DEV_TARGET_POLICY_SCHEMA_VERSION",
    "DEV_TARGET_SCHEMA_VERSION",
    "DevTarget",
    "DevTargetError",
    "DevTargetPolicy",
    "DevTargetRegistry",
    "target_identity",
]


if __name__ == "__main__":  # pragma: no cover
    main()
