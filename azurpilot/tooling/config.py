"""Безопасное чтение deploy-конфигурации без публикации секретов."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ResultCode
from .errors import ToolingError
from .filesystem import bounded_read_text, canonical_path


@dataclass(frozen=True)
class DeploySettings:
    """Только значения, необходимые базовым операционным службам."""

    source_path: Path | None
    webui_host: str = "127.0.0.1"
    webui_port: int = 25548
    python_executable: str | None = None
    uv_executable: str | None = None
    adb_executable: str | None = None
    install_dependencies: bool = True
    git_remote: str = "origin"
    git_branch: str = "personal/stable"
    repository_url: str | None = None
    upstream_remote: str = "upstream"
    upstream_push_url: str = "DISABLED"
    postgres_backup_root: str | None = None
    shortcut_path: str | None = None
    shortcut_icon: str | None = None


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Секция {label} должна быть отображением.",
        )
    return value


def _string(
    section: dict[str, Any], key: str, default: str | None = None
) -> str | None:
    value = section.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Параметр {key} имеет неверный тип.",
        )
    return value.strip()


def _boolean(section: dict[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Параметр {key} должен иметь логический тип.",
        )
    return value


def _port(section: dict[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Параметр {key} имеет неверный порт.",
        )
    return value


def load_deploy_settings(root: Path, *, allow_template: bool = False) -> DeploySettings:
    """Загрузить только безопасный набор операционных параметров из YAML."""

    root = canonical_path(root)
    config_path = root / "config" / "deploy.yaml"
    if not config_path.is_file() and allow_template:
        template = root / "config" / "deploy.template.yaml"
        if template.is_file():
            config_path = template
    if not config_path.is_file():
        return DeploySettings(source_path=None)
    try:
        import yaml

        document = yaml.safe_load(bounded_read_text(config_path))
    except ToolingError:
        raise
    except Exception as exc:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED, "Не удалось разобрать deploy YAML."
        ) from exc
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Корень deploy YAML должен быть отображением.",
        )
    deploy = _mapping(document.get("Deploy"), "Deploy")
    python = _mapping(deploy.get("Python"), "Deploy.Python")
    adb = _mapping(deploy.get("Adb"), "Deploy.Adb")
    webui = _mapping(deploy.get("Webui"), "Deploy.Webui")
    git = _mapping(deploy.get("Git"), "Deploy.Git")
    host = _string(webui, "WebuiHost", "127.0.0.1") or "127.0.0.1"
    return DeploySettings(
        source_path=config_path,
        webui_host=host,
        webui_port=_port(webui, "WebuiPort", 25548),
        python_executable=_string(python, "PythonExecutable"),
        uv_executable=_string(python, "UvExecutable"),
        adb_executable=_string(adb, "AdbExecutable"),
        install_dependencies=_boolean(python, "InstallDependencies", True),
        git_remote=_string(git, "Remote", "origin") or "origin",
        git_branch=_string(git, "Branch", "personal/stable") or "personal/stable",
        repository_url=_string(git, "Repository"),
        upstream_remote=_string(git, "UpstreamRemote", "upstream") or "upstream",
        upstream_push_url=_string(git, "UpstreamPushUrl", "DISABLED") or "DISABLED",
        postgres_backup_root=_string(git, "PostgreSqlBackupRoot"),
        shortcut_path=_string(deploy, "ShortcutPath"),
        shortcut_icon=_string(deploy, "ShortcutIcon"),
    )


def _joined_path(root: Path, value: str | None, default_name: str) -> Path:
    """Собрать путь без разрешения symlink, сохраняя разбор relative/absolute."""

    if value:
        normalized = value.replace("\\", os.sep).replace("/", os.sep)
        candidate = Path(normalized)
        if not candidate.is_absolute():
            candidate = root / candidate
    else:
        candidate = root / default_name
    return candidate


def _relative_or_absolute(root: Path, value: str | None, default_name: str) -> Path:
    """Вернуть канонический путь исполняемого файла, не владеющего venv-границей."""

    return canonical_path(_joined_path(root, value, default_name))


def project_python(root: Path, settings: DeploySettings | None = None) -> Path:
    """Вернуть путь запуска project Python без потери логической venv-границы.

    Configured значение (например ``./.venv/bin/python``) не канонизируется:
    разрешение symlink уничтожило бы venv-семантику дочернего интерпретатора до
    того, как её увидит process layer. Логический путь запуска и каноническую
    identity фактического runtime (symlink POSIX и перенаправитель Windows)
    разрешает ``StructuredProcessRunner`` из одного и того же пути.
    """

    settings = settings or load_deploy_settings(root)
    configured = _joined_path(root, settings.python_executable, "")
    if settings.python_executable and configured.is_file():
        return configured
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def project_uv(root: Path, settings: DeploySettings | None = None) -> Path:
    settings = settings or load_deploy_settings(root)
    configured = _relative_or_absolute(root, settings.uv_executable, "")
    platform_default = (
        root / ".venv" / ("Scripts/uv.exe" if os.name == "nt" else "bin/uv")
    )
    if settings.uv_executable and configured.is_file():
        return configured
    return canonical_path(platform_default)


def project_adb(root: Path, settings: DeploySettings | None = None) -> Path:
    settings = settings or load_deploy_settings(root)
    configured = _relative_or_absolute(root, settings.adb_executable, "")
    platform_default = (
        root / ".venv" / ("Scripts/adb.exe" if os.name == "nt" else "bin/adb")
    )
    if settings.adb_executable and configured.is_file():
        return configured
    return canonical_path(platform_default)


def local_webui_url(settings: DeploySettings) -> str:
    host = (
        "127.0.0.1"
        if settings.webui_host in {"0.0.0.0", "::", "localhost"}
        else settings.webui_host
    )
    return f"http://{host}:{settings.webui_port}/"


__all__ = [
    "DeploySettings",
    "load_deploy_settings",
    "local_webui_url",
    "project_adb",
    "project_python",
    "project_uv",
]
