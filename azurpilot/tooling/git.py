"""Структурированный адаптер Git для Update и проверки корня репозитория."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from urllib.parse import urlsplit

from .contracts import RepositoryIdentity, ResultCode
from .errors import ToolingError
from .filesystem import path_has_link
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

    def run(
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

    def text(self, *args: str, timeout_seconds: float = 60.0) -> str:
        command = self.run(*args, timeout_seconds=timeout_seconds)
        return command.result.stdout.strip()

    def head(self) -> str:
        value = self.text("rev-parse", "HEAD")
        if not _is_sha(value):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Git HEAD имеет неверный формат.",
            )
        return value

    def branch(self) -> str:
        value = self.text("branch", "--show-current")
        if not value:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Detached HEAD не поддерживается Update.",
            )
        return value

    def status_porcelain(self) -> str:
        return self.text("status", "--porcelain=v1", "--untracked-files=all")

    def remote_url(self, remote: str) -> str:
        return self.text("config", "--get", f"remote.{remote}.url")

    def remote_push_url(self, remote: str) -> str | None:
        """Прочитать именно политику отправки, не подменяя её адресом получения."""

        try:
            return self.text("config", "--get", f"remote.{remote}.pushurl")
        except ToolingError as error:
            if error.code is ResultCode.TOOLING_GIT_FAILED:
                return None
            raise

    def remote_exists(self, remote: str) -> bool:
        try:
            self.run("remote", "get-url", remote)
        except ToolingError as error:
            if error.code is ResultCode.TOOLING_GIT_FAILED:
                return False
            raise
        return True

    def remote_identity(self, remote: str) -> str:
        return canonical_remote_identity(self.remote_url(remote))

    def upstream(self) -> str:
        return self.text(
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        )

    def fetch_branch(self, remote: str, branch: str) -> None:
        refspec = f"refs/heads/{branch}:refs/remotes/{remote}/{branch}"
        self.run("fetch", "--no-tags", remote, refspec, timeout_seconds=15 * 60)

    def remote_head(self, remote: str, branch: str) -> str:
        value = self.text("rev-parse", f"refs/remotes/{remote}/{branch}")
        if not _is_sha(value):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Remote HEAD имеет неверный формат.",
            )
        return value

    def remote_ref(self, remote: str, branch: str) -> str | None:
        """Прочитать exact remote ref без доверия к локальному tracking ref."""

        output = self.text(
            "ls-remote",
            "--refs",
            remote,
            f"refs/heads/{branch}",
            timeout_seconds=120.0,
        )
        if not output:
            return None
        rows = [line.split() for line in output.splitlines() if line.strip()]
        if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != f"refs/heads/{branch}":
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Git remote вернул неоднозначный exact ref.",
            )
        value = rows[0][0]
        if not _is_sha(value):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Git remote вернул SHA неверного формата.",
            )
        return value

    def staged_paths(self) -> tuple[str, ...]:
        """Получить только пути index, не расширяя scope до working tree."""

        output = self.run(
            "diff", "--cached", "--name-only", "-z", "--"
        ).result.stdout
        return tuple(sorted(path for path in output.split("\x00") if path))

    def status_z(self) -> str:
        """Получить bounded machine-readable status для snapshot."""

        # Нельзя использовать text(): strip() уничтожает первый пробел в
        # porcelain XY-коде и превращает unstaged ` M` в ложный staged `M`.
        return self.run(
            "status", "--porcelain=v1", "-z", "--untracked-files=all"
        ).result.stdout

    def index_blob(self, path: str) -> str:
        value = self.text("rev-parse", f":{path}")
        if not _is_sha(value):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Index blob имеет неверный формат.",
            )
        return value

    def working_blob(self, path: str) -> str:
        value = self.text("hash-object", "--no-filters", "--", path)
        if not _is_sha(value):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Working-tree blob имеет неверный формат.",
            )
        return value

    def filtered_working_blob(self, path: str) -> str:
        """Получить blob SHA после штатных Git clean filters."""

        value = self.text("hash-object", f"--path={path}", "--", path)
        if not _is_sha(value):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Filtered working-tree blob имеет неверный формат.",
            )
        return value

    def object_bytes(self, revision_path: str) -> bytes:
        """Прочитать Git blob без потери бинарных байтов."""

        if not revision_path or revision_path.startswith("-"):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Git object path должен быть непустым и не начинаться с дефиса.",
            )
        command = self.run(
            "show",
            revision_path,
            max_output_bytes=_MAX_GIT_OBJECT_BYTES,
        )
        if command.result.stdout_truncated:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Git object прочитан не полностью из-за ограничения stdout.",
            )
        return command.result.stdout_bytes

    def object_exists(self, revision_path: str) -> bool:
        """Проверить наличие Git object отдельным bounded-запросом."""

        if not revision_path or revision_path.startswith("-"):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Git object path должен быть непустым и не начинаться с дефиса.",
            )
        command = self.run(
            "cat-file",
            "-e",
            revision_path,
            max_output_bytes=16 * 1024,
            allow_nonzero=True,
        )
        if command.result.returncode == 0:
            return True
        stderr = command.result.stderr.casefold()
        if (
            command.result.returncode in {1, 128}
            and not command.result.stderr_truncated
            and any(
                marker in stderr
                for marker in ("does not exist in", "path '")
            )
        ):
            return False
        raise ToolingError(
            ResultCode.TOOLING_GIT_FAILED,
            "Git не смог подтвердить наличие object.",
        )

    def object_sha256(self, revision_path: str) -> str:
        return hashlib.sha256(self.object_bytes(revision_path)).hexdigest()

    def stage(self, paths: tuple[str, ...]) -> None:
        if not paths:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Нельзя выполнить staging без явного списка путей.",
            )
        # Некоторые исторически tracked docs/config paths одновременно
        # покрываются `.gitignore`. `-f` допустим только вместе с уже
        # проверенным allowlist, не расширяет scope и не означает force push.
        self.run("add", "-f", "--", *paths)

    def unstage(self, paths: tuple[str, ...]) -> None:
        if paths:
            self.run("reset", "--", *paths)

    def commit(self, message: str) -> str:
        self.run("commit", "--message", message, timeout_seconds=120.0)
        return self.head()

    def commit_parent(self, commit: str) -> str:
        value = self.text("rev-parse", f"{commit}^")
        if not _is_sha(value):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Parent commit имеет неверный формат.",
            )
        return value

    def commit_paths(self, commit: str) -> tuple[str, ...]:
        output = self.run(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
            "--",
        ).result.stdout
        return tuple(sorted(path for path in output.split("\x00") if path))

    def commits_in_range(self, start: str, end: str) -> tuple[str, ...]:
        """Вернуть непустой exact range, пригодный для scoped analysis."""

        output = self.text("rev-list", "--reverse", f"{start}..{end}")
        commits = tuple(output.splitlines())
        if not commits or any(not _is_sha(commit) for commit in commits):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Git range не содержит подтверждённых reachable commits.",
            )
        return commits

    def push(self, remote: str, local_branch: str, remote_branch: str) -> None:
        refspec = f"refs/heads/{local_branch}:refs/heads/{remote_branch}"
        self.run("push", "--porcelain", remote, refspec, timeout_seconds=15 * 60)

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
        if command.returncode == 0:
            return True
        if command.returncode == 1:
            return False
        raise ToolingError(
            ResultCode.TOOLING_GIT_FAILED, "Проверка предка Git завершилась ошибкой."
        )

    def dependency_changed(self, before: str, after: str) -> bool:
        output = self.text(
            "diff",
            "--name-only",
            f"{before}..{after}",
            "--",
            "pyproject.toml",
            "uv.lock",
            "deploy/uv.py",
        )
        return bool(output)

    def archive_dependencies(self, revision: str, destination: Path) -> None:
        raw_destination = Path(destination).expanduser()
        if path_has_link(raw_destination) or path_has_link(raw_destination.parent):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Архив зависимостей имеет небезопасный файловый путь.",
            )
        destination = raw_destination.resolve(strict=False)
        if os.path.lexists(str(destination)):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Целевой архив зависимостей уже существует.",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            "archive",
            "--format=tar",
            f"--output={destination}",
            revision,
            "--",
            "pyproject.toml",
            "uv.lock",
            timeout_seconds=60,
        )

    def merge_ff_only(self, target: str) -> None:
        self.run("merge", "--ff-only", target, timeout_seconds=15 * 60)

    def active_operation(self) -> bool:
        git_dir_text = self.text("rev-parse", "--git-dir")
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
    """Нормализовать SSH/HTTPS формы одного hosted repository.

    Локальные fixture remotes тоже поддерживаются, но сравниваются только по
    canonical path. URL с credentials, query или fragment запрещены.
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
    if "@" in value and "://" in value:
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            raise ToolingError(
                ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                "Идентичность Git remote содержит учётные данные.",
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
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
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
    path = "/".join(part for part in raw_path.replace("\\", "/").split("/") if part)
    if path.casefold().endswith(".git"):
        path = path[:-4]
    if not path:
        raise ToolingError(
            ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
            "Идентичность Git remote не содержит пути repository.",
        )
    return f"hosted:{host}/{path.casefold()}"


def repository_identity_from_remote(value: str) -> RepositoryIdentity:
    """Извлечь typed identity hosted remote или явно обозначенного fixture remote."""

    identity = canonical_remote_identity(value)
    if identity.startswith("local:"):
        return RepositoryIdentity(
            host="local",
            owner="fixture",
            repository=identity.removeprefix("local:"),
        )
    if not identity.startswith("hosted:"):
        raise ToolingError(
            ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
            "Для hosted repository требуется hosted Git remote.",
        )
    parts = identity.removeprefix("hosted:").split("/")
    if len(parts) != 3:
        raise ToolingError(
            ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
            "Hosted remote должен содержать ровно host, owner и repository.",
        )
    return RepositoryIdentity(host=parts[0], owner=parts[1], repository=parts[2])


def _is_sha(value: str) -> bool:
    return 40 <= len(value) <= 64 and all(
        character in "0123456789abcdef" for character in value.lower()
    )


__all__ = [
    "GitClient",
    "GitCommand",
    "canonical_remote_identity",
    "repository_identity_from_remote",
]
