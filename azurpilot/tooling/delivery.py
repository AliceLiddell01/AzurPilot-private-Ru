"""Fail-closed Git delivery с явным scope, exact refs и read-only recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import which
from typing import Any

from .contracts import (
    AnalysisScope,
    BranchIdentity,
    CommitIdentity,
    DeliveryChange,
    DeliveryDetails,
    DeliveryEvidence,
    DeliveryJournal,
    DeliveryManifest,
    DeliveryPhase,
    DeliveryTarget,
    GitRange,
    GitSnapshot,
    OperationState,
    PublicationIntent,
    RemoteIdentity,
    RepositoryIdentity,
    ResultCode,
    ToolingResult,
)
from .errors import ToolingError
from .filesystem import (
    ScopedPath,
    StateLayout,
    bounded_read_text,
    canonical_path,
    path_has_link,
    path_identity,
    sha256_file,
)
from .git import GitClient, is_ad_hoc_remote_ref, repository_identity_from_remote
from .process import ProcessResult, ProcessSpec, StructuredProcessRunner
from .repository import RepositoryResolver, ResolvedRepository

_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SAFE_REMOTE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_MAX_MANIFEST_BYTES = 512 * 1024
_DELIVERY_STATE_NAME = "state.json"


def _error(code: ResultCode, message: str, *, state: OperationState = OperationState.FAILED) -> ToolingError:
    return ToolingError(code, message, state=state)


def load_delivery_manifest(path: str | os.PathLike[str]) -> DeliveryManifest:
    """Прочитать закрытый JSON manifest без fallback на cwd или свободные поля."""

    manifest_path = Path(path).expanduser()
    try:
        if not manifest_path.is_absolute() or path_has_link(manifest_path):
            raise _error(
                ResultCode.TOOLING_MANIFEST_INVALID,
                "Manifest должен быть абсолютным обычным файлом без symlink/reparse point.",
            )
        document = json.loads(
            bounded_read_text(manifest_path, max_bytes=_MAX_MANIFEST_BYTES)
        )
        return DeliveryManifest.model_validate(document)
    except ToolingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _error(
            ResultCode.TOOLING_MANIFEST_INVALID,
            "Не удалось разобрать закрытый delivery manifest.",
        ) from exc


@dataclass(frozen=True)
class _ValidatedDelivery:
    manifest: DeliveryManifest
    repository: ResolvedRepository
    git: GitClient
    snapshot: GitSnapshot
    targets: tuple[DeliveryTarget, ...]
    target_paths: tuple[str, ...]
    initial_staged_paths: tuple[str, ...]
    postimage_candidates: tuple[_PostimageCandidate, ...]
    changes: tuple[DeliveryChange, ...]
    publication_remote: RemoteIdentity
    base_remote: RemoteIdentity
    branch_identity: BranchIdentity
    layout: StateLayout


@dataclass(frozen=True)
class _PostimageCandidate:
    """Снимок raw и Git-clean postimage до начала staging."""

    path: str
    raw_sha256: str | None
    raw_size: int | None
    filtered_blob: str | None


class DeliveryJournalStore:
    """Небольшое typed-хранилище delivery state поверх существующего StateLayout."""

    def __init__(self, layout: StateLayout) -> None:
        self.layout = layout

    def _directory(self, operation_id: str) -> Path:
        if (
            not operation_id
            or Path(operation_id).name != operation_id
            or len(operation_id) > 80
        ):
            raise _error(
                ResultCode.TOOLING_MANIFEST_INVALID,
                "Идентификатор delivery-операции имеет небезопасный формат.",
            )
        return self.layout.transactions_directory / operation_id

    def save(self, journal: DeliveryJournal) -> None:
        self.layout.ensure()
        directory = self._directory(journal.operation_id)
        if directory.exists() and path_has_link(directory):
            raise _error(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Каталог delivery-транзакции содержит symlink/reparse point.",
            )
        directory.mkdir(parents=True, exist_ok=True)
        ScopedPath(directory).atomic_write_text(
            _DELIVERY_STATE_NAME,
            journal.model_dump_json(indent=2),
        )

    def load(self, operation_id: str) -> DeliveryJournal:
        state_path = self._directory(operation_id) / _DELIVERY_STATE_NAME
        try:
            return DeliveryJournal.model_validate_json(
                bounded_read_text(state_path, max_bytes=128 * 1024)
            )
        except ToolingError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise _error(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние delivery-транзакции повреждено или недоступно.",
            ) from exc


class GitleaksScanner:
    """Gitleaks только по staged index или exact committed Git range."""

    def __init__(
        self,
        root: Path,
        runner: StructuredProcessRunner | None = None,
    ) -> None:
        self.root = canonical_path(root)
        self.runner = runner or StructuredProcessRunner()
        self.executable = which("gitleaks")

    def _run(self, args: tuple[str, ...]) -> ProcessResult:
        if self.executable is None:
            raise _error(
                ResultCode.TOOLING_SECRET_SCANNER_UNAVAILABLE,
                "Gitleaks не найден; публикация заблокирована.",
            )
        result = self.runner.run(
            ProcessSpec(
                executable=self.executable,
                argv=args,
                cwd=self.root,
                timeout_seconds=15 * 60,
                max_output_bytes=128 * 1024,
                env={"NO_COLOR": "1"},
            )
        )
        if result.timed_out:
            raise _error(
                ResultCode.TOOLING_SECRET_SCAN_FAILED,
                "Gitleaks не завершил scoped scan в установленный срок.",
                state=OperationState.UNKNOWN,
            )
        if result.stdout_truncated or result.stderr_truncated:
            raise _error(
                ResultCode.TOOLING_SECRET_SCAN_FAILED,
                "Gitleaks вернул усечённый отчёт; scope не подтверждён.",
            )
        report_text = result.stdout.strip()
        if not report_text:
            raise _error(
                ResultCode.TOOLING_SECRET_SCAN_FAILED,
                "Gitleaks не вернул machine-readable report.",
            )
        try:
            report = json.loads(report_text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _error(
                ResultCode.TOOLING_SECRET_SCAN_FAILED,
                "Gitleaks вернул повреждённый machine-readable report.",
            ) from exc
        if not isinstance(report, list):
            raise _error(
                ResultCode.TOOLING_SECRET_SCAN_FAILED,
                "Gitleaks report имеет неожиданную структуру.",
            )
        if report:
            raise _error(
                ResultCode.TOOLING_SECRET_SCAN_FAILED,
                "Gitleaks обнаружил finding в заявленном scope.",
            )
        if result.returncode == 1:
            raise _error(
                ResultCode.TOOLING_SECRET_SCAN_FAILED,
                "Gitleaks обнаружил finding в заявленном scope.",
            )
        if result.returncode != 0:
            raise _error(
                ResultCode.TOOLING_SECRET_SCAN_FAILED,
                "Gitleaks завершился с ошибкой; scope не подтверждён.",
            )
        return result

    def scan_staged(self) -> None:
        # --staged читает только index. До вызова service уже доказал, что в
        # index нет путей вне manifest allowlist.
        self._run(
            (
                "git",
                "--staged",
                "--redact",
                "--no-banner",
                "--no-color",
                "--report-format=json",
                "--report-path=-",
                ".",
            )
        )

    def scan_committed_range(self, start_sha: str, end_sha: str) -> None:
        self._run(
            (
                "git",
                f"--log-opts={start_sha}..{end_sha}",
                "--redact",
                "--no-banner",
                "--no-color",
                "--report-format=json",
                "--report-path=-",
                ".",
            )
        )


class DeliveryService:
    """Операции validate/publish/status/recover для explicit working-tree scope."""

    def __init__(
        self,
        *,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
        git_factory: type[GitClient] = GitClient,
        scanner_factory: type[GitleaksScanner] = GitleaksScanner,
        allow_non_hosted_remote: bool = False,
    ) -> None:
        self.runner = runner or StructuredProcessRunner()
        self.resolver = resolver or RepositoryResolver(runner=self.runner)
        self.git_factory = git_factory
        self.scanner_factory = scanner_factory
        self.allow_non_hosted_remote = allow_non_hosted_remote

    def validate(
        self,
        manifest_path: str | os.PathLike[str],
        repository_root: str | os.PathLike[str] | None = None,
    ) -> ToolingResult[DeliveryDetails, DeliveryEvidence]:
        manifest = load_delivery_manifest(manifest_path)
        context = self._validate(manifest, repository_root)
        return self._result(context, phase=DeliveryPhase.VALIDATED)

    def publish(
        self,
        manifest_path: str | os.PathLike[str],
        repository_root: str | os.PathLike[str] | None = None,
    ) -> ToolingResult[DeliveryDetails, DeliveryEvidence]:
        manifest = load_delivery_manifest(manifest_path)
        if manifest.publication_intent is not PublicationIntent.COMMIT_AND_PUSH:
            raise _error(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Publish требует publication_intent=commit_and_push.",
            )
        context = self._validate(manifest, repository_root)
        operation_id = f"delivery-{secrets.token_hex(12)}"
        store = DeliveryJournalStore(context.layout)
        journal = DeliveryJournal(
            operation_id=operation_id,
            repository_root_identity=path_identity(context.repository.path),
            phase=DeliveryPhase.VALIDATED,
            branch=manifest.expected_branch,
            remote_name=manifest.remote_name,
            remote_branch=manifest.remote_branch,
            expected_local_head=manifest.expected_local_head,
            expected_base_sha=manifest.expected_base_sha,
            expected_remote_sha=manifest.expected_remote_sha,
            target_paths=context.target_paths,
            changes=context.changes,
            updated_at=_now(),
        )
        store.save(journal)
        owns_staging = set(context.initial_staged_paths)
        staging_attempted = False
        scanner = self.scanner_factory(context.repository.path, self.runner)
        scans: list[AnalysisScope] = []

        try:
            self._verify_preexisting_staged(context)
            paths_to_stage = tuple(
                path
                for path in context.target_paths
                if path not in context.initial_staged_paths
            )
            if paths_to_stage:
                staging_attempted = True
                context.git.stage(paths_to_stage)
            journal = journal.model_copy(
                update={"phase": DeliveryPhase.STAGED, "updated_at": _now()}
            )
            store.save(journal)
            self._verify_staged(context)
            scanner.scan_staged()
            scans.append(
                AnalysisScope(paths=context.target_paths, mode="staged")
            )
            journal = journal.model_copy(
                update={
                    "phase": DeliveryPhase.PRE_COMMIT_SCANNED,
                    "updated_at": _now(),
                }
            )
            store.save(journal)

            commit_sha = context.git.commit(manifest.commit_message)
            self._verify_commit(context, commit_sha)
            commit = CommitIdentity(
                sha=commit_sha,
                parent_sha=context.git.commit_parent(commit_sha),
            )
            journal = journal.model_copy(
                update={
                    "phase": DeliveryPhase.COMMITTED,
                    "commit_sha": commit_sha,
                    "updated_at": _now(),
                }
            )
            store.save(journal)

            context.git.commits_in_range(manifest.expected_base_sha, commit_sha)
            scanner.scan_committed_range(manifest.expected_base_sha, commit_sha)
            scans.append(
                AnalysisScope(
                    paths=(),
                    git_range=GitRange(
                        start_sha=manifest.expected_base_sha,
                        end_sha=commit_sha,
                    ),
                    mode="committed_range",
                )
            )
            journal = journal.model_copy(
                update={
                    "phase": DeliveryPhase.COMMITTED_RANGE_SCANNED,
                    "updated_at": _now(),
                }
            )
            store.save(journal)

            current_remote = context.git.remote_ref(
                manifest.remote_name, manifest.remote_branch
            )
            if current_remote != manifest.expected_remote_sha:
                self._save_failure(
                    store,
                    journal,
                    ResultCode.TOOLING_REMOTE_REF_CONFLICT,
                    "Remote ref изменился после validate; push остановлен.",
                )
                raise _error(
                    ResultCode.TOOLING_REMOTE_REF_CONFLICT,
                    "Remote ref изменился после validate; push остановлен.",
                    state=OperationState.CONFLICT,
                )

            journal = journal.model_copy(
                update={
                    "phase": DeliveryPhase.PUSH_IN_FLIGHT,
                    "updated_at": _now(),
                }
            )
            store.save(journal)
            try:
                context.git.push(
                    manifest.remote_name,
                    manifest.expected_branch,
                    manifest.remote_branch,
                )
            except ToolingError as push_error:
                return self._resolve_push_result(
                    context,
                    store,
                    journal,
                    commit,
                    scans,
                    push_error,
                )

            remote_sha = context.git.remote_ref(
                manifest.remote_name, manifest.remote_branch
            )
            if remote_sha != commit_sha:
                self._save_failure(
                    store,
                    journal,
                    ResultCode.TOOLING_PUSH_UNKNOWN,
                    "После push exact remote SHA не подтверждён.",
                    phase=DeliveryPhase.UNKNOWN,
                )
                raise _error(
                    ResultCode.TOOLING_PUSH_UNKNOWN,
                    "После push exact remote SHA не подтверждён; blind retry запрещён.",
                    state=OperationState.IN_FLIGHT,
                )
            journal = journal.model_copy(
                update={
                    "phase": DeliveryPhase.DELIVERED,
                    "updated_at": _now(),
                }
            )
            store.save(journal)
            return self._result(
                context,
                phase=DeliveryPhase.DELIVERED,
                commit=commit,
                remote_sha=remote_sha,
                scans=tuple(scans),
                operation_id=operation_id,
                message="Изменения опубликованы; exact remote SHA подтверждён.",
            )
        except ToolingError as error:
            if staging_attempted or journal.phase in {
                DeliveryPhase.STAGED,
                DeliveryPhase.PRE_COMMIT_SCANNED,
            }:
                self._unstage_owned(context, owns_staging)
            if journal.phase not in {
                DeliveryPhase.DELIVERED,
                DeliveryPhase.PUSH_IN_FLIGHT,
                DeliveryPhase.UNKNOWN,
            }:
                self._save_failure(
                    store,
                    journal,
                    error.code,
                    error.message,
                )
            raise

    def status(
        self,
        operation_id: str,
        repository_root: str | os.PathLike[str] | None = None,
    ) -> ToolingResult[DeliveryDetails, Any]:
        root = self.resolver.resolve(repository_root)
        journal = DeliveryJournalStore(StateLayout.for_repository(root.path)).load(
            operation_id
        )
        self._check_journal_root(root, journal)
        terminal_phase = journal.phase in {
            DeliveryPhase.FAILED,
            DeliveryPhase.PUSH_NOT_DELIVERED,
        }
        if terminal_phase:
            code = journal.last_error_code or ResultCode.TOOLING_VERIFICATION_UNKNOWN
            return ToolingResult(
                ok=False,
                code=code,
                state=OperationState.FAILED,
                message=journal.last_error_message
                or f"Delivery завершилась на терминальной фазе {journal.phase.value}.",
                operation_id=operation_id,
                details=DeliveryDetails(
                    phase=journal.phase,
                    target_paths=journal.target_paths,
                    target_count=len(journal.target_paths),
                    changes=journal.changes,
                    commit_sha=journal.commit_sha,
                    recovery_required=False,
                ),
            )
        recovery_required = journal.phase in {
            DeliveryPhase.PUSH_IN_FLIGHT,
            DeliveryPhase.UNKNOWN,
        }
        return ToolingResult(
            ok=journal.phase is DeliveryPhase.DELIVERED,
            code=(
                ResultCode.OK
                if journal.phase is DeliveryPhase.DELIVERED
                else ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED
            ),
            state=(
                OperationState.READY
                if journal.phase is DeliveryPhase.DELIVERED
                else OperationState.IN_FLIGHT
                if journal.phase is DeliveryPhase.PUSH_IN_FLIGHT
                else OperationState.UNKNOWN
            ),
            message=f"Delivery phase: {journal.phase.value}.",
            operation_id=operation_id,
            details=DeliveryDetails(
                phase=journal.phase,
                target_paths=journal.target_paths,
                target_count=len(journal.target_paths),
                changes=journal.changes,
                commit_sha=journal.commit_sha,
                recovery_required=recovery_required,
            ),
        )

    def recover(
        self,
        operation_id: str,
        repository_root: str | os.PathLike[str] | None = None,
    ) -> ToolingResult[DeliveryDetails, Any]:
        root = self.resolver.resolve(repository_root)
        layout = StateLayout.for_repository(root.path)
        store = DeliveryJournalStore(layout)
        journal = store.load(operation_id)
        self._check_journal_root(root, journal)
        if journal.phase is not DeliveryPhase.PUSH_IN_FLIGHT:
            return self.status(operation_id, repository_root)
        git = self.git_factory(root.path, self.runner)
        remote_sha = git.remote_ref(journal.remote_name, journal.remote_branch)
        if remote_sha == journal.commit_sha:
            journal = journal.model_copy(
                update={"phase": DeliveryPhase.DELIVERED, "updated_at": _now()}
            )
            store.save(journal)
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Read-only recovery подтвердил доставленный commit.",
                operation_id=operation_id,
                details=DeliveryDetails(
                    phase=journal.phase,
                    target_paths=journal.target_paths,
                    target_count=len(journal.target_paths),
                    changes=journal.changes,
                    commit_sha=journal.commit_sha,
                    remote_sha=remote_sha,
                ),
            )
        if remote_sha == journal.expected_remote_sha:
            self._save_failure(
                store,
                journal,
                ResultCode.TOOLING_PUSH_UNKNOWN,
                "Remote сохранил старый SHA; повторный push запрещён.",
                phase=DeliveryPhase.PUSH_NOT_DELIVERED,
            )
            raise _error(
                ResultCode.TOOLING_PUSH_UNKNOWN,
                "Read-only recovery доказал, что commit не доставлен; blind retry запрещён.",
                state=OperationState.IN_FLIGHT,
            )
        self._save_failure(
            store,
            journal,
            ResultCode.TOOLING_REMOTE_REF_CONFLICT,
            "Remote ref содержит третий SHA; требуется ручное разрешение конфликта.",
            phase=DeliveryPhase.UNKNOWN,
        )
        raise _error(
            ResultCode.TOOLING_REMOTE_REF_CONFLICT,
            "Remote ref содержит третий SHA; delivery оставлена в unknown.",
            state=OperationState.UNKNOWN,
        )

    def _validate(
        self,
        manifest: DeliveryManifest,
        repository_root: str | os.PathLike[str] | None,
    ) -> _ValidatedDelivery:
        if not _SAFE_REMOTE.fullmatch(manifest.remote_name):
            raise _error(
                ResultCode.TOOLING_MANIFEST_INVALID,
                "Имя remote имеет небезопасный формат.",
            )
        if not _SAFE_REMOTE.fullmatch(manifest.base_remote_name):
            raise _error(
                ResultCode.TOOLING_MANIFEST_INVALID,
                "Имя base remote имеет небезопасный формат.",
            )
        _validate_ref(manifest.expected_branch)
        _validate_ref(manifest.remote_branch)
        _validate_ref(manifest.base_branch)
        if is_ad_hoc_remote_ref(manifest.base_branch) or is_ad_hoc_remote_ref(
            manifest.remote_branch
        ):
            raise _error(
                ResultCode.TOOLING_AD_HOC_REMOTE_TOPOLOGY,
                "Delivery не принимает temporary/scratch/transport remote ref; "
                "parent и feature branch должны публиковаться canonical workflow-ом.",
                state=OperationState.CONFLICT,
            )
        if manifest.remote_branch != manifest.expected_branch:
            raise _error(
                ResultCode.TOOLING_AD_HOC_REMOTE_TOPOLOGY,
                "Delivery remote_branch обязан совпадать с реальной local branch; "
                "вспомогательная remote publication запрещена.",
                state=OperationState.CONFLICT,
            )
        repository = self.resolver.resolve(repository_root)
        git = self.git_factory(repository.path, self.runner)
        if (
            manifest.repository.host.casefold() == "local"
            and not self.allow_non_hosted_remote
        ):
            raise _error(
                ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                "Delivery для local/file remote разрешён только в явном disposable fixture режиме.",
            )
        publication_remote = _resolve_remote_identity(
            git,
            manifest.remote_name,
            manifest.repository,
            verify_push_url=True,
        )
        base_remote = _resolve_remote_identity(
            git,
            manifest.base_remote_name,
            manifest.repository,
            verify_push_url=False,
        )
        branch_identity = BranchIdentity(name=git.branch(), sha=git.head())
        if branch_identity.name != manifest.expected_branch:
            raise _error(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Текущая ветка не совпадает с manifest expected_branch.",
            )
        if branch_identity.sha != manifest.expected_local_head:
            raise _error(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Локальный HEAD не совпадает с manifest expected_local_head.",
            )
        if git.active_operation():
            raise _error(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "В checkout уже выполняется незавершённая Git-операция.",
                state=OperationState.CONFLICT,
            )
        base_sha = git.remote_ref(manifest.base_remote_name, manifest.base_branch)
        if base_sha != manifest.expected_base_sha:
            parent_is_local = False
            parent_remote_is_behind = False
            try:
                parent_is_local = git.object_exists(manifest.expected_base_sha)
                if parent_is_local:
                    parent_remote_is_behind = not git.is_ancestor(
                        manifest.expected_base_sha, base_sha
                    )
            except ToolingError:
                parent_is_local = False
                parent_remote_is_behind = False
            if (
                parent_is_local
                and parent_remote_is_behind
                and manifest.base_branch != manifest.expected_branch
            ):
                raise _error(
                    ResultCode.TOOLING_STACKED_PARENT_UNPUBLISHED,
                    "Exact parent branch remote SHA отличается от local parent HEAD; "
                    "сначала опубликуйте реальную parent branch через canonical delivery "
                    "workflow. Temporary remote ref создавать нельзя.",
                    state=OperationState.CONFLICT,
                )
            raise _error(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Exact base SHA remote не совпадает с manifest.",
            )
        if not git.is_ancestor(manifest.expected_base_sha, branch_identity.sha):
            raise _error(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Ожидаемый base SHA не является предком local HEAD.",
            )
        remote_sha = git.remote_ref(manifest.remote_name, manifest.remote_branch)
        if remote_sha != manifest.expected_remote_sha:
            raise _error(
                ResultCode.TOOLING_REMOTE_REF_CONFLICT,
                "Exact remote SHA не совпадает с manifest expected_remote_sha.",
                state=OperationState.CONFLICT,
            )
        normalized_targets = _normalize_targets(manifest.targets, repository.path)
        target_paths = tuple(target.path for target in normalized_targets)
        changes = tuple(_delivery_change(target) for target in normalized_targets)
        status_z = git.status_z()
        dirty_paths, staged_paths = _parse_status(status_z)
        unexpected_staged = set(staged_paths) - set(target_paths)
        if unexpected_staged:
            raise _error(
                ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
                "В index есть staged paths вне manifest allowlist.",
                state=OperationState.CONFLICT,
            )
        postimage_candidates: list[_PostimageCandidate] = []
        for target in normalized_targets:
            _verify_target_preimage(git, branch_identity.sha, target, repository.path)
            postimage_candidates.append(
                _verify_target_postimage(git, target, repository.path)
            )
        snapshot = GitSnapshot(
            repository=manifest.repository,
            root_identity=path_identity(repository.path),
            branch=branch_identity.name,
            head_sha=branch_identity.sha,
            base_sha=manifest.expected_base_sha,
            base_branch=manifest.base_branch,
            remote_name=manifest.remote_name,
            remote_branch=manifest.remote_branch,
            remote_sha=remote_sha,
            upstream=_safe_upstream(git),
            dirty_paths=dirty_paths,
            staged_paths=staged_paths,
            active_operation=False,
        )
        return _ValidatedDelivery(
            manifest=manifest,
            repository=repository,
            git=git,
            snapshot=snapshot,
            targets=normalized_targets,
            target_paths=target_paths,
            initial_staged_paths=staged_paths,
            postimage_candidates=tuple(postimage_candidates),
            changes=changes,
            publication_remote=publication_remote,
            base_remote=base_remote,
            branch_identity=branch_identity,
            layout=StateLayout.for_repository(repository.path),
        )

    def _verify_staged(self, context: _ValidatedDelivery) -> None:
        actual_paths = context.git.staged_paths()
        if actual_paths != tuple(sorted(context.target_paths)):
            raise _error(
                ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
                "Фактически staged paths не совпали с manifest allowlist.",
                state=OperationState.CONFLICT,
            )
        for target, candidate in zip(
            context.targets, context.postimage_candidates, strict=True
        ):
            self._verify_staged_target(context, target, candidate)

    def _verify_preexisting_staged(self, context: _ValidatedDelivery) -> None:
        """Проверить разрешённые staged paths до возможного service-owned staging."""

        initial = set(context.initial_staged_paths)
        for target, candidate in zip(
            context.targets, context.postimage_candidates, strict=True
        ):
            if target.path in initial:
                self._verify_staged_target(context, target, candidate)

    @staticmethod
    def _verify_staged_target(
        context: _ValidatedDelivery,
        target: DeliveryTarget,
        candidate: _PostimageCandidate,
    ) -> None:
        current = _capture_target_postimage(context.git, target, context.repository.path)
        if (
            current.raw_sha256 != candidate.raw_sha256
            or current.raw_size != candidate.raw_size
            or current.filtered_blob != candidate.filtered_blob
        ):
            raise _error(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                f"Target {target.path!r} изменился после проверки postimage; staging остановлен.",
            )
        if target.postimage.exists:
            if current.raw_sha256 != target.postimage.sha256 or (
                target.postimage.size is not None
                and current.raw_size != target.postimage.size
            ):
                raise _error(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    f"Raw postimage пути {target.path!r} не совпал с manifest.",
                )
            staged_blob = context.git.index_blob(target.path)
            if staged_blob != candidate.filtered_blob:
                raise _error(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    f"Index path {target.path!r} не совпал с сохранённым Git-clean postimage.",
                )
        elif context.git.object_exists(f":{target.path}"):
            raise _error(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                f"Удаляемый путь {target.path!r} остался в index.",
            )

    def _verify_commit(
        self, context: _ValidatedDelivery, commit_sha: str
    ) -> None:
        if context.git.commit_parent(commit_sha) != context.manifest.expected_local_head:
            raise _error(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Parent нового commit не совпал с ожидаемым local HEAD.",
            )
        paths = context.git.commit_paths(commit_sha)
        if paths != tuple(sorted(context.target_paths)):
            raise _error(
                ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
                "Commit diff содержит пути вне manifest allowlist.",
            )

    def _unstage_owned(
        self, context: _ValidatedDelivery, initial_staged: set[str]
    ) -> None:
        owned = tuple(path for path in context.target_paths if path not in initial_staged)
        try:
            context.git.unstage(owned)
        except ToolingError:
            # Ошибка cleanup не должна скрывать первичную ошибку candidate.
            return

    def _result(
        self,
        context: _ValidatedDelivery,
        *,
        phase: DeliveryPhase,
        commit: CommitIdentity | None = None,
        remote_sha: str | None = None,
        scans: tuple[AnalysisScope, ...] = (),
        operation_id: str | None = None,
        message: str = "Manifest и exact repository state подтверждены.",
    ) -> ToolingResult[DeliveryDetails, DeliveryEvidence]:
        return ToolingResult(
            ok=phase is DeliveryPhase.VALIDATED or phase is DeliveryPhase.DELIVERED,
            code=ResultCode.OK,
            state=OperationState.READY,
            message=message,
            operation_id=operation_id,
            details=DeliveryDetails(
                phase=phase,
                target_paths=context.target_paths,
                target_count=len(context.target_paths),
                changes=context.changes,
                commit_sha=commit.sha if commit else None,
                remote_sha=remote_sha,
            ),
            evidence=DeliveryEvidence(
                snapshot=context.snapshot,
                commit=commit,
                scans=scans,
                publication_remote=context.publication_remote,
                base_remote=context.base_remote,
                branch=context.branch_identity,
            ),
        )

    def _resolve_push_result(
        self,
        context: _ValidatedDelivery,
        store: DeliveryJournalStore,
        journal: DeliveryJournal,
        commit: CommitIdentity,
        scans: list[AnalysisScope],
        push_error: ToolingError,
    ) -> ToolingResult[DeliveryDetails, DeliveryEvidence]:
        try:
            remote_sha = context.git.remote_ref(
                journal.remote_name, journal.remote_branch
            )
        except ToolingError as read_error:
            self._save_failure(
                store,
                journal,
                ResultCode.TOOLING_PUSH_UNKNOWN,
                "Результат push неизвестен, remote ref прочитать не удалось.",
                phase=DeliveryPhase.PUSH_IN_FLIGHT,
            )
            raise _error(
                ResultCode.TOOLING_PUSH_UNKNOWN,
                "Результат push неизвестен; сначала выполните read-only recover.",
                state=OperationState.IN_FLIGHT,
            ) from read_error
        if remote_sha == commit.sha:
            delivered = journal.model_copy(
                update={"phase": DeliveryPhase.DELIVERED, "updated_at": _now()}
            )
            store.save(delivered)
            return self._result(
                context,
                phase=DeliveryPhase.DELIVERED,
                commit=commit,
                remote_sha=remote_sha,
                scans=tuple(scans),
                operation_id=journal.operation_id,
                message="Push завершился неоднозначно, но remote exact SHA подтверждён.",
            )
        if remote_sha == journal.expected_remote_sha:
            self._save_failure(
                store,
                journal,
                ResultCode.TOOLING_PUSH_UNKNOWN,
                "Remote сохранил прежний SHA после не подтверждённого push.",
                phase=DeliveryPhase.PUSH_NOT_DELIVERED,
            )
            raise _error(
                ResultCode.TOOLING_PUSH_UNKNOWN,
                "Commit не доставлен; повторный push вслепую запрещён.",
                state=OperationState.IN_FLIGHT,
            ) from push_error
        self._save_failure(
            store,
            journal,
            ResultCode.TOOLING_REMOTE_REF_CONFLICT,
            "Remote ref содержит третий SHA после не подтверждённого push.",
            phase=DeliveryPhase.UNKNOWN,
        )
        raise _error(
            ResultCode.TOOLING_REMOTE_REF_CONFLICT,
            "Remote ref содержит конфликтующий SHA; требуется ручное решение.",
            state=OperationState.UNKNOWN,
        ) from push_error

    def _save_failure(
        self,
        store: DeliveryJournalStore,
        journal: DeliveryJournal,
        code: ResultCode,
        message: str,
        *,
        phase: DeliveryPhase = DeliveryPhase.FAILED,
    ) -> None:
        try:
            store.save(
                journal.model_copy(
                    update={
                        "phase": phase,
                        "updated_at": _now(),
                        "last_error_code": code,
                        "last_error_message": message,
                    }
                )
            )
        except ToolingError:
            return

    @staticmethod
    def _check_journal_root(root: ResolvedRepository, journal: DeliveryJournal) -> None:
        if journal.repository_root_identity != path_identity(root.path):
            raise _error(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Delivery journal относится к другому repository root.",
            )


def _normalize_targets(
    targets: tuple[DeliveryTarget, ...], root: Path
) -> tuple[DeliveryTarget, ...]:
    if len(targets) > 128:
        raise _error(
            ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
            "Manifest содержит слишком много target paths.",
        )
    scoped = ScopedPath(root)
    normalized: list[DeliveryTarget] = []
    seen: set[str] = set()
    for target in targets:
        path = _normalize_relative_path(target.path)
        if path in seen:
            raise _error(
                ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
                "Manifest содержит duplicate target path.",
            )
        scoped.resolve(path, allow_missing=True)
        seen.add(path)
        normalized.append(target.model_copy(update={"path": path}))
    for index, path in enumerate(sorted(seen)):
        prefix = path + "/"
        if any(other.startswith(prefix) for other in sorted(seen)[index + 1 :]):
            raise _error(
                ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
                "Manifest содержит overlapping target paths.",
            )
    return tuple(sorted(normalized, key=lambda target: target.path))


def _normalize_relative_path(value: str) -> str:
    if (
        not value
        or "\x00" in value
        or Path(value).is_absolute()
        or value.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise _error(
            ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
            "Target path должен быть repository-relative.",
        )
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts) or ":" in normalized:
        raise _error(
            ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
            "Target path содержит traversal, пустой сегмент или двоеточие.",
        )
    return normalized


def _verify_target_preimage(
    git: GitClient,
    head: str,
    target: DeliveryTarget,
    root: Path,
) -> None:
    if target.preimage.exists:
        content = git.object_bytes(f"{head}:{target.path}")
        actual = hashlib.sha256(content).hexdigest()
        actual_size = len(content)
        exists = True
    else:
        exists = git.object_exists(f"{head}:{target.path}")
        actual = None
        actual_size = None
    if exists != target.preimage.exists or (
        exists and (actual != target.preimage.sha256 or target.preimage.size not in {None, actual_size})
    ):
        raise _error(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Preimage пути {target.path!r} не совпадает с exact HEAD.",
        )


def _capture_target_postimage(
    git: GitClient, target: DeliveryTarget, root: Path
) -> _PostimageCandidate:
    path = ScopedPath(root).resolve(target.path, allow_missing=True)
    if target.postimage.exists:
        if not path.is_file() or path_has_link(path):
            raise _error(
                ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
                f"Postimage пути {target.path!r} не является обычным файлом.",
            )
        actual = sha256_file(path)
        size = path.stat().st_size
        return _PostimageCandidate(
            path=target.path,
            raw_sha256=actual,
            raw_size=size,
            filtered_blob=git.filtered_working_blob(target.path),
        )
    if path.exists():
        raise _error(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Путь {target.path!r} должен быть удалён согласно postimage.",
        )
    return _PostimageCandidate(
        path=target.path,
        raw_sha256=None,
        raw_size=None,
        filtered_blob=None,
    )


def _verify_target_postimage(
    git: GitClient, target: DeliveryTarget, root: Path
) -> _PostimageCandidate:
    candidate = _capture_target_postimage(git, target, root)
    if target.postimage.exists and (
        candidate.raw_sha256 != target.postimage.sha256
        or (
            target.postimage.size is not None
            and candidate.raw_size != target.postimage.size
        )
    ):
        raise _error(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            f"Postimage пути {target.path!r} изменился после подготовки manifest.",
        )
    return candidate


def _delivery_change(target: DeliveryTarget) -> DeliveryChange:
    if not target.preimage.exists and target.postimage.exists:
        change = "A"
    elif target.preimage.exists and not target.postimage.exists:
        change = "D"
    elif target.preimage.exists and target.postimage.exists:
        change = "M"
    else:
        raise _error(
            ResultCode.TOOLING_DELIVERY_SCOPE_INVALID,
            f"Target {target.path!r} не содержит изменения между preimage и postimage.",
        )
    return DeliveryChange(path=target.path, change=change)


def _resolve_remote_identity(
    git: GitClient,
    remote_name: str,
    expected: RepositoryIdentity,
    *,
    verify_push_url: bool,
) -> RemoteIdentity:
    fetch_url = git.remote_url(remote_name)
    actual = repository_identity_from_remote(fetch_url)
    if not _same_repository(actual, expected):
        raise _error(
            ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
            f"Git remote {remote_name!r} не совпадает с canonical repository identity.",
        )
    push_url: str | None = None
    if verify_push_url:
        push_url = git.remote_push_url(remote_name)
        if push_url and push_url.upper() != "DISABLED":
            push_identity = repository_identity_from_remote(push_url)
            if not _same_repository(push_identity, expected):
                raise _error(
                    ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                    "Git pushurl не совпадает с canonical repository identity.",
                )
    return RemoteIdentity(
        name=remote_name,
        fetch_url=fetch_url,
        push_url=push_url,
        repository=actual,
    )


def _parse_status(status_z: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    dirty: set[str] = set()
    staged: set[str] = set()
    items = [item for item in status_z.split("\x00") if item]
    index = 0
    while index < len(items):
        item = items[index]
        if len(item) < 4:
            index += 1
            continue
        code = item[:2]
        path = item[3:]
        dirty.add(path)
        # Код `??` означает неотслеживаемое содержимое working tree, а не мутацию index.
        if code[0] not in {" ", "?"}:
            staged.add(path)
        if code[1] != " ":
            dirty.add(path)
        if code[0] in {"R", "C"} and index + 1 < len(items):
            index += 1
            dirty.add(items[index])
            if code[0] not in {" ", "?"}:
                staged.add(items[index])
        index += 1
    return tuple(sorted(dirty)), tuple(sorted(staged))


def _validate_ref(value: str) -> None:
    if (
        not _SAFE_REF.fullmatch(value)
        or value.startswith("/")
        or value.endswith(("/", "."))
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(character in value for character in "~^:?*[\\")
    ):
        raise _error(
            ResultCode.TOOLING_MANIFEST_INVALID,
            "Git ref имеет небезопасный формат.",
        )


def _same_repository(left: RepositoryIdentity, right: RepositoryIdentity) -> bool:
    return (
        left.host.casefold() == right.host.casefold()
        and left.owner.casefold() == right.owner.casefold()
        and left.repository.casefold() == right.repository.casefold()
    )


def _safe_upstream(git: GitClient) -> str | None:
    try:
        return git.upstream()
    except ToolingError:
        return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = [
    "DeliveryJournalStore",
    "DeliveryService",
    "GitleaksScanner",
    "load_delivery_manifest",
]
