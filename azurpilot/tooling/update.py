"""Безопасный fast-forward Update с внешним dependency journal."""

from __future__ import annotations

import re
from pathlib import Path
from secrets import token_hex

from .bootstrap import BootstrapService
from .config import load_deploy_settings
from .contracts import (
    OperationState,
    ResultCode,
    ToolingResult,
    ToolingWarning,
    UpdateDetails,
    UpdateEvidence,
    WarningCode,
)
from .coordination import RepositoryCoordinator, observe_tcp_port
from .errors import ToolingError
from .filesystem import JournalStore
from .git import GitClient
from .process import StructuredProcessRunner
from .repository import RepositoryResolver, ResolvedRepository

_SAFE_REMOTE_RE = re.compile(r"^[A-Za-z0-9._:/@+%~#?=&-]{1,512}$")
_SAFE_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")


class UpdateService:
    """Update не использует reset, clean, pull, rebase или force push."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        git_factory: type[GitClient] = GitClient,
        bootstrap: BootstrapService | None = None,
        runner: StructuredProcessRunner | None = None,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.git_factory = git_factory
        self.runner = runner or StructuredProcessRunner()
        self.bootstrap = bootstrap or BootstrapService(self.runner)

    def _assert_stopped(self, root: Path) -> None:
        coordinator = RepositoryCoordinator.for_root(root)
        record = coordinator.read_lifecycle()
        if record is not None and coordinator.identity_from_record(record).matches():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Update требует остановленного WebUI.",
            )
        settings = load_deploy_settings(root)
        port = observe_tcp_port(settings.webui_port)
        if port.inspection_failed:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Нельзя подтвердить свободный WebUI port.",
            )
        if port.pids:
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Update не выполняется при занятом WebUI port.",
            )

    @staticmethod
    def _check_remote_url(url: str) -> None:
        if not _SAFE_REMOTE_RE.fullmatch(url) or (
            "://" in url and "@" in url.split("://", 1)[1].split("/", 1)[0]
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Git remote URL содержит недопустимые или чувствительные данные.",
            )

    @staticmethod
    def _validate_git_names(branch: str, remote: str, target_branch: str) -> None:
        if not _SAFE_REMOTE_NAME_RE.fullmatch(remote):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Имя Git remote не соответствует безопасному формату.",
            )
        for value, label in ((branch, "ветка"), (target_branch, "remote branch")):
            if (
                not _SAFE_BRANCH_RE.fullmatch(value)
                or value.startswith(("/", "."))
                or value.endswith(("/", "."))
                or ".." in value
                or "//" in value
                or "@{" in value
            ):
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    f"Имя Git {label} не соответствует безопасному формату.",
                )

    def update(
        self,
        repository_root: str | Path | None = None,
        *,
        expected_branch: str | None = None,
        remote_name: str | None = None,
        remote_branch: str | None = None,
        expected_origin_url: str | None = None,
        timeout_seconds: float = 30 * 60,
    ) -> ToolingResult[UpdateDetails, UpdateEvidence]:
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        settings = load_deploy_settings(root)
        branch = expected_branch or settings.git_branch
        remote = remote_name or settings.git_remote
        target_branch = remote_branch or branch
        self._validate_git_names(branch, remote, target_branch)
        operation_id = f"update-{token_hex(8)}"
        coordinator = RepositoryCoordinator.for_root(root)
        lock = coordinator.lock("update")
        if not lock.acquire(timeout_seconds=0):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Update уже выполняется.",
                operation_id=operation_id,
            )
        transaction = None
        try:
            self._assert_stopped(root)
            repair_journal = JournalStore(coordinator.layout, "repair").active()
            build_journal = JournalStore(coordinator.layout, "build").active()
            existing_journal = JournalStore(coordinator.layout, "update").active()
            if (
                repair_journal is not None
                or build_journal is not None
                or existing_journal is not None
            ):
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "Незавершённая transaction блокирует Update.",
                    operation_id=operation_id,
                )
            git = self.git_factory(root, self.runner)
            actual_branch = git.branch()
            if actual_branch != branch:
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Текущая ветка не совпадает с безопасной веткой Update.",
                    operation_id=operation_id,
                )
            upstream = git.upstream()
            if upstream != f"{remote}/{target_branch}":
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Upstream не совпадает с выбранным remote branch.",
                    operation_id=operation_id,
                )
            remote_url = git.remote_url(remote)
            self._check_remote_url(remote_url)
            configured_origin_url = (
                expected_origin_url
                if expected_origin_url is not None
                else settings.repository_url
            )
            if configured_origin_url is not None and remote_url != configured_origin_url:
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Git remote URL не совпадает с настроенным origin.",
                    operation_id=operation_id,
                )
            if git.active_operation():
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "В checkout уже выполняется Git operation.",
                    operation_id=operation_id,
                )
            if git.status_porcelain():
                raise ToolingError(
                    ResultCode.TOOLING_UPDATE_DIRTY,
                    "Update требует чистого рабочего дерева.",
                    operation_id=operation_id,
                )
            pre_head = git.head()
            git.fetch_branch(remote, target_branch)
            remote_head = git.remote_head(remote, target_branch)
            if pre_head == remote_head:
                return ToolingResult[UpdateDetails, UpdateEvidence](
                    ok=True,
                    code=ResultCode.OK,
                    state=OperationState.READY,
                    message="Checkout уже находится на актуальном remote HEAD.",
                    operation_id=operation_id,
                    details=UpdateDetails(
                        status=OperationState.READY,
                        branch=branch,
                        remote=remote,
                        dependency_changed=False,
                        fast_forwarded=False,
                    ),
                    warnings=(
                        ToolingWarning(
                            code=WarningCode.TOOLING_POSTGRES_BACKUP_NOT_RUN,
                            message="PostgreSQL backup не запускался: Git update не потребовал изменений.",
                        ),
                    ),
                    evidence=UpdateEvidence(
                        repository=resolved.evidence,
                        pre_head=pre_head,
                        post_head=pre_head,
                        remote_head=remote_head,
                    ),
                )
            if git.is_ancestor(remote_head, pre_head):
                raise ToolingError(
                    ResultCode.TOOLING_UPDATE_LOCAL_AHEAD,
                    "Локальная ветка опережает remote; автоматическое переписывание запрещено.",
                    operation_id=operation_id,
                )
            if not git.is_ancestor(pre_head, remote_head):
                raise ToolingError(
                    ResultCode.TOOLING_UPDATE_DIVERGED,
                    "Локальная ветка разошлась с remote; требуется явное решение.",
                    operation_id=operation_id,
                )
            dependency_changed = git.dependency_changed(pre_head, remote_head)
            journal_store = JournalStore(coordinator.layout, "update")
            if dependency_changed:
                transaction = journal_store.create(
                    pre_head=pre_head, target_head=remote_head
                )
                journal_store.save(transaction)
            try:
                git.merge_ff_only(f"refs/remotes/{remote}/{target_branch}")
            except Exception:
                if transaction is not None:
                    journal_store.save(
                        transaction.model_copy(update={"phase": "merge_failed"})
                    )
                raise
            post_head = git.head()
            if post_head != remote_head or git.status_porcelain():
                if transaction is not None:
                    journal_store.save(
                        transaction.model_copy(update={"phase": "verification_failed"})
                    )
                return self._failed_result(
                    resolved,
                    operation_id,
                    branch,
                    remote,
                    dependency_changed,
                    pre_head,
                    post_head,
                    remote_head,
                    "После fast-forward не подтверждены HEAD или clean tree.",
                    transaction_id=transaction.transaction_id if transaction else None,
                )
            if dependency_changed:
                transaction = transaction.model_copy(update={"phase": "merged"})
                journal_store.save(transaction)
                try:
                    uv, _ = self.bootstrap.resolve_uv(root)
                    self.bootstrap.sync(root, uv, timeout_seconds)
                except (OSError, ValueError, ToolingError):
                    journal_store.save(
                        transaction.model_copy(update={"phase": "dependency_failed"})
                    )
                    return self._failed_result(
                        resolved,
                        operation_id,
                        branch,
                        remote,
                        True,
                        pre_head,
                        post_head,
                        remote_head,
                        "Git обновлён, но dependency postcondition не подтверждён; transaction оставлена для recovery.",
                        transaction_id=transaction.transaction_id,
                    )
                transaction = transaction.model_copy(update={"phase": "completed"})
                journal_store.save(transaction)
            return ToolingResult[UpdateDetails, UpdateEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Git обновлён только fast-forward; postcondition подтверждён.",
                operation_id=operation_id,
                details=UpdateDetails(
                    status=OperationState.READY,
                    branch=branch,
                    remote=remote,
                    dependency_changed=dependency_changed,
                    fast_forwarded=True,
                ),
                warnings=(
                    ToolingWarning(
                        code=WarningCode.TOOLING_POSTGRES_BACKUP_NOT_RUN,
                        message="PostgreSQL backup не запускался: database adapter не входит в этот core increment.",
                    ),
                ),
                evidence=UpdateEvidence(
                    repository=resolved.evidence,
                    pre_head=pre_head,
                    post_head=post_head,
                    remote_head=remote_head,
                    transaction_id=transaction.transaction_id if transaction else None,
                ),
            )
        finally:
            lock.release()

    @staticmethod
    def _failed_result(
        resolved: ResolvedRepository,
        operation_id: str,
        branch: str,
        remote: str,
        dependency_changed: bool,
        pre_head: str,
        post_head: str,
        remote_head: str,
        message: str,
        transaction_id: str | None = None,
    ) -> ToolingResult[UpdateDetails, UpdateEvidence]:
        return ToolingResult[UpdateDetails, UpdateEvidence](
            ok=False,
            code=ResultCode.TOOLING_VERIFICATION_UNKNOWN,
            state=OperationState.UNKNOWN,
            message=message,
            operation_id=operation_id,
            details=UpdateDetails(
                status=OperationState.UNKNOWN,
                branch=branch,
                remote=remote,
                dependency_changed=dependency_changed,
                fast_forwarded=True,
            ),
            evidence=UpdateEvidence(
                repository=resolved.evidence,
                pre_head=pre_head,
                post_head=post_head,
                remote_head=remote_head,
                transaction_id=transaction_id,
            ),
        )


__all__ = ["UpdateService"]
