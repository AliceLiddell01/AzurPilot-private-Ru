"""Контролируемое снятие завершённого Event overlay из repository data."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT
from module.event_datamine.assets import EVENT_ASSET_CATALOG_NAME, write_asset_catalog
from module.event_datamine.patches import COMPATIBILITY_ROOT
from module.event_datamine.registry import (
    EventArtifactRegistry,
    artifact_lifecycle,
    write_registry,
)
from module.event_datamine.supplemental import (
    DEFAULT_ASSET_ROOT,
    EVENT_SUPPLEMENTAL_ROOT,
    event_supplemental_slug,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GENERATED_EVENT_ROOT = _REPOSITORY_ROOT / "campaign" / "generated_event"
_SAFE_PACKAGE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class EventOverlayRetirementError(ValueError):
    """Event overlay нельзя безопасно вывести из эксплуатации."""


def _safe_package(value: Any) -> PurePosixPath:
    text = str(value or "").strip()
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or any(not _SAFE_PACKAGE_PART.fullmatch(part) for part in path.parts)
    ):
        raise EventOverlayRetirementError(
            f"Некорректный generated package Event overlay: {text!r}"
        )
    return path


def _generated_package(
    artifact: Mapping[str, Any],
) -> tuple[PurePosixPath | None, tuple[PurePosixPath, ...]]:
    metadata = artifact.get("metadata")
    if metadata is None:
        return None, ()
    if not isinstance(metadata, Mapping):
        raise EventOverlayRetirementError("Event artifact содержит некорректный metadata")

    declared_raw = str(metadata.get("generated_package") or "").strip()
    declared = _safe_package(declared_raw) if declared_raw else None

    generated = metadata.get("generated_maps", [])
    if not isinstance(generated, list):
        raise EventOverlayRetirementError(
            "Event artifact содержит некорректный generated_maps"
        )

    modules: list[PurePosixPath] = []
    parents: set[PurePosixPath] = set()
    for raw in generated:
        if not isinstance(raw, Mapping):
            raise EventOverlayRetirementError(
                "Event artifact содержит некорректную generated map запись"
            )
        module = str(raw.get("module") or "").strip()
        if not module:
            continue
        path = PurePosixPath(module)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.suffix != ".py"
            or not path.parent.parts
        ):
            raise EventOverlayRetirementError(
                f"Некорректный generated module Event overlay: {module!r}"
            )
        parent = _safe_package(path.parent.as_posix())
        if not _SAFE_PACKAGE_PART.fullmatch(path.stem):
            raise EventOverlayRetirementError(
                f"Некорректное имя generated module Event overlay: {module!r}"
            )
        parents.add(parent)
        modules.append(path)

    if len(parents) > 1:
        raise EventOverlayRetirementError(
            "Event artifact ссылается более чем на один generated package"
        )
    derived = next(iter(parents), None)
    if declared is not None and derived is not None and declared != derived:
        raise EventOverlayRetirementError(
            "generated_package Event artifact не совпадает с generated_maps"
        )
    return declared or derived, tuple(sorted(set(modules), key=str))


def _path_inside(root: Path, relative: PurePosixPath) -> Path:
    base = root.resolve()
    target = (base / Path(*relative.parts)).resolve()
    if target == base or base not in target.parents:
        raise EventOverlayRetirementError(
            f"Путь Event overlay вышел за пределы data root: {relative.as_posix()!r}"
        )
    return target


def _package_files(
    artifact: Mapping[str, Any],
    *,
    event_id: str,
    generated_root: Path,
) -> tuple[Path | None, tuple[Path, ...]]:
    package, modules = _generated_package(artifact)
    if package is None:
        return None, ()

    package_dir = _path_inside(generated_root, package)
    if package_dir.exists() and (not package_dir.is_dir() or package_dir.is_symlink()):
        raise EventOverlayRetirementError(
            f"Generated package не является обычным каталогом: {package.as_posix()}"
        )
    if not package_dir.exists():
        if modules:
            raise EventOverlayRetirementError(
                f"Generated package отсутствует: {package.as_posix()}"
            )
        return package_dir, ()

    expected: set[Path] = set()
    for module in modules:
        if module.parent != package:
            raise EventOverlayRetirementError(
                f"Generated module не принадлежит package {package.as_posix()}"
            )
        target = _path_inside(generated_root, module)
        if not target.is_file() or target.is_symlink():
            raise EventOverlayRetirementError(
                f"Generated module отсутствует или небезопасен: {module.as_posix()}"
            )
        expected.add(target)

    marker = package_dir / "__init__.py"
    if marker.exists():
        if not marker.is_file() or marker.is_symlink():
            raise EventOverlayRetirementError(
                f"Некорректный marker generated package: {marker}"
            )
        expected.add(marker.resolve())

    runtime_policy = package_dir / "runtime.json"
    if runtime_policy.exists():
        if not runtime_policy.is_file() or runtime_policy.is_symlink():
            raise EventOverlayRetirementError(
                f"Некорректная runtime-policy generated package: {runtime_policy}"
            )
        try:
            runtime_data = json.loads(runtime_policy.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EventOverlayRetirementError(
                f"Не удалось проверить runtime-policy {runtime_policy}"
            ) from exc
        if (
            not isinstance(runtime_data, Mapping)
            or str(runtime_data.get("event_id") or "").strip() != event_id
        ):
            raise EventOverlayRetirementError(
                "runtime-policy generated package не соответствует Event identity"
            )
        expected.add(runtime_policy.resolve())

    actual: set[Path] = set()
    for item in package_dir.rglob("*"):
        if item.is_symlink():
            raise EventOverlayRetirementError(
                f"Generated package содержит symbolic link: {item}"
            )
        if not item.is_file():
            continue
        if "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        actual.add(item.resolve())

    unexpected = sorted(actual - expected)
    if unexpected:
        raise EventOverlayRetirementError(
            "Generated package содержит неожиданные source files: "
            + ", ".join(str(path) for path in unexpected)
        )
    return package_dir, tuple(sorted(actual, key=str))


def _supplemental_files(
    event_id: str,
    supplemental_root: Path,
) -> tuple[Path, tuple[Path, ...]]:
    root = supplemental_root.resolve()
    directory = (root / event_supplemental_slug(event_id)).resolve()
    if directory == root or root not in directory.parents:
        raise EventOverlayRetirementError(
            "Supplemental path вышел за пределы data root"
        )
    if not directory.exists():
        return directory, ()
    if not directory.is_dir() or directory.is_symlink():
        raise EventOverlayRetirementError(
            f"Supplemental Event overlay не является обычным каталогом: {directory}"
        )

    files: list[Path] = []
    for item in directory.rglob("*"):
        if item.is_symlink():
            raise EventOverlayRetirementError(
                f"Supplemental Event overlay содержит symbolic link: {item}"
            )
        if item.is_file():
            files.append(item.resolve())
    return directory, tuple(sorted(files, key=str))


def _remove_empty_directories(directory: Path) -> None:
    if not directory.exists():
        return
    children = sorted(
        (path for path in directory.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for child in children:
        try:
            child.rmdir()
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass


def _restore_files(
    backups: Mapping[Path, bytes | None],
    directories: tuple[Path, ...],
) -> None:
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    for path, content in backups.items():
        if content is None:
            if path.exists() and path.is_file():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def retire_event_overlay(
    event_id: str,
    *,
    now: datetime,
    artifact_root: Path | str = BUILTIN_ARTIFACT_ROOT,
    asset_root: Path | str = DEFAULT_ASSET_ROOT,
    generated_root: Path | str = DEFAULT_GENERATED_EVENT_ROOT,
    supplemental_root: Path | str = EVENT_SUPPLEMENTAL_ROOT,
    compatibility_root: Path | str = COMPATIBILITY_ROOT,
) -> dict[str, Any]:
    """Удалить только source-controlled overlay события после полного lifecycle.

    Операция не вызывается runtime-контуром и никогда не удаляет static assets.
    Для активной или redemption-фазы действует fail-closed.
    """

    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        raise EventOverlayRetirementError("Event identity для retirement не задана")

    artifact_root_path = Path(artifact_root).resolve()
    asset_root_path = Path(asset_root).resolve()
    generated_root_path = Path(generated_root).resolve()
    supplemental_root_path = Path(supplemental_root).resolve()
    compatibility_root_path = Path(compatibility_root).resolve()

    registry = EventArtifactRegistry(artifact_root_path)
    matches = [
        entry
        for entry in registry.entries
        if entry["role"] == "production" and entry["id"] == normalized_event_id
    ]
    if len(matches) != 1:
        raise EventOverlayRetirementError(
            f"Retirement требует ровно один production artifact {normalized_event_id!r}"
        )
    target = matches[0]
    lifecycle = artifact_lifecycle(target, now)
    if lifecycle != "expired":
        raise EventOverlayRetirementError(
            f"Event overlay {normalized_event_id!r} нельзя удалить в фазе {lifecycle!r}"
        )

    artifact_path = _path_inside(
        artifact_root_path,
        PurePosixPath(str(target["path"])),
    )
    if not artifact_path.is_file() or artifact_path.is_symlink():
        raise EventOverlayRetirementError(
            f"Production Event artifact отсутствует или небезопасен: {artifact_path}"
        )

    package, _ = _generated_package(target["artifact"])
    if package is not None:
        for other in registry.entries:
            if other["id"] == normalized_event_id:
                continue
            other_package, _ = _generated_package(other["artifact"])
            if other_package == package:
                raise EventOverlayRetirementError(
                    f"Generated package {package.as_posix()!r} используется другим Event artifact"
                )

    package_dir, package_files = _package_files(
        target["artifact"],
        event_id=normalized_event_id,
        generated_root=generated_root_path,
    )
    supplemental_dir, supplemental_files = _supplemental_files(
        normalized_event_id,
        supplemental_root_path,
    )
    compatibility_file = (
        compatibility_root_path
        / f"{event_supplemental_slug(normalized_event_id)}.json"
    ).resolve()
    if compatibility_file.parent != compatibility_root_path:
        raise EventOverlayRetirementError(
            "Compatibility path вышел за пределы data root"
        )
    if compatibility_file.exists() and (
        not compatibility_file.is_file() or compatibility_file.is_symlink()
    ):
        raise EventOverlayRetirementError(
            f"Compatibility snapshot небезопасен: {compatibility_file}"
        )

    index_path = artifact_root_path / "index.json"
    asset_catalog_path = artifact_root_path / EVENT_ASSET_CATALOG_NAME
    touched = [
        artifact_path,
        index_path,
        asset_catalog_path,
        *package_files,
        *supplemental_files,
    ]
    if compatibility_file.exists():
        touched.append(compatibility_file)

    backups = {
        path: path.read_bytes() if path.exists() else None
        for path in touched
    }
    restore_directories = tuple(
        path
        for path in (package_dir, supplemental_dir)
        if path is not None and path.exists()
    )

    selectors = [
        {
            "server": item["server"],
            "selector": item["selector"],
        }
        for item in registry.campaign_selectors
        if item["event_id"] == normalized_event_id
    ]

    try:
        artifact_path.unlink()
        write_registry(
            artifact_root_path,
            retired_event_id=normalized_event_id,
        )
        write_asset_catalog(
            artifact_root_path,
            asset_root=asset_root_path,
        )

        for path in package_files:
            path.unlink()
        if package_dir is not None:
            _remove_empty_directories(package_dir)

        for path in supplemental_files:
            path.unlink()
        _remove_empty_directories(supplemental_dir)

        if compatibility_file.exists():
            compatibility_file.unlink()

        refreshed = EventArtifactRegistry(artifact_root_path)
        if any(entry["id"] == normalized_event_id for entry in refreshed.entries):
            raise EventOverlayRetirementError(
                "Retired Event artifact остался в registry"
            )
        if any(
            item["event_id"] == normalized_event_id
            for item in refreshed.campaign_selectors
        ):
            raise EventOverlayRetirementError(
                "Retired Event selector binding остался в registry"
            )
    except BaseException:
        try:
            _restore_files(backups, restore_directories)
        except BaseException as rollback_exc:
            raise EventOverlayRetirementError(
                "Retirement завершился ошибкой, а rollback source-controlled overlay не удался"
            ) from rollback_exc
        raise

    return {
        "event_id": normalized_event_id,
        "lifecycle": lifecycle,
        "artifact": str(target["path"]),
        "selectors": selectors,
        "generated_package": package.as_posix() if package is not None else "",
        "static_assets_removed": False,
    }
