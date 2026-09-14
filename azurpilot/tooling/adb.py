"""Ограниченная работа с закреплённым артефактом ADB для Windows Build/Repair."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import DeploySettings, project_adb
from .contracts import CapabilityStatus, ResultCode
from .errors import ToolingError
from .filesystem import StateLayout, is_unsafe_path, path_has_link
from .process import ProcessSpec, StructuredProcessRunner

ADB_VERSION = "37.0.0"
ADB_ARCHIVE_URL = "https://dl.google.com/android/repository/platform-tools_r37.0.0-win.zip"
ADB_ARCHIVE_SHA256 = "4fe305812db074cea32903a489d061eb4454cbc90a49e8fea677f4b7af764918"
ADB_FILES = (
    "adb.exe",
    "AdbWinApi.dll",
    "AdbWinUsbApi.dll",
    "libwinpthread-1.dll",
)
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class AdbResolution:
    status: CapabilityStatus
    path: Path | None
    version: str | None
    source: str


def _version_ok(output: str) -> bool:
    return f"Version {ADB_VERSION}" in output or f"version {ADB_VERSION}" in output


def is_healthy(
    path: Path,
    root: Path,
    runner: StructuredProcessRunner,
) -> bool:
    path = Path(path)
    if path_has_link(path) or not path.is_file():
        return False
    parent = path.parent
    if is_unsafe_path(parent):
        return False
    if os.name == "nt" and any(
        is_unsafe_path(parent / name) or not (parent / name).is_file()
        for name in ADB_FILES[1:]
    ):
        return False
    try:
        result = runner.run(
            ProcessSpec(
                executable=path,
                argv=("version",),
                cwd=root,
                timeout_seconds=15.0,
                max_output_bytes=16 * 1024,
                no_window=True,
            )
        )
    except Exception:  # noqa: BLE001 - read-only проверка завершается fail-closed
        return False
    return result.ok and _version_ok(result.stdout + result.stderr)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_cache_directory(path: Path) -> Path:
    raw = Path(path).expanduser()
    if path_has_link(raw) or path_has_link(raw.parent):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Кэш ADB содержит symlink или reparse point.",
        )
    path = raw.resolve(strict=False)
    if is_unsafe_path(path.parent) or is_unsafe_path(path):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Кэш ADB содержит symlink или reparse point.",
        )
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or is_unsafe_path(path):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Кэш ADB не является безопасным каталогом.",
        )
    return path


def _download_archive(destination: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".download", dir=str(destination.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        request = urllib.request.Request(
            ADB_ARCHIVE_URL,
            headers={"User-Agent": "AzurPilot/Bootstrap"},
        )
        with urllib.request.urlopen(request, timeout=60.0) as response, temporary.open("wb") as stream:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_ARCHIVE_BYTES:
                    raise ToolingError(
                        ResultCode.TOOLING_ADB_FAILED,
                        "Официальный архив ADB превышает безопасный размер.",
                    )
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if _sha256(temporary) != ADB_ARCHIVE_SHA256:
            raise ToolingError(
                ResultCode.TOOLING_ADB_FAILED,
                "SHA-256 официального архива ADB не совпал.",
            )
        os.replace(temporary, destination)
    except ToolingError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise ToolingError(
            ResultCode.TOOLING_ADB_FAILED,
            "Не удалось загрузить официальный архив ADB.",
        ) from exc


def _extract_archive(archive: Path, destination: Path) -> Path:
    if path_has_link(destination) or path_has_link(destination.parent):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Распакованный кэш ADB содержит symlink или reparse point.",
        )
    temporary = Path(tempfile.mkdtemp(prefix="adb-extract-", dir=str(destination.parent)))
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > 512:
                raise ToolingError(
                    ResultCode.TOOLING_ADB_FAILED,
                    "Архив ADB содержит слишком много файлов.",
                )
            total_size = 0
            for member in members:
                member_name = member.filename.replace("\\", "/")
                member_path = PurePosixPath(member_name)
                if (
                    member_name.startswith("/")
                    or member_path.is_absolute()
                    or ".." in member_path.parts
                    or "\\" in member.filename
                ):
                    raise ToolingError(
                        ResultCode.TOOLING_ADB_FAILED,
                        "Архив ADB содержит небезопасный путь.",
                    )
                if member.is_dir():
                    continue
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ToolingError(
                        ResultCode.TOOLING_ADB_FAILED,
                        "Архив ADB содержит symlink.",
                    )
                total_size += member.file_size
                if total_size > _MAX_ARCHIVE_BYTES:
                    raise ToolingError(
                        ResultCode.TOOLING_ADB_FAILED,
                        "Распакованный архив ADB превышает безопасный размер.",
                    )
            bundle.extractall(temporary)
        extracted_root = temporary / "platform-tools"
        if not extracted_root.is_dir() or any(
            not (extracted_root / name).is_file() for name in ADB_FILES
        ):
            raise ToolingError(
                ResultCode.TOOLING_ADB_FAILED,
                "Архив ADB не содержит полный закреплённый контракт platform-tools.",
            )
        if os.path.lexists(str(destination)):
            if is_unsafe_path(destination):
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Распакованный кэш ADB содержит symlink или reparse point.",
                )
            shutil.rmtree(destination)
        os.replace(extracted_root, destination)
        return destination
    except ToolingError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise ToolingError(
            ResultCode.TOOLING_ADB_FAILED,
            "Не удалось распаковать официальный архив ADB.",
        ) from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def resolve_adb(
    root: Path,
    settings: DeploySettings,
    layout: StateLayout,
    runner: StructuredProcessRunner,
) -> AdbResolution:
    """Найти исправный ADB; на Windows при необходимости получить закреплённый артефакт."""

    configured = project_adb(root, settings)
    candidates: list[tuple[Path, str]] = []
    configured_is_project_default = False
    if settings.adb_executable:
        configured_path = Path(settings.adb_executable).expanduser()
        if not configured_path.is_absolute():
            configured_path = root / configured_path
        if path_has_link(configured_path):
            raise ToolingError(
                ResultCode.TOOLING_ADB_FAILED,
                "Явно настроенный ADB содержит symlink или reparse point.",
            )
        configured_path = configured_path.resolve(strict=False)
        default_path = (
            root
            / ".venv"
            / ("Scripts/adb.exe" if os.name == "nt" else "bin/adb")
        ).resolve(strict=False)
        configured_is_project_default = os.path.normcase(str(configured_path)) == os.path.normcase(
            str(default_path)
        )
        candidates.append((configured_path, "deploy_config"))
    candidates.append((configured, "project_environment"))
    path_adb = shutil.which("adb.exe" if os.name == "nt" else "adb")
    if path_adb:
        candidates.append((Path(path_adb), "PATH"))
    seen: set[str] = set()
    for candidate, source in candidates:
        candidate = candidate.resolve(strict=False)
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        if is_healthy(candidate, root, runner):
            return AdbResolution(CapabilityStatus.READY, candidate, ADB_VERSION, source)
        if source == "deploy_config" and not configured_is_project_default:
            raise ToolingError(
                ResultCode.TOOLING_ADB_FAILED,
                "Явно настроенный ADB отсутствует или не соответствует pinned версии.",
            )

    if os.name != "nt":
        return AdbResolution(
            CapabilityStatus.NOT_CONFIGURED,
            None,
            None,
            "POSIX",
        )

    cache = _safe_cache_directory(layout.bootstrap_cache_directory / "platform-tools" / ADB_VERSION)
    archive = cache / "platform-tools.zip"
    if path_has_link(archive):
        raise ToolingError(
            ResultCode.TOOLING_ADB_FAILED,
            "Кэш архива ADB содержит symlink или reparse point.",
        )
    if archive.is_file() and _sha256(archive) != ADB_ARCHIVE_SHA256:
        archive.unlink()
    if not archive.is_file():
        _download_archive(archive)
    extracted = cache / "extracted"
    extracted_ready = extracted.is_dir() and all(
        not path_has_link(extracted / name) and (extracted / name).is_file()
        for name in ADB_FILES
    )
    if extracted_ready and not is_healthy(extracted / "adb.exe", root, runner):
        if is_unsafe_path(extracted):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Распакованный кэш ADB содержит небезопасный объект.",
            )
        shutil.rmtree(extracted)
        extracted_ready = False
    if not extracted_ready:
        extracted = _extract_archive(archive, extracted)
    source_adb = extracted / "adb.exe"
    if not is_healthy(source_adb, root, runner):
        raise ToolingError(
            ResultCode.TOOLING_ADB_FAILED,
            "Официальный закреплённый ADB не прошёл проверку запуска.",
        )
    return AdbResolution(CapabilityStatus.READY, source_adb, ADB_VERSION, "pinned_artifact")


def install_adb(
    source: Path,
    destination_directory: Path,
    root: Path,
    runner: StructuredProcessRunner,
) -> None:
    """Атомарно обновить только файлы ADB после проверки источника."""

    if os.name != "nt":
        return
    if not is_healthy(source, root, runner):
        raise ToolingError(
            ResultCode.TOOLING_ADB_FAILED,
            "Источник ADB не прошёл проверку перед установкой.",
        )
    raw_destination = Path(destination_directory).expanduser()
    if path_has_link(raw_destination) or path_has_link(raw_destination.parent):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Каталог ADB проекта содержит symlink или reparse point.",
        )
    destination_directory = raw_destination.resolve(strict=False)
    if is_unsafe_path(destination_directory):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Каталог ADB проекта содержит symlink или reparse point.",
        )
    destination_directory.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="adb-install-", dir=str(destination_directory)))
    rollback = Path(tempfile.mkdtemp(prefix="adb-rollback-", dir=str(destination_directory)))
    replaced: list[str] = []

    def _restore(names: list[str]) -> None:
        try:
            for name in reversed(names):
                target = destination_directory / name
                saved = rollback / name
                if saved.is_file():
                    os.replace(saved, target)
                else:
                    target.unlink(missing_ok=True)
        except OSError as rollback_error:
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Установка ADB не завершена, а восстановление старых файлов не подтверждено.",
            ) from rollback_error

    try:
        for name in ADB_FILES:
            source_file = source.parent / name
            if path_has_link(source_file) or not source_file.is_file():
                raise ToolingError(
                    ResultCode.TOOLING_ADB_FAILED,
                    "Источник ADB потерял обязательный файл до установки.",
                )
            staged = staging / name
            shutil.copy2(source_file, staged)
        for name in ADB_FILES:
            target = destination_directory / name
            if os.path.lexists(str(target)):
                if is_unsafe_path(target) or not target.is_file():
                    raise ToolingError(
                        ResultCode.TOOLING_ADB_FAILED,
                        "Существующий файл ADB проекта имеет небезопасный тип.",
                    )
                shutil.copy2(target, rollback / name)
            os.replace(staging / name, target)
            replaced.append(name)
    except ToolingError:
        _restore(replaced)
        raise
    except (OSError, shutil.Error) as exc:
        _restore(replaced)
        raise ToolingError(
            ResultCode.TOOLING_ADB_FAILED,
            "Не удалось атомарно сохранить ADB в среде проекта.",
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(rollback, ignore_errors=True)


__all__ = [
    "ADB_ARCHIVE_SHA256",
    "ADB_ARCHIVE_URL",
    "ADB_FILES",
    "ADB_VERSION",
    "AdbResolution",
    "install_adb",
    "is_healthy",
    "resolve_adb",
]
