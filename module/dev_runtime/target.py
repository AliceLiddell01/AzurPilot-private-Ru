"""Каноническая конфигурация development target для Dev Runtime."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from deploy.atomic import file_write, replace_tmp, to_tmp_file
from module.config.profile import ProfileDiscoveryError, discover_profile_configs
from module.dev_runtime.bounded_io import BoundedReadTooLarge, read_bounded_bytes

DEV_TARGET_SCHEMA_VERSION = 1
DEV_TARGET_FILE_NAME = "dev-runtime-target.json"
_MAX_TARGET_BYTES = 16 * 1024
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


@dataclass(frozen=True, slots=True)
class DevTarget:
    """Явно назначенный профиль единственной development-среды."""

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


class DevTargetRegistry:
    """Читает и атомарно записывает repository-scoped target marker."""

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
        except FileNotFoundError as exc:
            raise DevTargetError(
                "DEV_TARGET_NOT_CONFIGURED",
                "Development target не назначен в config/state/dev-runtime-target.json",
            ) from exc
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
            code = "DEV_TARGET_AMBIGUOUS" if len(matches) > 1 else "DEV_TARGET_PROFILE_MISSING"
            message = (
                "Development target соответствует нескольким профилям"
                if len(matches) > 1
                else "Назначенный development target отсутствует или структурно недопустим"
            )
            raise DevTargetError(code, message)
        return DevTarget(profile_name=matches[0].name, mod_name=matches[0].mod_name)

    @classmethod
    def configure(
        cls,
        repository_root: Path,
        *,
        profile_name: str,
        mod_name: str = "alas",
    ) -> DevTarget:
        """Явно назначить target после проверки единственного профиля."""

        target = DevTarget(profile_name=profile_name, mod_name=mod_name)
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
            raise DevTargetError(
                "DEV_TARGET_PROFILE_MISSING",
                "Нельзя назначить отсутствующий или структурно недопустимый профиль",
            )

        path = _target_file(repository_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(path.parent) or _is_reparse_point(path):
            raise DevTargetError(
                "DEV_TARGET_UNSAFE_PATH",
                "Маркер development target не должен быть ссылкой или junction",
            )
        canonical_target = DevTarget(
            profile_name=matches[0].name,
            mod_name=matches[0].mod_name,
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
    parser.add_argument("profile_name", help="Имя существующего профиля из config/*.json")
    args = parser.parse_args()
    try:
        DevTargetRegistry.configure(
            Path(__file__).resolve().parents[2],
            profile_name=args.profile_name,
        )
    except DevTargetError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": exc.message}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps({"ok": True, "code": "DEV_TARGET_CONFIGURED"}, ensure_ascii=False))


__all__ = [
    "DEV_TARGET_FILE_NAME",
    "DEV_TARGET_SCHEMA_VERSION",
    "DevTarget",
    "DevTargetError",
    "DevTargetRegistry",
]


if __name__ == "__main__":  # pragma: no cover
    main()
