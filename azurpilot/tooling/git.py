"""Свидетельства Git только для чтения для MCP, Semgrep и проверки репозитория."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from urllib.parse import urlsplit

from .contracts import ResultCode
from .errors import ToolingError
from .process import ProcessResult, ProcessSpec, StructuredProcessRunner

_MAX_GIT_OBJECT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class GitCommand:
    args: tuple[str, ...]
    result: ProcessResult


class GitClient:
    """Git только через argv, cwd и ограниченный результат процесса."""

    def __init__(
        self, root: Path, runner: StructuredProcessRunner | None = None
    ) -> None:
        self.root = root.resolve(strict=False)
        self.runner = runner or StructuredProcessRunner()
        self.executable = which("git")

    def _run(
        self,
        *args: str,
        timeout_seconds: float = 60.0,
        max_output_bytes: int = 128 * 1024,
        allow_nonzero: bool = False,
    ) -> GitCommand:
        if self.executable is None:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE, "git не найден."
            )
        if any("\x00" in arg for arg in args):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION, "Аргумент Git содержит NUL."
            )
        result = self.runner.run(
            ProcessSpec(
                executable=self.executable,
                argv=("-C", str(self.root), *args),
                cwd=self.root,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
                env={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
            )
        )
        command = GitCommand(tuple(args), result)
        if result.timed_out:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT, "Операция Git превысила установленный срок."
            )
        if result.returncode != 0 and not allow_nonzero:
            raise ToolingError(
                ResultCode.TOOLING_GIT_FAILED, "Операция Git завершилась ошибкой."
            )
        return command

    def _text(self, *args: str, timeout_seconds: float = 60.0) -> str:
        command = self._run(*args, timeout_seconds=timeout_seconds)
        _require_complete(command, "Текстовый ответ Git")
        return command.result.stdout.strip()

    def head(self) -> str:
        value = self._text("rev-parse", "HEAD")
        if not _is_sha(value):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Git HEAD имеет неверный формат.",
            )
        return value

    def branch(self) -> str:
        value = self._text("branch", "--show-current")
        if not value:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Для проверки ветки требуется именованная ветка Git.",
            )
        return value

    def status_porcelain(self) -> str:
        return self._text("status", "--porcelain=v1", "--untracked-files=all")

    def remote_url(self, remote: str) -> str:
        return self._text("config", "--get", f"remote.{remote}.url")

    def upstream(self) -> str:
        return self._text(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        )

    def staged_paths(self) -> tuple[str, ...]:
        """Получить только пути индекса, не расширяя проверку до рабочего дерева."""

        # Переименование представляется удалением старого и добавлением нового
        # пути, чтобы потребитель анализировал оба содержимых.
        command = self._run(
            "diff", "--cached", "--name-only", "--no-renames", "-z", "--"
        )
        _require_complete(command, "Список путей индекса")
        output = command.result.stdout
        return tuple(sorted(path for path in output.split("\x00") if path))

    def status_z(self) -> str:
        """Получить ограниченный машиночитаемый снимок состояния."""

        # Нельзя использовать _text(): strip() уничтожает первый пробел в
        # porcelain XY-коде и превращает непроиндексированный ` M` в ложный индексированный `M`.
        command = self._run(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        )
        _require_complete(command, "Git status")
        return command.result.stdout

    def object_bytes(self, revision_path: str) -> bytes:
        """Прочитать Git blob без потери бинарных байтов."""

        if not revision_path or revision_path.startswith("-"):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Путь объекта Git должен быть непустым и не начинаться с дефиса.",
            )
        command = self._run(
            "show",
            revision_path,
            max_output_bytes=_MAX_GIT_OBJECT_BYTES,
        )
        if getattr(command.result, "stdout_truncated", False) or getattr(
            command.result, "stderr_truncated", False
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Объект Git прочитан не полностью из-за ограничения вывода.",
            )
        return command.result.stdout_bytes

    def object_exists(self, revision_path: str) -> bool:
        """Проверить наличие объекта Git отдельным ограниченным запросом."""

        if not revision_path or revision_path.startswith("-"):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Путь объекта Git должен быть непустым и не начинаться с дефиса.",
            )
        command = self._run(
            "cat-file",
            "-e",
            revision_path,
            max_output_bytes=16 * 1024,
            allow_nonzero=True,
        )
        _require_complete(command, "Проверка наличия объекта Git")
        if command.result.returncode == 0:
            return True
        stderr = command.result.stderr.casefold()
        if (
            command.result.returncode in {1, 128}
            and not getattr(command.result, "stderr_truncated", False)
            and any(
                marker in stderr
                for marker in (
                    "does not exist",
                    "exists on disk, but not in",
                    "not in the index",
                    "not a valid object name",
                )
            )
        ):
            return False
        raise ToolingError(
            ResultCode.TOOLING_GIT_FAILED,
            "Git не смог подтвердить наличие объекта Git.",
        )

    def changed_paths(self, start: str, end: str) -> tuple[str, ...]:
        """Вернуть ограниченный список путей между двумя подтверждёнными ревизиями."""

        if not _is_sha(start) or not _is_sha(end):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Ревизии Git для сравнения имеют неверный формат.",
            )
        command = self._run(
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"{start}..{end}",
            "--",
            max_output_bytes=_MAX_GIT_OBJECT_BYTES,
        )
        _require_complete(command, "Список изменённых Git paths")
        output = command.result.stdout
        return tuple(sorted(path for path in output.split("\x00") if path))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if self.executable is None:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE, "git не найден."
            )
        command = self.runner.run(
            ProcessSpec(
                executable=self.executable,
                argv=(
                    "-C",
                    str(self.root),
                    "merge-base",
                    "--is-ancestor",
                    ancestor,
                    descendant,
                ),
                cwd=self.root,
                timeout_seconds=30,
                max_output_bytes=16 * 1024,
                env={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
            )
        )
        if command.timed_out:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT, "Проверка предка Git превысила установленный срок."
            )
        if getattr(command, "stdout_truncated", False) or getattr(
            command, "stderr_truncated", False
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Проверка предка Git вернула усечённый ответ.",
            )
        if command.returncode == 0:
            return True
        if command.returncode == 1:
            return False
        raise ToolingError(
            ResultCode.TOOLING_GIT_FAILED, "Проверка предка Git завершилась ошибкой."
        )

    def active_operation(self) -> bool:
        git_dir_text = self._text("rev-parse", "--git-dir")
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = self.root / git_dir
        return any(
            (git_dir / marker).exists()
            for marker in (
                "MERGE_HEAD",
                "CHERRY_PICK_HEAD",
                "REVERT_HEAD",
                "rebase-merge",
                "rebase-apply",
            )
        )


def canonical_remote_identity(value: str) -> str:
    """Нормализовать SSH/HTTPS формы одного удалённого репозитория.

    Локальные тестовые репозитории тоже поддерживаются, но сравниваются только по
    каноническому пути. URL с учётными данными, query или fragment запрещены.
    """

    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or any(char.isspace() for char in value)
    ):
        raise ToolingError(
            ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
            "Идентичность Git remote пуста или имеет небезопасный формат.",
        )
    scp_match = re.fullmatch(r"[^/@:\s]+@(?P<host>[^/:\s]+):(?P<path>.+)", value)
    if scp_match:
        host = scp_match.group("host").casefold()
        path = scp_match.group("path").strip("/")
        if "?" in path or "#" in path:
            raise ToolingError(
                ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                "Идентичность Git remote содержит query или fragment.",
            )
        return _hosted_identity(host, path)
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https", "ssh", "git"}:
        if (
            parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "ssh" and parsed.username)
            or (
                parsed.scheme == "ssh"
                and parsed.username is not None
                and parsed.username.casefold() != "git"
            )
        ):
            raise ToolingError(
                ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                "Идентичность Git remote содержит учётные данные или дополнительные параметры.",
            )
        if not parsed.hostname or not parsed.path.strip("/"):
            raise ToolingError(
                ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                "Идентичность Git remote не содержит hosted repository.",
            )
        return _hosted_identity(parsed.hostname.casefold(), parsed.path)
    windows_absolute = re.fullmatch(r"[A-Za-z]:[\\/].*", value) is not None
    if windows_absolute:
        normalized = value.replace("\\", "/").rstrip("/").casefold()
        return "local:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    path = Path(value).expanduser()
    if path.is_absolute():
        normalized = str(path.resolve(strict=False)).replace("\\", "/").rstrip("/").casefold()
        return "local:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    raise ToolingError(
        ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
        "Идентичность Git remote нельзя доказать по известному hosted или local формату.",
    )


def _hosted_identity(host: str, raw_path: str) -> str:
    parts = [part for part in raw_path.replace("\\", "/").split("/") if part]
    if any(part in {".", ".."} or ":" in part for part in parts):
        raise ToolingError(
            ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
            "Идентичность Git remote содержит небезопасный путь repository.",
        )
    path = "/".join(parts)
    if path.casefold().endswith(".git"):
        path = path[:-4]
    if not path:
        raise ToolingError(
            ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
            "Идентичность Git remote не содержит пути repository.",
        )
    return f"hosted:{host}/{path.casefold()}"


def _is_sha(value: str) -> bool:
    return 40 <= len(value) <= 64 and all(
        character in "0123456789abcdef" for character in value.lower()
    )


def _require_complete(command: GitCommand, description: str) -> None:
    """Не принимать критичные для безопасности Git свидетельства с усечённым выводом."""

    if getattr(command.result, "stdout_truncated", False) or getattr(
        command.result, "stderr_truncated", False
    ):
        raise ToolingError(
            ResultCode.TOOLING_VERIFICATION_UNKNOWN,
            f"{description} усечён ограничением вывода; постусловие не подтверждено.",
        )


__all__ = [
    "GitClient",
    "GitCommand",
    "canonical_remote_identity",
]
