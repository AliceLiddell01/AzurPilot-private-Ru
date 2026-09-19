"""Registry и CLI-facing service для шести прямых интеграций."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from azurpilot.tooling.contracts import (
    AnalysisScope,
    OperationState,
    ResultCode,
    ToolingResult,
)
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.repository import RepositoryResolver

from .adapters import (
    AdapterOutcome,
    Context7Adapter,
    DockerDocsAdapter,
    DockerHubAdapter,
    GrafanaAdapter,
    IntegrationAdapter,
    SemgrepAdapter,
)
from .coderabbit import CodeRabbitAdapter, CodeRabbitProgress
from .config import IntegrationConfig, load_integration_config
from .contracts import (
    CodeRabbitCycleSummary,
    IntegrationDetails,
    IntegrationEvidenceBundle,
    IntegrationFinding,
    IntegrationName,
    IntegrationRecord,
    IntegrationState,
)

ADAPTER_ORDER = (
    IntegrationName.CODERABBIT,
    IntegrationName.SEMGREP,
    IntegrationName.GRAFANA,
    IntegrationName.CONTEXT7,
    IntegrationName.DOCKER_DOCS,
    IntegrationName.DOCKER_HUB,
)

_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _target_name(value: str | IntegrationName) -> IntegrationName:
    if isinstance(value, IntegrationName):
        return value
    normalized = value.casefold().replace("_", "-")
    if normalized == "dockerhub":
        normalized = "docker-hub"
    try:
        return IntegrationName(normalized)
    except ValueError as exc:
        raise ToolingError(
            ResultCode.TOOLING_INVALID_INVOCATION,
            "Неизвестное семейство внешней интеграции.",
        ) from exc


def _error_record(name: IntegrationName, code: str, state: IntegrationState) -> IntegrationRecord:
    from .contracts import CredentialRef, IntegrationEvidence

    return IntegrationRecord(
        name=name,
        state=state,
        reason_code=code,
        message="Состояние внешней интеграции не удалось подтвердить.",
        evidence=IntegrationEvidence(
            route="direct",
            configured=False,
            reachable=False,
            read_only=True,
            credential=CredentialRef(),
        ),
    )


def _unexpected_reason_code(error: BaseException) -> str:
    candidate = type(error).__name__.upper()[:120]
    if _REASON_CODE_RE.fullmatch(candidate):
        return candidate
    return "INTEGRATION_UNEXPECTED_ERROR"


class IntegrationRegistry:
    """Закрытый каталог типизированных adapters без произвольного dispatcher."""

    def __init__(self, adapters: Iterable[IntegrationAdapter] | None = None) -> None:
        selected = tuple(adapters or (
            CodeRabbitAdapter(),
            SemgrepAdapter(),
            GrafanaAdapter(),
            Context7Adapter(),
            DockerDocsAdapter(),
            DockerHubAdapter(),
        ))
        by_name = {adapter.name: adapter for adapter in selected}
        if tuple(by_name) != tuple(adapter.name for adapter in selected):
            raise ValueError("IntegrationRegistry содержит duplicate adapter")
        if set(by_name) != set(ADAPTER_ORDER):
            raise ValueError("IntegrationRegistry должен содержать ровно шесть adapters")
        self._adapters = by_name

    @property
    def adapters(self) -> tuple[IntegrationAdapter, ...]:
        return tuple(self._adapters[name] for name in ADAPTER_ORDER)

    def adapter(self, name: str | IntegrationName) -> IntegrationAdapter:
        target = _target_name(name)
        return self._adapters[target]

    def status_records(self, root: Path, config: IntegrationConfig) -> tuple[IntegrationRecord, ...]:
        records: list[IntegrationRecord] = []
        for adapter in self.adapters:
            try:
                records.append(adapter.status(root, config))
            except ToolingError as error:
                records.append(
                    _error_record(
                        adapter.name,
                        error.code.value,
                        IntegrationState.UNKNOWN,
                    )
                )
            except Exception as error:  # noqa: BLE001 - bounded status boundary.
                records.append(
                    _error_record(adapter.name, _unexpected_reason_code(error), IntegrationState.UNKNOWN)
                )
        return tuple(records)

    async def probe_records(
        self,
        root: Path,
        config: IntegrationConfig,
        *,
        names: Iterable[IntegrationName] | None = None,
    ) -> tuple[AdapterOutcome, ...]:
        selected = tuple(names or ADAPTER_ORDER)

        async def probe_one(name: IntegrationName) -> AdapterOutcome:
            adapter = self._adapters[name]
            try:
                return await adapter.probe(root, config)
            except ToolingError as error:
                return AdapterOutcome(
                    _error_record(name, error.code.value, IntegrationState.UNKNOWN)
                )
            except Exception as error:  # noqa: BLE001 - bounded probe boundary.
                return AdapterOutcome(
                    _error_record(
                        name,
                        _unexpected_reason_code(error),
                        IntegrationState.UNKNOWN,
                    )
                )

        return tuple(await asyncio.gather(*(probe_one(name) for name in selected)))


class IntegrationService:
    """Read-only status/doctor и явно разрешённые typed leaves CLI."""

    def __init__(
        self,
        registry: IntegrationRegistry | None = None,
        resolver: RepositoryResolver | None = None,
    ) -> None:
        self.registry = registry or IntegrationRegistry()
        self.resolver = resolver or RepositoryResolver()

    def resolve_root(self, repository_root: str | Path | None = None) -> Path:
        """Разрешить и валидировать repository root через общий resolver."""

        return self.resolver.resolve(repository_root).path

    @staticmethod
    def _state(records: tuple[IntegrationRecord, ...]) -> IntegrationState:
        states = {record.state for record in records}
        if states == {IntegrationState.READY}:
            return IntegrationState.READY
        for state in (
            IntegrationState.INCOMPATIBLE,
            IntegrationState.RATE_LIMITED,
            IntegrationState.UNAUTHENTICATED,
            IntegrationState.UNAVAILABLE,
            IntegrationState.NOT_CONFIGURED,
            IntegrationState.DEGRADED,
            IntegrationState.UNKNOWN,
        ):
            if state in states:
                return state
        return IntegrationState.UNKNOWN

    @staticmethod
    def _result_code(records: tuple[IntegrationRecord, ...]) -> ResultCode:
        state = IntegrationService._state(records)
        return {
            IntegrationState.READY: ResultCode.OK,
            IntegrationState.RATE_LIMITED: ResultCode.TOOLING_PROVIDER_UNAVAILABLE,
            IntegrationState.INCOMPATIBLE: ResultCode.TOOLING_PRECONDITION_FAILED,
            IntegrationState.UNKNOWN: ResultCode.TOOLING_VERIFICATION_UNKNOWN,
        }.get(state, ResultCode.TOOLING_CAPABILITY_UNAVAILABLE)

    @staticmethod
    def _message(action: str, records: tuple[IntegrationRecord, ...]) -> str:
        state = IntegrationService._state(records)
        if state is IntegrationState.READY:
            return f"Проверка прямых внешних интеграций ({action}) пройдена."
        not_ready = sum(record.state is not IntegrationState.READY for record in records)
        return f"{action.capitalize()} завершён; неподтверждённых интеграций: {not_ready}."

    def _result(
        self,
        action: str,
        records: tuple[IntegrationRecord, ...],
        *,
        target: IntegrationName | None = None,
        scope: AnalysisScope | None = None,
        findings: tuple[IntegrationFinding, ...] = (),
        coderabbit_cycle: CodeRabbitCycleSummary | None = None,
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        aggregate = self._state(records)
        return ToolingResult[IntegrationDetails, IntegrationEvidenceBundle](
            ok=aggregate is IntegrationState.READY,
            code=self._result_code(records),
            state=OperationState.READY
            if aggregate is IntegrationState.READY
            else OperationState.DIAGNOSTIC,
            message=self._message(action, records),
            details=IntegrationDetails(
                action=action,
                integrations=records,
                target=target,
                scope=scope,
                findings=findings,
                coderabbit_cycle=coderabbit_cycle,
            ),
            evidence=IntegrationEvidenceBundle(generated_at=_now()),
        )

    def status(self, repository_root: str | Path | None = None) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        records = self.registry.status_records(root, config)
        return self._result("status", records)

    def status_one(
        self,
        name: str | IntegrationName,
        repository_root: str | Path | None = None,
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        target = _target_name(name)
        adapter = self.registry.adapter(target)
        try:
            record = adapter.status(root, config)
        except ToolingError as error:
            record = _error_record(target, error.code.value, IntegrationState.UNKNOWN)
        except Exception as error:  # noqa: BLE001 - bounded status boundary.
            record = _error_record(
                target,
                _unexpected_reason_code(error),
                IntegrationState.UNKNOWN,
            )
        cycle_summary = None
        if isinstance(adapter, CodeRabbitAdapter):
            try:
                cycle_summary = adapter.cycle_summary(root)
            except ToolingError:
                cycle_summary = None
        return self._result(
            "status", (record,), target=target, coderabbit_cycle=cycle_summary
        )

    async def doctor_async(
        self, repository_root: str | Path | None = None
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        outcomes = await self.registry.probe_records(root, config)
        return self._result("doctor", tuple(item.record for item in outcomes), findings=tuple(
            finding for item in outcomes for finding in item.findings
        ))

    def doctor(self, repository_root: str | Path | None = None) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        return asyncio.run(self.doctor_async(repository_root))

    async def probe_async(
        self,
        name: str | IntegrationName,
        repository_root: str | Path | None = None,
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        target = _target_name(name)
        outcomes = await self.registry.probe_records(root, config, names=(target,))
        return self._result(
            "probe",
            tuple(item.record for item in outcomes),
            target=target,
            findings=tuple(
                finding for outcome in outcomes for finding in outcome.findings
            ),
        )

    def probe(
        self,
        name: str | IntegrationName,
        repository_root: str | Path | None = None,
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        return asyncio.run(self.probe_async(name, repository_root))

    def scan(
        self,
        scope: AnalysisScope,
        repository_root: str | Path | None = None,
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        adapter = self.registry.adapter(IntegrationName.SEMGREP)
        if not isinstance(adapter, SemgrepAdapter):
            raise ToolingError(ResultCode.TOOLING_PRECONDITION_FAILED, "Semgrep adapter имеет неверный тип.")
        outcome = adapter.scan(root, config, scope)
        return self._result(
            "scan",
            (outcome.record,),
            target=IntegrationName.SEMGREP,
            scope=scope,
            findings=outcome.findings,
        )

    def review(
        self,
        *,
        base_sha: str,
        head_sha: str,
        repository_root: str | Path | None = None,
        progress_callback: Callable[[CodeRabbitProgress], None] | None = None,
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        adapter = self.registry.adapter(IntegrationName.CODERABBIT)
        if not isinstance(adapter, CodeRabbitAdapter):
            raise ToolingError(ResultCode.TOOLING_PRECONDITION_FAILED, "CodeRabbit adapter имеет неверный тип.")
        outcome = adapter.review(
            root,
            config,
            base_sha=base_sha,
            head_sha=head_sha,
            progress_callback=progress_callback,
        )
        cycle_summary = outcome.coderabbit_cycle
        if cycle_summary is None:
            try:
                cycle_summary = adapter.cycle_summary(root)
            except ToolingError:
                cycle_summary = None
        return self._result(
            "review",
            (outcome.record,),
            target=IntegrationName.CODERABBIT,
            findings=outcome.findings,
            coderabbit_cycle=cycle_summary,
        )

    def findings(
        self,
        *,
        base_sha: str,
        head_sha: str,
        repository_root: str | Path | None = None,
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        adapter = self.registry.adapter(IntegrationName.CODERABBIT)
        if not isinstance(adapter, CodeRabbitAdapter):
            raise ToolingError(ResultCode.TOOLING_PRECONDITION_FAILED, "CodeRabbit adapter имеет неверный тип.")
        outcome = adapter.findings(root, config, base_sha=base_sha, head_sha=head_sha)
        return self._result("findings", (outcome.record,), target=IntegrationName.CODERABBIT, findings=outcome.findings, coderabbit_cycle=outcome.coderabbit_cycle)

    def recover_coderabbit_review(
        self, *, repository_root: str | Path | None = None
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        adapter = self.registry.adapter(IntegrationName.CODERABBIT)
        if not isinstance(adapter, CodeRabbitAdapter):
            raise ToolingError(ResultCode.TOOLING_PRECONDITION_FAILED, "CodeRabbit adapter имеет неверный тип.")
        outcome = adapter.recover_interrupted_review(root, config)
        return self._result("recover", (outcome.record,), target=IntegrationName.CODERABBIT, findings=outcome.findings, coderabbit_cycle=outcome.coderabbit_cycle)

    def reconcile_coderabbit(
        self, *, repository_root: str | Path | None = None
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        """Явно согласовать persistent WSL managed clone CodeRabbit."""

        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        adapter = self.registry.adapter(IntegrationName.CODERABBIT)
        if not isinstance(adapter, CodeRabbitAdapter):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "CodeRabbit adapter имеет неверный тип.",
            )
        outcome = adapter.reconcile(root, config)
        return self._result(
            "reconcile",
            (outcome.record,),
            target=IntegrationName.CODERABBIT,
            findings=outcome.findings,
            coderabbit_cycle=outcome.coderabbit_cycle,
        )

    def start_coderabbit_cycle(
        self,
        *,
        base_sha: str | None = None,
        repository_root: str | Path | None = None,
    ) -> ToolingResult[IntegrationDetails, IntegrationEvidenceBundle]:
        """Создать новый CodeRabbit cycle без запуска provider review."""

        root = self.resolve_root(repository_root)
        config = load_integration_config(root)
        adapter = self.registry.adapter(IntegrationName.CODERABBIT)
        if not isinstance(adapter, CodeRabbitAdapter):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "CodeRabbit adapter имеет неверный тип.",
            )
        outcome = adapter.start_cycle(root, config, base_sha=base_sha)
        return self._result(
            "cycle-start",
            (outcome.record,),
            target=IntegrationName.CODERABBIT,
            findings=outcome.findings,
            coderabbit_cycle=outcome.coderabbit_cycle,
        )


__all__ = ["ADAPTER_ORDER", "IntegrationRegistry", "IntegrationService"]
