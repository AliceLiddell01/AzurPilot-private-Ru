"""Сервис CLI для управления автономным владельцем Bot Runtime."""

from __future__ import annotations

import time
import uuid
import os
from pathlib import Path

from module.application.runtime_control import (
    BotRuntimeBootstrapper,
    RuntimeControlClient,
    RuntimeControlError,
    RuntimeControlOperation,
    RuntimeOwnerIdentity,
)
from module.application.runtime_state import RuntimeStateError, RuntimeStateStore
from module.application.runtime_worker_registry import (
    LEGACY_WORKER_REGISTRY_FILES,
    get_canonical_owner_record_read_only,
    get_canonical_workers_read_only,
    process_matches,
)

from .contracts import (
    BotRuntimeDetails,
    BotRuntimeWorker,
    RepositoryRootEvidence,
    ToolingResult,
)
from .errors import RepositoryResolutionError, ToolingError
from .repository import RepositoryResolver
from .result import OperationState, ResultCode


class BotRuntimeService:
    """Запускает, проверяет и останавливает Bot Runtime без зависимости от WebUI."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        *,
        poll_interval: float = 0.1,
    ) -> None:
        if not 0 < poll_interval <= 1:
            raise ValueError("poll_interval должен быть в диапазоне (0, 1]")
        self.resolver = resolver or RepositoryResolver()
        self.poll_interval = poll_interval

    def status(
        self, repository_root: str | Path | None = None
    ) -> ToolingResult[BotRuntimeDetails, RepositoryRootEvidence]:
        resolved = self._resolve(repository_root)
        details = self._snapshot(resolved.path)
        return self._result(
            resolved.evidence,
            details,
            message=self._status_message(details),
        )

    def start(
        self,
        repository_root: str | Path | None = None,
        *,
        timeout_seconds: float = 30,
    ) -> ToolingResult[BotRuntimeDetails, RepositoryRootEvidence]:
        timeout = self._validate_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout
        resolved = self._resolve(repository_root)
        root = resolved.path
        bootstrap_timeout = deadline - time.monotonic()
        if bootstrap_timeout <= 0:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT,
                "Срок запуска Bot Runtime истёк до bootstrap.",
                state=OperationState.UNKNOWN,
            )
        try:
            BotRuntimeBootstrapper(
                root,
                owner_reader=lambda: get_canonical_owner_record_read_only(
                    repository_root=root
                ),
                owner_matches=self._owner_matches,
                timeout=bootstrap_timeout,
            ).ensure()
        except RuntimeControlError as exc:
            code = (
                ResultCode.TOOLING_TIMEOUT
                if "TIMEOUT" in exc.code
                else ResultCode.TOOLING_PRECONDITION_FAILED
            )
            raise ToolingError(code, str(exc)) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                f"Bot Runtime нельзя безопасно запустить: {type(exc).__name__}",
            ) from exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT,
                "Срок запуска Bot Runtime истёк до запроса настроенных профилей.",
                state=OperationState.UNKNOWN,
            )
        client = RuntimeControlClient(
            root,
            owner_reader=lambda: get_canonical_owner_record_read_only(
                repository_root=root
            ),
            owner_matches=self._owner_matches,
            timeout=remaining,
        )
        try:
            start_result = client.call(
                RuntimeControlOperation.START_CONFIGURED_PROFILES,
                "runtime",
                idempotency_key=f"cli-start-{uuid.uuid4()}",
            )
        except RuntimeControlError as exc:
            code = (
                ResultCode.TOOLING_TIMEOUT
                if "TIMEOUT" in exc.code
                else ResultCode.TOOLING_VERIFICATION_UNKNOWN
            )
            raise ToolingError(code, str(exc)) from exc
        if not start_result.ok:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                f"Bot Runtime не принял запрос запуска настроенных профилей: {start_result.message}",
                state=OperationState.UNKNOWN,
            )
        if time.monotonic() >= deadline:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT,
                "Срок запуска Bot Runtime истёк после принятия запроса профилей.",
                state=OperationState.IN_FLIGHT,
            )

        details = self._snapshot(root)
        if time.monotonic() >= deadline:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT,
                "Срок запуска Bot Runtime истёк при проверке владельца.",
                state=OperationState.IN_FLIGHT,
                details=details,
            )
        if not details.owner_running:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Bot Runtime не подтвердил владельца после bootstrap.",
                state=OperationState.UNKNOWN,
            )
        return self._result(
            resolved.evidence,
            details,
            message="Bot Runtime запущен и подтвердил запрос настроенных профилей; WebUI не запускался.",
        )

    def stop(
        self,
        repository_root: str | Path | None = None,
        *,
        timeout_seconds: float = 120,
    ) -> ToolingResult[BotRuntimeDetails, RepositoryRootEvidence]:
        timeout = self._validate_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout
        resolved = self._resolve(repository_root)
        root = resolved.path
        before = self._snapshot(root)
        if not before.owner_running:
            if before.status is OperationState.STOPPED and not before.recovery_required:
                return self._result(
                    resolved.evidence,
                    before,
                    message="Bot Runtime уже остановлен.",
                )
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние Bot Runtime не допускает безопасную остановку.",
                state=OperationState.UNKNOWN,
                details=before,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT,
                "Срок остановки Bot Runtime истёк до отправки control request.",
                state=OperationState.UNKNOWN,
            )
        client = RuntimeControlClient(
            root,
            owner_reader=lambda: get_canonical_owner_record_read_only(
                repository_root=root
            ),
            owner_matches=self._owner_matches,
            timeout=remaining,
        )
        try:
            result = client.call(
                RuntimeControlOperation.STOP_RUNTIME,
                "runtime",
                idempotency_key=f"cli-stop-{uuid.uuid4()}",
            )
        except RuntimeControlError as exc:
            code = (
                ResultCode.TOOLING_TIMEOUT
                if "TIMEOUT" in exc.code
                else ResultCode.TOOLING_VERIFICATION_UNKNOWN
            )
            raise ToolingError(code, str(exc)) from exc

        if not result.ok:
            raise ToolingError(
                ResultCode.TOOLING_CLEANUP_UNKNOWN,
                f"Bot Runtime не подтвердил штатную остановку: {result.message}",
                state=OperationState.IN_FLIGHT,
            )

        if result.owner is None:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Bot Runtime подтвердил Stop без проверяемой identity владельца.",
                state=OperationState.UNKNOWN,
            )
        owner = result.owner
        while True:
            if deadline - time.monotonic() <= 0:
                raise ToolingError(
                    ResultCode.TOOLING_CLEANUP_UNKNOWN,
                    "После Stop не подтверждено завершение Bot Runtime и всех workers.",
                    state=OperationState.IN_FLIGHT,
                )
            try:
                current_owner = get_canonical_owner_record_read_only(
                    repository_root=root
                )
                workers = get_canonical_workers_read_only(repository_root=root)
                old_owner_running = self._owner_matches(owner)
                live_workers = [
                    name
                    for name, record in workers.items()
                    if self._process_matches(record) is True
                ]
            except (RuntimeError, OSError) as exc:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Не удалось проверить Bot Runtime после Stop.",
                    state=OperationState.UNKNOWN,
                ) from exc
            if current_owner is not None:
                try:
                    observed = RuntimeOwnerIdentity.from_value(current_owner)
                except RuntimeControlError as exc:
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "Registry содержит недействительную identity после Stop.",
                        state=OperationState.UNKNOWN,
                    ) from exc
                try:
                    observed_running = self._owner_matches(observed)
                except (RuntimeError, OSError) as exc:
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "Не удалось проверить identity нового Bot Runtime owner после Stop.",
                        state=OperationState.UNKNOWN,
                    ) from exc
                if observed != owner and observed_running is True:
                    raise ToolingError(
                        ResultCode.TOOLING_OPERATION_CONFLICT,
                        "После Stop зарегистрирован другой Bot Runtime owner.",
                        state=OperationState.CONFLICT,
                    )
            if old_owner_running is not True and not live_workers:
                after = self._snapshot(root)
                if after.status is OperationState.STOPPED and time.monotonic() <= deadline:
                    return self._result(
                        resolved.evidence,
                        after,
                        message="Bot Runtime и принадлежащие ему workers остановлены.",
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolingError(
                    ResultCode.TOOLING_CLEANUP_UNKNOWN,
                    "После Stop не подтверждено завершение Bot Runtime и всех workers.",
                    state=OperationState.IN_FLIGHT,
                )
            time.sleep(min(self.poll_interval, remaining))

    def _resolve(self, repository_root: str | Path | None):
        try:
            return self.resolver.resolve(repository_root)
        except RepositoryResolutionError as exc:
            raise ToolingError(exc.code, str(exc)) from exc

    @staticmethod
    def _validate_timeout(timeout_seconds: float) -> float:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 120
        ):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Срок Bot Runtime должен быть в диапазоне (0, 120] секунд.",
            )
        return float(timeout_seconds)

    @staticmethod
    def _owner_matches(owner: RuntimeOwnerIdentity) -> bool | None:
        return process_matches(owner.as_dict())

    @staticmethod
    def _process_matches(identity: dict[str, object]) -> bool | None:
        return process_matches(identity)

    @classmethod
    def _snapshot(cls, root: Path) -> BotRuntimeDetails:
        try:
            state_store = RuntimeStateStore(root)
        except (RuntimeStateError, RuntimeControlError, OSError, RuntimeError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                f"Состояние Bot Runtime нельзя безопасно прочитать: {type(exc).__name__}",
                state=OperationState.UNKNOWN,
            ) from exc
        legacy_paths = tuple(root / path for path in LEGACY_WORKER_REGISTRY_FILES) + (
            state_store.legacy_path,
        )
        if any(
            os.path.lexists(path)
            or path.is_symlink()
            or (hasattr(path, "is_junction") and path.is_junction())
            for path in legacy_paths
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Обнаружено legacy WebUI runtime state; безопасный статус доступен после миграции Bot Runtime.",
                state=OperationState.UNKNOWN,
            )
        try:
            owner_record = get_canonical_owner_record_read_only(
                repository_root=root
            )
            workers = get_canonical_workers_read_only(repository_root=root)
            snapshots = state_store.read_all()
        except (RuntimeStateError, RuntimeControlError, OSError, RuntimeError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                f"Состояние Bot Runtime нельзя безопасно прочитать: {type(exc).__name__}",
                state=OperationState.UNKNOWN,
            ) from exc

        owner: RuntimeOwnerIdentity | None = None
        owner_running = False
        recovery_required = False
        if owner_record is not None:
            try:
                owner = RuntimeOwnerIdentity.from_value(owner_record)
            except RuntimeControlError as exc:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Registry содержит недействительную identity Bot Runtime owner.",
                    state=OperationState.UNKNOWN,
                ) from exc
            try:
                owner_matches = cls._owner_matches(owner)
            except (OSError, RuntimeError) as exc:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Нельзя подтвердить identity Bot Runtime owner.",
                    state=OperationState.UNKNOWN,
                ) from exc
            owner_running = owner_matches is True
            recovery_required = not owner_running

        worker_items: list[BotRuntimeWorker] = []
        active_workers: set[str] = set()
        for profile, record in workers.items():
            try:
                matches = cls._process_matches(record)
            except (OSError, RuntimeError) as exc:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    f"Нельзя подтвердить identity worker профиля {profile}.",
                    state=OperationState.UNKNOWN,
                ) from exc
            if matches is True:
                active_workers.add(profile)
            else:
                recovery_required = True
            worker_items.append(
                BotRuntimeWorker(
                    profile=profile,
                    pid=record["pid"],
                    created_at=record["created_at"],
                    running=matches is True,
                )
            )

        for profile, snapshot in snapshots.items():
            if snapshot.worker_running is not True:
                continue
            if profile in workers:
                record = workers[profile]
                if (
                    snapshot.worker_pid != record["pid"]
                    or snapshot.worker_created_at != record["created_at"]
                ):
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        f"Runtime state и registry расходятся для worker {profile}.",
                        state=OperationState.UNKNOWN,
                    )
                continue
            if snapshot.worker_pid is None or snapshot.worker_created_at is None:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    f"Runtime state worker {profile} не содержит полной identity.",
                    state=OperationState.UNKNOWN,
                )
            try:
                matches = cls._process_matches(
                    {
                        "pid": snapshot.worker_pid,
                        "created_at": snapshot.worker_created_at,
                    }
                )
            except (OSError, RuntimeError) as exc:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    f"Нельзя проверить orphan worker из runtime state: {profile}.",
                    state=OperationState.UNKNOWN,
                ) from exc
            if matches is True:
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    f"Runtime state указывает на незарегистрированный живой worker {profile}.",
                    state=OperationState.CONFLICT,
                )
            recovery_required = True

        if active_workers and not owner_running:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Живой worker не принадлежит подтверждённому Bot Runtime owner.",
                state=OperationState.CONFLICT,
            )
        status = (
            OperationState.READY
            if owner_running
            else OperationState.STOPPED
        )
        return BotRuntimeDetails(
            status=status,
            owner_pid=owner.pid if owner is not None else None,
            owner_created_at=owner.created_at if owner is not None else None,
            owner_running=owner_running,
            workers=tuple(worker_items),
            recovery_required=recovery_required,
        )

    @staticmethod
    def _status_message(details: BotRuntimeDetails) -> str:
        if details.owner_running:
            return "Bot Runtime работает; WebUI lifecycle не влияет на его owner."
        if details.recovery_required:
            return "Bot Runtime остановлен; перед запуском требуется безопасное восстановление stale state."
        return "Bot Runtime остановлен."

    @staticmethod
    def _result(
        evidence: RepositoryRootEvidence,
        details: BotRuntimeDetails,
        *,
        message: str,
    ) -> ToolingResult[BotRuntimeDetails, RepositoryRootEvidence]:
        return ToolingResult[BotRuntimeDetails, RepositoryRootEvidence](
            ok=True,
            code=ResultCode.OK,
            state=details.status,
            message=message,
            details=details,
            evidence=evidence,
        )


__all__ = ["BotRuntimeService"]
