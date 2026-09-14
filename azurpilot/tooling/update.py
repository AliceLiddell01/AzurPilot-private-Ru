"""Безопасный fast-forward Update с резервной копией и внешней транзакцией."""

from __future__ import annotations

import os
import re
import shutil
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from secrets import token_hex

from .bootstrap import BootstrapService
from .config import load_deploy_settings, project_python
from .contracts import (
    OperationState,
    RemoteIdentityEvidence,
    ResultCode,
    ToolingResult,
    UpdateDetails,
    UpdateEvidence,
)
from .coordination import RepositoryCoordinator, observe_tcp_port
from .errors import ToolingError
from .filesystem import (
    JournalStore,
    ScopedPath,
    bounded_read_text,
    is_unsafe_path,
    path_has_link,
    path_identity,
)
from .git import GitClient, canonical_remote_identity
from .postgres import BackupOutcome, PostgreSqlBackupService
from .process import ProcessSpec, StructuredProcessRunner, safe_environment
from .repository import RepositoryResolver

_SAFE_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
@dataclass(frozen=True)
class _Candidate:
    transaction_root: Path
    source_root: Path
    environment: Path
    previous: Path
    backup: Path


class UpdateService:
    """Update не использует reset, clean, pull, rebase или force push."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        git_factory: type[GitClient] = GitClient,
        bootstrap: BootstrapService | None = None,
        runner: StructuredProcessRunner | None = None,
        backup_service: PostgreSqlBackupService | None = None,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.git_factory = git_factory
        self.runner = runner or StructuredProcessRunner()
        self.bootstrap = bootstrap or BootstrapService(self.runner)
        self.backup_service = backup_service or PostgreSqlBackupService()

    def _assert_stopped(self, root: Path) -> None:
        coordinator = RepositoryCoordinator.for_root(root)
        record = coordinator.read_lifecycle()
        if record is not None:
            if coordinator.identity_from_record(record).matches():
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Update требует остановленного WebUI.",
                )
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Старое состояние lifecycle не подтверждается текущим процессом; Update остановлен.",
            )
        settings = load_deploy_settings(root)
        port = observe_tcp_port(settings.webui_port)
        if port.inspection_failed or port.listener_present is None:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Нельзя подтвердить свободный порт WebUI.",
            )
        if port.listener_present or port.pids:
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Update не выполняется при занятом порте WebUI.",
            )

    @staticmethod
    def _check_remote_url(url: str) -> str:
        return canonical_remote_identity(url)

    @staticmethod
    def _validate_git_names(branch: str, remote: str, target_branch: str) -> None:
        if not _SAFE_REMOTE_NAME_RE.fullmatch(remote):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Имя Git remote не соответствует безопасному формату.",
            )
        for value, label in ((branch, "ветка"), (target_branch, "ветка remote")):
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

    @staticmethod
    def _tree_safe(path: Path) -> bool:
        if is_unsafe_path(path) or path_has_link(path.parent):
            return False
        try:
            for item in path.rglob("*"):
                if item.is_symlink():
                    # Обычный POSIX venv может содержать ссылку bin/python.
                    # Разрешаем только ссылки на обычные файлы и копируем их содержимое.
                    if os.name == "nt":
                        return False
                    try:
                        target = item.resolve(strict=True)
                    except (OSError, RuntimeError):
                        return False
                    if item.is_dir() or not target.is_file():
                        return False
                    continue
                if is_unsafe_path(item):
                    return False
            return True
        except OSError:
            return False

    @classmethod
    def _copy_tree(cls, source: Path, destination: Path) -> None:
        if not source.is_dir() or not cls._tree_safe(source):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Окружение содержит symlink/reparse point; транзакция остановлена.",
            )
        if os.path.lexists(str(destination)):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Целевой каталог транзакции уже существует.",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(source, destination, symlinks=False)
        except (OSError, shutil.Error) as exc:
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Не удалось создать полную копию окружения.",
            ) from exc
        if not cls._tree_safe(destination):
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Копия окружения не прошла проверку безопасности.",
            )

    @staticmethod
    def _write_owned_marker(path: Path, transaction_id: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        ScopedPath(path).atomic_write_text(
            ".azurpilot-update-owned",
            f"schema_version=1\ntransaction_id={transaction_id}\n",
        )

    @staticmethod
    def _is_owned_environment(path: Path, transaction_id: str) -> bool:
        marker = path / ".azurpilot-update-owned"
        if is_unsafe_path(path) or is_unsafe_path(marker) or not marker.is_file():
            return False
        try:
            return any(
                line.strip() == f"transaction_id={transaction_id}"
                for line in bounded_read_text(marker, max_bytes=4096).splitlines()
            )
        except ToolingError:
            return False

    @staticmethod
    def _extract_candidate(archive: Path, candidate: Path) -> None:
        candidate.mkdir(parents=True, exist_ok=True)
        allowed = {"pyproject.toml", "uv.lock"}
        try:
            with tarfile.open(archive, mode="r") as bundle:
                members = bundle.getmembers()
                if len(members) != len(allowed):
                    raise ToolingError(
                        ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                        "Архив зависимостей remote имеет неожиданный состав.",
                    )
                for member in members:
                    name = PurePosixPath(member.name)
                    if (
                        str(name) not in allowed
                        or member.issym()
                        or member.islnk()
                        or not member.isfile()
                    ):
                        raise ToolingError(
                            ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                            "Архив зависимостей remote содержит небезопасный объект.",
                        )
                    data = bundle.extractfile(member)
                    if data is None:
                        raise ToolingError(
                            ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                            "Архив зависимостей remote не читается.",
                        )
                    ScopedPath(candidate).atomic_write_bytes(str(name), data.read(16 * 1024 * 1024 + 1))
        except ToolingError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                "Не удалось безопасно распаковать архив зависимостей кандидата.",
            ) from exc
        if any(not (candidate / name).is_file() for name in allowed):
            raise ToolingError(
                ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                "Кандидат зависимостей remote не содержит pyproject.toml и uv.lock.",
            )

    def _build_candidate(
        self,
        root: Path,
        settings,
        git: GitClient,
        remote_ref: str,
        transaction_root: Path,
        transaction_id: str,
        timeout_seconds: float,
    ) -> _Candidate:
        candidate = transaction_root / "candidate"
        environment = transaction_root / "candidate-environment"
        previous = transaction_root / "venv-previous"
        backup = transaction_root / "venv-backup"
        archive = transaction_root / "dependencies.tar"
        transaction_root.mkdir(parents=True, exist_ok=True)
        git.archive_dependencies(remote_ref, archive)
        if is_unsafe_path(archive) or archive.stat().st_size > 16 * 1024 * 1024:
            raise ToolingError(
                ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                "Архив зависимостей имеет небезопасный размер или тип.",
            )
        self._extract_candidate(archive, candidate)
        try:
            tomllib.loads((candidate / "pyproject.toml").read_text(encoding="utf-8-sig"))
            tomllib.loads((candidate / "uv.lock").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                "Кандидат зависимостей содержит повреждённый TOML.",
            ) from exc
        uv, _ = self.bootstrap.resolve_uv(root)
        lock_check = self.runner.run(
            ProcessSpec(
                executable=uv,
                argv=("lock", "--check", "--project", str(candidate)),
                cwd=root,
                timeout_seconds=min(180.0, timeout_seconds),
                max_output_bytes=64 * 1024,
                env=safe_environment({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}),
                no_window=True,
            )
        )
        if not lock_check.ok:
            raise ToolingError(
                ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                "Файлы кандидата pyproject.toml и uv.lock не прошли проверку.",
            )
        current_python = project_python(root, settings)
        if not current_python.is_file():
            raise ToolingError(
                ResultCode.TOOLING_REPAIR_REQUIRED,
                "Для синхронизации кандидата отсутствует текущий Python проекта; сначала выполните Repair.",
            )
        self.bootstrap.sync(
            root,
            uv,
            timeout_seconds,
            project_path=candidate,
            project_environment=environment,
            python_executable=current_python,
            state_root=transaction_root,
            install_project=False,
        )
        candidate_python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        verify = self.runner.run(
            ProcessSpec(
                executable=uv,
                argv=(
                    "sync",
                    "--project",
                    str(candidate),
                    "--python",
                    str(candidate_python),
                    "--frozen",
                    "--no-dev",
                    "--no-install-project",
                    "--dry-run",
                ),
                cwd=root,
                timeout_seconds=min(180.0, timeout_seconds),
                max_output_bytes=64 * 1024,
                env=safe_environment({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}),
                no_window=True,
            )
        )
        if not verify.ok:
            raise ToolingError(
                ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                "Среда кандидата не прошла повторную проверку frozen.",
            )
        self._write_owned_marker(environment, transaction_id)
        return _Candidate(transaction_root, candidate, environment, previous, backup)

    def _replace_environment(
        self,
        root: Path,
        candidate: _Candidate,
        transaction_id: str,
    ) -> None:
        venv = root / ".venv"
        if is_unsafe_path(venv) or not venv.is_dir() or not self._tree_safe(venv):
            raise ToolingError(
                ResultCode.TOOLING_REPAIR_REQUIRED,
                "Текущая .venv отсутствует или имеет небезопасный тип.",
            )
        self._copy_tree(venv, candidate.backup)
        if is_unsafe_path(candidate.environment) or not candidate.environment.is_dir():
            raise ToolingError(
                ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                "Среда кандидата отсутствует.",
            )
        if os.path.lexists(str(candidate.previous)):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Предыдущая среда кандидата уже существует.",
            )
        staging = root / f".venv.staging-{transaction_id}"
        move_started = False
        try:
            shutil.move(str(venv), str(candidate.previous))
            move_started = True
            self._copy_tree(candidate.environment, staging)
            self._write_owned_marker(staging, transaction_id)
            os.replace(staging, venv)
        except Exception as error:
            if not move_started:
                if (
                    venv.is_dir()
                    and self._tree_safe(venv)
                    and not os.path.lexists(str(candidate.previous))
                ):
                    raise ToolingError(
                        ResultCode.TOOLING_APPLY_FAILED_ROLLED_BACK,
                        "Замена .venv не началась; исходное окружение сохранено.",
                    ) from error
                raise ToolingError(
                    ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                    "Перемещение исходной `.venv` завершилось неоднозначно; откат остановлен.",
                ) from error
            if os.path.lexists(str(staging)):
                if is_unsafe_path(staging) or not self._tree_safe(staging):
                    raise ToolingError(
                        ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                        "Не удалось подтвердить временную среду кандидата; откат остановлен.",
                    ) from error
                shutil.rmtree(staging)
            try:
                if os.path.lexists(str(venv)):
                    if not self._is_owned_environment(venv, transaction_id) or not self._tree_safe(venv):
                        raise ToolingError(
                            ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                        "Текущая .venv не подтверждена как созданная транзакцией.",
                        )
                    shutil.rmtree(venv)
                if not candidate.previous.is_dir() or not self._tree_safe(candidate.previous):
                    raise ToolingError(
                        ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                        "Предыдущая .venv отсутствует или не прошла проверку.",
                    )
                shutil.move(str(candidate.previous), str(venv))
            except (OSError, ToolingError) as rollback_error:
                raise ToolingError(
                    ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                    "Замена `.venv` завершилась ошибкой, а откат не подтверждён.",
                ) from rollback_error
            raise ToolingError(
                ResultCode.TOOLING_APPLY_FAILED_ROLLED_BACK,
                "Замена .venv не завершена; исходное окружение восстановлено.",
            ) from error

    def _rollback_environment(self, root: Path, journal) -> None:
        transaction_id = journal.transaction_id
        venv = root / ".venv"
        if is_unsafe_path(venv):
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Текущая `.venv` имеет symlink/reparse point; откат остановлен.",
            )
        if venv.exists() and not self._is_owned_environment(venv, transaction_id):
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Текущая `.venv` не принадлежит транзакции; откат остановлен без записи.",
            )
        previous = Path(journal.previous_path) if journal.previous_path else None
        backup = Path(journal.backup_path) if journal.backup_path else None
        if venv.exists():
            shutil.rmtree(venv)
        if previous is not None and previous.exists():
            if is_unsafe_path(previous) or not self._tree_safe(previous):
                raise ToolingError(ResultCode.TOOLING_ROLLBACK_UNKNOWN, "Предыдущая .venv небезопасна.")
            shutil.move(str(previous), str(venv))
            return
        if backup is None or not backup.is_dir() or not self._tree_safe(backup):
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Для отката отсутствует проверенная резервная копия `.venv`.",
            )
        self._copy_tree(backup, venv)

    def _verify_replaced_environment(self, root: Path, settings, journal) -> None:
        venv = root / ".venv"
        if not self._is_owned_environment(venv, journal.transaction_id):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "После Update владение новой `.venv` не подтверждено.",
            )
        candidate = Path(journal.candidate_path or "")
        uv, _ = self.bootstrap.resolve_uv(root)
        python = project_python(venv.parent, settings)
        if not python.is_file():
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        result = self.runner.run(
            ProcessSpec(
                executable=uv,
                argv=(
                    "sync",
                    "--project",
                    str(candidate),
                    "--python",
                    str(python),
                    "--frozen",
                    "--no-dev",
                    "--no-install-project",
                    "--dry-run",
                ),
                cwd=root,
                timeout_seconds=180.0,
                max_output_bytes=64 * 1024,
                env=safe_environment({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}),
                no_window=True,
            )
        )
        if not result.ok:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Новая `.venv` не прошла постусловие frozen.",
            )

    def _recover_pending(
        self,
        root: Path,
        settings,
        coordinator: RepositoryCoordinator,
        git: GitClient,
    ) -> str | None:
        store = JournalStore(coordinator.layout, "update")
        journal = store.active()
        if journal is None:
            return None
        tx_root = coordinator.layout.transactions_directory / journal.transaction_id
        if is_unsafe_path(tx_root) or not tx_root.is_dir():
            raise ToolingError(
                ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
                "Корень транзакции не подтверждён; автоматическое восстановление запрещено.",
            )
        if journal.root_identity != path_identity(root):
            raise ToolingError(
                ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
                "Журнал Update относится к другому корню репозитория.",
            )
        current_head = git.head()
        if git.status_porcelain():
            raise ToolingError(
                ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
                "Рабочее дерево изменилось во время Update; автоматическое восстановление запрещено.",
            )
        if journal.pre_head is None or journal.target_head is None:
            raise ToolingError(
                ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
                "Журнал Update не содержит точные исходный и целевой HEAD.",
            )
        if current_head == journal.pre_head:
            try:
                previous = Path(journal.previous_path) if journal.previous_path else None
                if (
                    previous is not None
                    and previous.exists()
                ) or journal.phase in {
                    "environment_synchronized",
                    "merge_pending",
                    "merged",
                    "verification_failed",
                    "rollback_unknown",
                }:
                    self._rollback_environment(root, journal)
                updated = journal.model_copy(update={"phase": "rolled_back", "failure_reason": "recovered_before_merge"})
                store.save(updated)
                store.retain_terminal()
                return "rolled_back"
            except ToolingError as error:
                try:
                    store.save(journal.model_copy(update={"phase": "rollback_unknown", "failure_reason": "recovery_failed"}))
                except ToolingError:
                    pass
                raise ToolingError(
                    ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
                    "Восстановление Update не подтвердило старую `.venv`; запись остановлена без автоматического повтора.",
                ) from error
        if current_head == journal.target_head:
            try:
                if git.status_porcelain():
                    raise ToolingError(
                        ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
                        "Checkout находится на целевом HEAD, но рабочее дерево изменено.",
                    )
                self._verify_replaced_environment(root, settings, journal)
            except ToolingError as error:
                raise ToolingError(
                    ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
                    "Checkout уже на целевом HEAD, но среда кандидата не подтверждена; требуется восстановление только для чтения.",
                ) from error
            updated = journal.model_copy(update={"phase": "completed", "failure_reason": None})
            store.save(updated)
            store.retain_terminal()
            return "completed"
        raise ToolingError(
            ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
            "Журнал Update имеет неоднозначный HEAD; автоматическое восстановление запрещено.",
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
        if not 0 < timeout_seconds <= 24 * 60 * 60:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Срок должен быть положительным и ограниченным числом.",
            )
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        settings = load_deploy_settings(root)
        branch = expected_branch or settings.git_branch
        remote = remote_name or settings.git_remote
        target_branch = remote_branch or branch
        self._validate_git_names(branch, remote, target_branch)
        if not _SAFE_REMOTE_NAME_RE.fullmatch(settings.upstream_remote):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Имя upstream remote не соответствует безопасному формату.",
            )
        operation_id = f"update-{token_hex(8)}"
        coordinator = RepositoryCoordinator.for_root(root)
        lock = coordinator.lock("update")
        if not lock.acquire(timeout_seconds=0):
            raise ToolingError(ResultCode.TOOLING_OPERATION_CONFLICT, "Update уже выполняется.", operation_id=operation_id)
        transaction = None
        backup: BackupOutcome | None = None
        journal_store = JournalStore(coordinator.layout, "update")
        try:
            self._assert_stopped(root)
            git = self.git_factory(root, self.runner)
            self._recover_pending(root, settings, coordinator, git)
            if JournalStore(coordinator.layout, "repair").active() or JournalStore(coordinator.layout, "build").active() or journal_store.active():
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "Незавершённая транзакция блокирует Update; сначала завершите восстановление.",
                    operation_id=operation_id,
                )
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
                    "Upstream не совпадает с выбранной веткой remote.",
                    operation_id=operation_id,
                )
            upstream_remote = settings.upstream_remote
            if not git.remote_exists(remote) or not git.remote_exists(upstream_remote):
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Настроенные Git remote отсутствуют или не подтверждены.",
                    operation_id=operation_id,
                )
            remote_url = git.remote_url(remote)
            actual_identity = self._check_remote_url(remote_url)
            configured_origin_url = expected_origin_url or settings.repository_url
            if not configured_origin_url:
                raise ToolingError(
                    ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                    "Идентичность канонического репозитория не настроена; изменения Update запрещены.",
                    operation_id=operation_id,
                )
            configured_identity = self._check_remote_url(configured_origin_url)
            if actual_identity != configured_identity:
                raise ToolingError(
                    ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                    "Идентичность Git remote не совпадает с настроенным каноническим репозиторием.",
                    operation_id=operation_id,
                )
            push_url = git.remote_push_url(upstream_remote)
            if push_url != settings.upstream_push_url:
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Политика отправки upstream не соответствует безопасному контракту DISABLED.",
                    operation_id=operation_id,
                )
            if git.active_operation():
                raise ToolingError(ResultCode.TOOLING_OPERATION_CONFLICT, "В checkout уже выполняется операция Git.", operation_id=operation_id)
            if git.status_porcelain():
                raise ToolingError(ResultCode.TOOLING_UPDATE_DIRTY, "Update требует чистого рабочего дерева.", operation_id=operation_id)
            pre_head = git.head()
            git.fetch_branch(remote, target_branch)
            remote_head = git.remote_head(remote, target_branch)
            identity_evidence = RemoteIdentityEvidence(
                configured=configured_identity,
                actual=actual_identity,
                equivalent=True,
                tracking=upstream,
                upstream_push_policy=push_url or "missing",
            )
            if pre_head == remote_head:
                return ToolingResult[UpdateDetails, UpdateEvidence](
                    ok=True,
                    code=ResultCode.OK,
                    state=OperationState.READY,
                    message="Checkout уже находится на актуальном HEAD remote.",
                    operation_id=operation_id,
                    details=UpdateDetails(
                        status=OperationState.READY,
                        branch=branch,
                        remote=remote,
                        dependency_changed=False,
                        fast_forwarded=False,
                    ),
                    evidence=UpdateEvidence(
                        repository=resolved.evidence,
                        pre_head=pre_head,
                        post_head=pre_head,
                        remote_head=remote_head,
                        remote_identity=identity_evidence,
                    ),
                )
            if git.is_ancestor(remote_head, pre_head):
                raise ToolingError(ResultCode.TOOLING_UPDATE_LOCAL_AHEAD, "Локальная ветка опережает remote; автоматическое переписывание запрещено.", operation_id=operation_id)
            if not git.is_ancestor(pre_head, remote_head):
                raise ToolingError(ResultCode.TOOLING_UPDATE_DIVERGED, "Локальная ветка разошлась с remote; требуется явное решение.", operation_id=operation_id)

            # Резервная копия создаётся до изменения кандидата, Git и окружения.
            try:
                backup = self.backup_service.create(
                    root,
                    coordinator.layout,
                    settings,
                    pre_head=pre_head,
                    operation_id=operation_id,
                )
            except ToolingError as error:
                raise ToolingError(error.code, error.message, operation_id=operation_id) from error
            dependency_changed = git.dependency_changed(pre_head, remote_head)
            if dependency_changed:
                transaction = journal_store.create(pre_head=pre_head, target_head=remote_head)
                transaction = transaction.model_copy(
                    update={"remote_name": remote, "remote_branch": target_branch, "backup_id": backup.evidence.backup_id}
                )
                journal_store.save(transaction)
                tx_root = coordinator.layout.transactions_directory / transaction.transaction_id
                environment_replaced = False
                try:
                    candidate = self._build_candidate(
                        root,
                        settings,
                        git,
                        f"refs/remotes/{remote}/{target_branch}",
                        tx_root,
                        transaction.transaction_id,
                        timeout_seconds,
                    )
                    transaction = transaction.model_copy(
                        update={
                            "phase": "candidate_validated",
                            "candidate_path": str(candidate.source_root),
                            "venv_path": str(root / ".venv"),
                            "backup_path": str(candidate.backup),
                            "previous_path": str(candidate.previous),
                            "backup_present": True,
                            "ownership_confirmed": True,
                        }
                    )
                    journal_store.save(transaction)
                    self._replace_environment(root, candidate, transaction.transaction_id)
                    environment_replaced = True
                    transaction = transaction.model_copy(update={"phase": "environment_synchronized"})
                    journal_store.save(transaction)
                except ToolingError as error:
                    if transaction is not None:
                        phase = (
                            "rollback_unknown"
                            if error.code is ResultCode.TOOLING_ROLLBACK_UNKNOWN
                            else "rolled_back"
                        )
                        if environment_replaced:
                            try:
                                self._rollback_environment(root, transaction)
                                phase = "rolled_back"
                            except ToolingError as rollback_error:
                                phase = "rollback_unknown"
                                error = ToolingError(
                                    ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                                    "После ошибки Update откат окружения не подтверждён.",
                                    operation_id=operation_id,
                                )
                                error.__cause__ = rollback_error
                        transaction = transaction.model_copy(
                            update={"phase": phase, "failure_reason": error.code.value}
                        )
                        try:
                            journal_store.save(transaction)
                            if phase == "rolled_back":
                                journal_store.retain_terminal()
                        except ToolingError as journal_error:
                            raise ToolingError(
                                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                                "Не удалось записать итог транзакции Update; восстановление остановлено.",
                                operation_id=operation_id,
                            ) from journal_error
                    raise
                transaction = transaction.model_copy(update={"phase": "merge_pending"})
                journal_store.save(transaction)
            try:
                git.merge_ff_only(f"refs/remotes/{remote}/{target_branch}")
            except ToolingError:
                if transaction is not None:
                    current = git.head()
                    if current == pre_head:
                        try:
                            self._rollback_environment(root, transaction)
                            transaction = transaction.model_copy(update={"phase": "rolled_back", "failure_reason": "merge_failed"})
                            journal_store.save(transaction)
                            journal_store.retain_terminal()
                        except ToolingError as error:
                            journal_store.save(transaction.model_copy(update={"phase": "rollback_unknown", "failure_reason": "merge_rollback_failed"}))
                            raise ToolingError(ResultCode.TOOLING_ROLLBACK_UNKNOWN, "Слияние Git не выполнено, а откат окружения не подтверждён.", operation_id=operation_id) from error
                raise
            post_head = git.head()
            if post_head != remote_head or git.status_porcelain():
                if transaction is not None:
                    journal_store.save(transaction.model_copy(update={"phase": "verification_failed", "failure_reason": "head_or_tree_postcondition"}))
                raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, "После fast-forward не подтверждены точный HEAD или чистое дерево.", operation_id=operation_id)
            if transaction is not None:
                self._verify_replaced_environment(root, settings, transaction)
                transaction = transaction.model_copy(update={"phase": "merged"})
                journal_store.save(transaction)
                transaction = transaction.model_copy(update={"phase": "completed"})
                journal_store.save(transaction)
                journal_store.retain_terminal()
            return ToolingResult[UpdateDetails, UpdateEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Update выполнен fast-forward; резервная копия PostgreSQL и постусловие окружения подтверждены.",
                operation_id=operation_id,
                details=UpdateDetails(
                    status=OperationState.READY,
                    branch=branch,
                    remote=remote,
                    dependency_changed=dependency_changed,
                    fast_forwarded=True,
                    backup_required=True,
                    backup_validated=backup.evidence.validated,
                    transaction_phase="completed" if transaction else None,
                ),
                evidence=UpdateEvidence(
                    repository=resolved.evidence,
                    pre_head=pre_head,
                    post_head=post_head,
                    remote_head=remote_head,
                    transaction_id=transaction.transaction_id if transaction else None,
                    remote_identity=identity_evidence,
                    postgres_backup=backup.evidence,
                ),
            )
        except ToolingError as error:
            if error.operation_id is None:
                raise ToolingError(error.code, error.message, state=error.state, details=error.details, evidence=error.evidence, operation_id=operation_id) from error
            raise
        finally:
            lock.release()


__all__ = ["UpdateService"]
