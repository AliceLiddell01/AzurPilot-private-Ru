"""Deterministic, fail-closed разрешение repository root."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from shutil import which

import azurpilot

from .contracts import RepositoryRootEvidence, ResultCode, RootSource
from .errors import RepositoryResolutionError
from .filesystem import (
    bounded_read_bytes,
    canonical_path,
    is_unsafe_path,
    path_has_link,
    path_identity,
)
from .process import ProcessSpec, StructuredProcessRunner

MAX_PROJECT_METADATA_BYTES = 512 * 1024


def _same_path(left: Path, right: Path) -> bool:
    if os.name == "nt":
        return os.path.normcase(str(left)) == os.path.normcase(str(right))
    return left == right


@dataclass(frozen=True)
class ResolvedRepository:
    """Внутренний resolved root и неперсональная provenance evidence."""

    path: Path
    evidence: RepositoryRootEvidence

    @property
    def source(self) -> RootSource:
        return self.evidence.source


class RepositoryResolver:
    """Resolver explicit → configured → installation без доверия к CWD."""

    def __init__(self, runner: StructuredProcessRunner | None = None) -> None:
        self.runner = runner or StructuredProcessRunner()

    def resolve(
        self, explicit: str | os.PathLike[str] | None = None
    ) -> ResolvedRepository:
        if explicit is not None:
            candidate = Path(explicit).expanduser()
            if not str(explicit).strip():
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "Явно указанный repository root пуст.",
                )
            if path_has_link(candidate):
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "Явно указанный repository root содержит symlink или reparse point.",
                )
            validated = self._validate(
                canonical_path(candidate), RootSource.EXPLICIT, 1
            )
            return validated

        configured = self._configured_candidates()
        if configured:
            validated = [
                self._validate(candidate, RootSource.CONFIGURED, len(configured))
                for candidate in configured
            ]
            unique = {os.path.normcase(str(item.path)): item for item in validated}
            if len(unique) != 1:
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_AMBIGUOUS,
                    "Конфигурация указывает на несколько разных repository root.",
                )
            return next(iter(unique.values()))

        installed = self._installation_candidates()
        validated: list[ResolvedRepository] = []
        for candidate in installed:
            try:
                validated.append(
                    self._validate(
                        candidate, RootSource.INSTALLATION, min(len(installed), 8)
                    )
                )
            except RepositoryResolutionError as error:
                if error.code in {
                    ResultCode.TOOLING_REPOSITORY_NOT_FOUND,
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                }:
                    continue
                raise
        unique = {os.path.normcase(str(item.path)): item for item in validated}
        if not unique:
            raise RepositoryResolutionError(
                ResultCode.TOOLING_REPOSITORY_NOT_FOUND,
                "Не удалось доказать repository root через installation identity.",
            )
        if len(unique) != 1:
            raise RepositoryResolutionError(
                ResultCode.TOOLING_REPOSITORY_AMBIGUOUS,
                "Installation identity указывает на несколько repository root.",
            )
        return next(iter(unique.values()))

    def _configured_candidates(self) -> tuple[Path, ...]:
        values: list[Path] = []
        if "AZURPILOT_REPOSITORY_ROOT" in os.environ:
            raw = os.environ["AZURPILOT_REPOSITORY_ROOT"].strip()
            if not raw:
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "AZURPILOT_REPOSITORY_ROOT задан пустым значением.",
                )
            configured_path = Path(raw).expanduser()
            if not configured_path.is_absolute():
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "AZURPILOT_REPOSITORY_ROOT должен быть абсолютным путём.",
                )
            if path_has_link(configured_path):
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "AZURPILOT_REPOSITORY_ROOT содержит symlink или reparse point.",
                )
            values.append(canonical_path(configured_path))

        config_paths: list[Path] = []
        for variable in (
            "AZURPILOT_USER_CONFIG",
            "AZURPILOT_MACHINE_CONFIG",
            "AZURPILOT_CONFIG_FILE",
        ):
            if variable in os.environ:
                raw = os.environ[variable].strip()
                if not raw:
                    raise RepositoryResolutionError(
                        ResultCode.TOOLING_REPOSITORY_INVALID,
                        f"{variable} задан пустым значением.",
                    )
                config_path = Path(raw).expanduser()
                if not config_path.is_absolute():
                    raise RepositoryResolutionError(
                        ResultCode.TOOLING_REPOSITORY_INVALID,
                        f"{variable} должен быть абсолютным путём.",
                    )
                config_paths.append(canonical_path(config_path))
        user_config = (
            os.environ.get("APPDATA")
            if os.name == "nt"
            else os.environ.get("XDG_CONFIG_HOME")
        )
        if user_config:
            config_paths.append(
                canonical_path(Path(user_config) / "azurpilot" / "config.toml")
            )
        elif os.name != "nt":
            config_paths.append(
                canonical_path(Path.home() / ".config" / "azurpilot" / "config.toml")
            )
        machine_config = os.environ.get("PROGRAMDATA") if os.name == "nt" else "/etc"
        if machine_config:
            config_paths.append(
                canonical_path(Path(machine_config) / "AzurPilot" / "config.toml")
            )

        seen: set[str] = set()
        for config_path in config_paths:
            key = os.path.normcase(str(config_path))
            if key in seen or not config_path.is_file():
                continue
            seen.add(key)
            try:
                raw_config = bounded_read_bytes(
                    config_path, max_bytes=MAX_PROJECT_METADATA_BYTES
                )
                document = tomllib.loads(raw_config.decode("utf-8-sig"))
            except Exception as exc:
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "Файл repository configuration повреждён или не читается.",
                ) from exc
            repository = document.get("repository")
            if repository is None:
                continue
            if not isinstance(repository, dict) or not isinstance(
                repository.get("root"), str
            ):
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "Секция repository.root в конфигурации имеет неверную схему.",
                )
            root_value = repository["root"].strip()
            if not root_value:
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "Секция repository.root в конфигурации пуста.",
                )
            configured_root = Path(root_value).expanduser()
            if not configured_root.is_absolute():
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "repository.root должен быть абсолютным путём.",
                )
            if path_has_link(configured_root):
                raise RepositoryResolutionError(
                    ResultCode.TOOLING_REPOSITORY_INVALID,
                    "repository.root содержит symlink или reparse point.",
                )
            values.append(canonical_path(configured_root))
        return tuple(values)

    def _installation_candidates(self) -> tuple[Path, ...]:
        starts: list[Path] = []
        package_file = getattr(azurpilot, "__file__", None)
        if package_file:
            starts.append(Path(package_file).resolve(strict=False).parent)
        starts.append(Path(__file__).resolve(strict=False).parent)
        starts.append(Path(sys.executable).resolve(strict=False).parent)
        candidates: dict[str, Path] = {}
        for start in starts:
            current = start
            for _ in range(8):
                key = os.path.normcase(str(current))
                candidates[key] = current
                if current.parent == current:
                    break
                current = current.parent
        return tuple(candidates.values())

    def _validate(
        self, candidate: Path, source: RootSource, candidate_count: int
    ) -> ResolvedRepository:
        checks: list[str] = []
        root = canonical_path(candidate)
        if not root.is_dir():
            raise RepositoryResolutionError(
                ResultCode.TOOLING_REPOSITORY_NOT_FOUND,
                "Указанный repository root не является каталогом.",
            )
        if is_unsafe_path(root / ".git"):
            raise RepositoryResolutionError(
                ResultCode.TOOLING_REPOSITORY_INVALID,
                "Repository metadata содержит symlink или reparse point.",
            )
        if not (root / ".git").exists():
            raise RepositoryResolutionError(
                ResultCode.TOOLING_REPOSITORY_INVALID,
                "Repository root не содержит .git.",
            )
        checks.append("git metadata")
        required_files = ("pyproject.toml", "uv.lock")
        required_directories = ("module", "deploy")
        if any(
            is_unsafe_path(root / item) or not (root / item).is_file()
            for item in required_files
        ) or any(
            is_unsafe_path(root / item) or not (root / item).is_dir()
            for item in required_directories
        ):
            raise RepositoryResolutionError(
                ResultCode.TOOLING_REPOSITORY_INVALID,
                "Repository root не содержит обязательные AzurPilot markers.",
            )
        checks.append("project markers")
        try:
            project = tomllib.loads(
                bounded_read_bytes(
                    root / "pyproject.toml", max_bytes=MAX_PROJECT_METADATA_BYTES
                ).decode("utf-8-sig")
            )
        except Exception as exc:
            raise RepositoryResolutionError(
                ResultCode.TOOLING_REPOSITORY_INVALID,
                "pyproject.toml repository root не читается.",
            ) from exc
        project_metadata = project.get("project")
        if (
            not isinstance(project_metadata, dict)
            or project_metadata.get("name") != "azurpilot"
        ):
            raise RepositoryResolutionError(
                ResultCode.TOOLING_REPOSITORY_INVALID,
                "pyproject.toml не описывает проект azurpilot.",
            )
        checks.append("project identity")
        git = which("git")
        if git is None:
            raise RepositoryResolutionError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Для проверки repository root не найден git.",
            )
        result = self.runner.run(
            ProcessSpec(
                executable=git,
                argv=("-C", str(root), "rev-parse", "--show-toplevel"),
                cwd=root,
                timeout_seconds=10,
                max_output_bytes=16 * 1024,
            )
        )
        git_root = (
            canonical_path(result.stdout.strip())
            if result.ok and result.stdout.strip()
            else None
        )
        if git_root is None or not _same_path(git_root, root):
            raise RepositoryResolutionError(
                ResultCode.TOOLING_REPOSITORY_INVALID,
                "git rev-parse не подтвердил canonical repository root.",
            )
        checks.append("git canonical root")
        return ResolvedRepository(
            path=root,
            evidence=RepositoryRootEvidence(
                source=source,
                candidate_count=max(1, candidate_count),
                validation_checks=tuple(checks),
                root_identity=path_identity(root),
            ),
        )


def resolve_repository_root(
    explicit: str | os.PathLike[str] | None = None,
) -> ResolvedRepository:
    """Convenience API для сервисов и тестов."""

    return RepositoryResolver().resolve(explicit)


__all__ = ["RepositoryResolver", "ResolvedRepository", "resolve_repository_root"]
