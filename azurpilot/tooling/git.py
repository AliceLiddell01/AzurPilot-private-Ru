"""Структурированный Git adapter для Update и root validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from shutil import which

from .contracts import ResultCode
from .errors import ToolingError
from .process import ProcessResult, ProcessSpec, StructuredProcessRunner


@dataclass(frozen=True)
class GitCommand:
    args: tuple[str, ...]
    result: ProcessResult


class GitClient:
    """Git только через argv, cwd и bounded process result."""

    def __init__(
        self, root: Path, runner: StructuredProcessRunner | None = None
    ) -> None:
        self.root = root.resolve(strict=False)
        self.runner = runner or StructuredProcessRunner()
        self.executable = which("git")

    def run(self, *args: str, timeout_seconds: float = 60.0) -> GitCommand:
        if self.executable is None:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE, "git не найден."
            )
        if any("\x00" in arg for arg in args):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION, "Git argument содержит NUL."
            )
        result = self.runner.run(
            ProcessSpec(
                executable=self.executable,
                argv=("-C", str(self.root), *args),
                cwd=self.root,
                timeout_seconds=timeout_seconds,
                max_output_bytes=128 * 1024,
                env={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
            )
        )
        command = GitCommand(tuple(args), result)
        if result.timed_out:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT, "Git operation превысил deadline."
            )
        if result.returncode != 0:
            raise ToolingError(
                ResultCode.TOOLING_GIT_FAILED, "Git operation завершился ошибкой."
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
                ResultCode.TOOLING_TIMEOUT, "Проверка Git ancestry превысила deadline."
            )
        if command.returncode == 0:
            return True
        if command.returncode == 1:
            return False
        raise ToolingError(
            ResultCode.TOOLING_GIT_FAILED, "Проверка Git ancestry завершилась ошибкой."
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


def _is_sha(value: str) -> bool:
    return 40 <= len(value) <= 64 and all(
        character in "0123456789abcdef" for character in value.lower()
    )


__all__ = ["GitClient", "GitCommand"]
