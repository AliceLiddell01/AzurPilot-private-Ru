"""Публичный `azur` CLI: argparse parser и presentation adapter."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from pydantic import BaseModel

from .integrations import IntegrationService
from .integrations.coderabbit import CodeRabbitProgress
from .integrations.contracts import IntegrationName
from .tooling.bootstrap import BuildService
from .tooling.contracts import (
    AnalysisScope,
    CapabilityStatus,
    DeliveryPhase,
    GitRange,
    McpLifecycleDetails,
    McpStatusDetails,
    McpVersionDetails,
    OperationState,
    ResultCode,
    ToolingResult,
    exit_code_for,
)
from .tooling.delivery import DeliveryService
from .tooling.doctor import DoctorService
from .tooling.errors import ToolingError
from .tooling.lifecycle import LifecycleService
from .tooling.mcp import McpService
from .tooling.pull_request import PullRequestService
from .tooling.repair import RepairService
from .tooling.update import UpdateService


class CliInvocationError(Exception):
    """Ошибка argv без побочного вывода argparse в машинном режиме."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliInvocationError(message)


@dataclass
class ServiceContainer:
    """Граница внедрения сервисов для CLI-тестов и будущих адаптеров транспорта."""

    doctor: DoctorService
    lifecycle: LifecycleService
    build: BuildService
    repair: RepairService
    update: UpdateService
    delivery: DeliveryService
    pull_request: PullRequestService
    mcp: McpService
    integrations: IntegrationService

    @classmethod
    def create(cls) -> ServiceContainer:
        mcp = McpService()
        integrations = IntegrationService()
        return cls(
            doctor=DoctorService(integrations=integrations),
            lifecycle=LifecycleService(),
            build=BuildService(),
            repair=RepairService(),
            update=UpdateService(mcp_service=mcp),
            delivery=DeliveryService(),
            pull_request=PullRequestService(),
            mcp=mcp,
            integrations=integrations,
        )


def _add_common_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--repository-root",
        dest="repository_root",
        metavar="PATH",
        default=default,
        help="явно указать проверенный корень репозитория",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="вывести один машинно-читаемый JSON-отчёт",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="отключить ANSI и цвета Rich",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="показать идентификатор операции и дополнительные сведения",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="синоним --verbose для диагностики оператора",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="azur",
        description="Безопасное кроссплатформенное управление checkout AzurPilot.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    doctor = subparsers.add_parser(
        "doctor", help="проверка возможностей проекта без изменений"
    )
    _add_common_options(doctor, suppress_defaults=True)
    doctor.add_argument(
        "--full",
        action="store_true",
        help="добавить дорогую read-only проверку внешних интеграций",
    )

    start = subparsers.add_parser(
        "start", help="запустить WebUI после проверки владения и готовности"
    )
    _add_common_options(start, suppress_defaults=True)
    start.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="общий срок проверки готовности",
    )
    start.add_argument(
        "--browser",
        action="store_true",
        default=argparse.SUPPRESS,
        help="открыть WebUI после подтверждённой готовности",
    )
    start.add_argument(
        "--no-browser",
        dest="browser",
        action="store_false",
        default=argparse.SUPPRESS,
        help="не открывать WebUI автоматически",
    )
    start.add_argument(
        "--foreground", action="store_true", help="удерживать CLI до остановки службы WebUI"
    )

    stop = subparsers.add_parser(
        "stop", help="остановить только подтверждённое дерево WebUI"
    )
    _add_common_options(stop, suppress_defaults=True)
    stop.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="срок остановки",
    )

    build = subparsers.add_parser(
        "build", help="подготовить окружение Python без Git update"
    )
    _add_common_options(build, suppress_defaults=True)
    build.add_argument(
        "--timeout",
        type=float,
        default=30 * 60,
        metavar="SECONDS",
        help="общий срок подготовки",
    )
    shortcut_group = build.add_mutually_exclusive_group()
    shortcut_group.add_argument(
        "--shortcut",
        dest="shortcut",
        action="store_true",
        default=argparse.SUPPRESS,
        help="создать или проверить ярлык Windows",
    )
    shortcut_group.add_argument(
        "--no-shortcut",
        dest="shortcut",
        action="store_false",
        default=argparse.SUPPRESS,
        help="не изменять ярлык Windows",
    )

    repair = subparsers.add_parser(
        "repair", help="диагностировать и транзакционно восстановить окружение"
    )
    _add_common_options(repair, suppress_defaults=True)
    repair.add_argument(
        "--diagnostic-only", action="store_true", help="не выполнять изменения"
    )
    repair_shortcut_group = repair.add_mutually_exclusive_group()
    repair_shortcut_group.add_argument(
        "--repair-shortcut",
        dest="repair_shortcut",
        action="store_true",
        help="восстановить ярлык Windows после проверки окружения",
    )
    repair_shortcut_group.add_argument(
        "--shortcut-only",
        dest="shortcut_only",
        action="store_true",
        help="восстановить только ярлык Windows",
    )
    repair.add_argument(
        "--timeout",
        type=float,
        default=30 * 60,
        metavar="SECONDS",
        help="общий срок восстановления",
    )

    update = subparsers.add_parser(
        "update", help="выполнить только проверенный fast-forward из upstream"
    )
    _add_common_options(update, suppress_defaults=True)
    update.add_argument(
        "--expected-branch", default=None, help="ожидаемая рабочая ветка"
    )
    update.add_argument("--remote", default=None, help="имя Git remote")
    update.add_argument("--remote-branch", default=None, help="имя ветки remote")
    update.add_argument(
        "--expected-origin-url",
        default=None,
        help="ожидаемая каноническая идентичность настроенного remote",
    )
    update.add_argument(
        "--timeout",
        type=float,
        default=30 * 60,
        metavar="SECONDS",
        help="общий срок обновления",
    )

    delivery = subparsers.add_parser(
        "delivery", help="проверить или опубликовать allowlisted Git delivery"
    )
    delivery_subparsers = delivery.add_subparsers(
        dest="delivery_command", required=True, metavar="ACTION"
    )
    delivery_validate = delivery_subparsers.add_parser(
        "validate", help="только проверить manifest и exact repository state"
    )
    _add_common_options(delivery_validate, suppress_defaults=True)
    delivery_validate.add_argument("manifest", metavar="MANIFEST")
    delivery_publish = delivery_subparsers.add_parser(
        "publish", help="staged scan, commit, scoped scan и ordinary push"
    )
    _add_common_options(delivery_publish, suppress_defaults=True)
    delivery_publish.add_argument("manifest", metavar="MANIFEST")
    for action in ("status", "recover"):
        delivery_status = delivery_subparsers.add_parser(
            action,
            help=(
                "прочитать delivery journal"
                if action == "status"
                else "выполнить только read-only recovery push state"
            ),
        )
        _add_common_options(delivery_status, suppress_defaults=True)
        delivery_status.add_argument("operation_id", metavar="OPERATION_ID")

    pr = subparsers.add_parser(
        "pr", help="подготовить, опубликовать или проверить draft PR"
    )
    pr_subparsers = pr.add_subparsers(
        dest="pr_command", required=True, metavar="ACTION"
    )
    pr_prepare = pr_subparsers.add_parser(
        "prepare", help="проверить spec, Git identity и structured PR body"
    )
    _add_common_options(pr_prepare, suppress_defaults=True)
    pr_prepare.add_argument("spec", metavar="SPEC")
    pr_publish = pr_subparsers.add_parser(
        "publish", help="создать или подтвердить draft PR через gh"
    )
    _add_common_options(pr_publish, suppress_defaults=True)
    pr_publish.add_argument("spec", metavar="SPEC")
    pr_verify = pr_subparsers.add_parser(
        "verify", help="прочитать PR и подтвердить exact identity/body"
    )
    _add_common_options(pr_verify, suppress_defaults=True)
    pr_verify.add_argument("number", type=int, metavar="PR_NUMBER")
    pr_verify.add_argument("--spec", required=True, metavar="SPEC")

    mcp = subparsers.add_parser(
        "mcp", help="проверить и согласовать first-party MCP source/runtime"
    )
    mcp_subparsers = mcp.add_subparsers(
        dest="mcp_command", required=True, metavar="ACTION"
    )
    for action in ("status", "versions", "start", "stop", "restart"):
        command = mcp_subparsers.add_parser(
            action,
            help={
                "status": "прочитать source, runtime, plugin и session state",
                "versions": "прочитать canonical MCP bundle versions и hashes",
                "start": "запустить owned loopback MCP supervisor",
                "stop": "остановить owned loopback MCP supervisor",
                "restart": "перезапустить owned loopback MCP supervisor",
            }[action],
        )
        _add_common_options(command, suppress_defaults=True)
    reconcile = mcp_subparsers.add_parser(
        "reconcile", help="согласовать source bundle или owned runtime"
    )
    _add_common_options(reconcile, suppress_defaults=True)
    reconcile.add_argument(
        "--source",
        action="store_true",
        help="обновить tracked canonical manifest и derived plugin metadata",
    )
    reconcile.add_argument(
        "--bump",
        choices=("auto", "patch", "minor", "major"),
        default=None,
        help="явная политика server SemVer для доказанного contract change",
    )

    integrations = subparsers.add_parser(
        "integrations", help="проверить прямые внешние интеграции"
    )
    integration_subparsers = integrations.add_subparsers(
        dest="integration_target", required=True, metavar="TARGET"
    )
    for action in ("status", "doctor"):
        command = integration_subparsers.add_parser(
            action,
            help=(
                "прочитать конфигурацию и доступность интеграций"
                if action == "status"
                else "выполнить bounded read-only probes интеграций"
            ),
        )
        _add_common_options(command, suppress_defaults=True)
    for name in IntegrationName:
        provider = integration_subparsers.add_parser(
            name.value, help=f"операции интеграции {name.value}"
        )
        provider_subparsers = provider.add_subparsers(
            dest="integration_action", required=True, metavar="ACTION"
        )
        for action in ("status", "doctor", "probe"):
            command = provider_subparsers.add_parser(
                action,
                help=(
                    "прочитать конфигурацию"
                    if action == "status"
                    else "выполнить bounded read-only probe"
                ),
            )
            _add_common_options(command, suppress_defaults=True)
        if name is IntegrationName.SEMGREP:
            scan = provider_subparsers.add_parser(
                "scan", help="выполнить только явно ограниченный Semgrep scan"
            )
            _add_common_options(scan, suppress_defaults=True)
            scope_group = scan.add_mutually_exclusive_group(required=False)
            scope_group.add_argument(
                "--staged", action="store_true", help="взять только staged paths"
            )
            scope_group.add_argument(
                "--changed", action="store_true", help="взять paths из base..HEAD"
            )
            scan.add_argument(
                "--base",
                dest="scan_base",
                default=None,
                help="exact base SHA для --changed",
            )
            scope_group.add_argument(
                "--paths",
                action="append",
                default=[],
                metavar="PATH",
                help="явный repository-relative файл; параметр можно повторять",
            )
        if name is IntegrationName.CODERABBIT:
            review = provider_subparsers.add_parser(
                "review", help="запустить advisory CodeRabbit review"
            )
            _add_common_options(review, suppress_defaults=True)
            review.add_argument("--base", required=True, help="exact base SHA")
            review.add_argument(
                "--head", default=None, help="exact review HEAD; по умолчанию текущий HEAD"
            )
            cycle = provider_subparsers.add_parser(
                "cycle", help="управлять bounded CodeRabbit review cycles"
            )
            cycle_subparsers = cycle.add_subparsers(
                dest="coderabbit_cycle_action", required=True, metavar="ACTION"
            )
            cycle_start = cycle_subparsers.add_parser(
                "start", help="создать новый cycle без запуска provider review"
            )
            _add_common_options(cycle_start, suppress_defaults=True)
            cycle_start.add_argument(
                "--base", default=None, help="необязательный exact base SHA"
            )
    return parser


def _json_requested(argv: Sequence[str] | None) -> bool:
    return bool(argv and "--json" in argv)


def _error_result(error: ToolingError) -> ToolingResult[BaseModel, BaseModel]:
    return ToolingResult[BaseModel, BaseModel](
        ok=False,
        code=error.code,
        state=error.state,
        message=error.message,
        operation_id=error.operation_id,
        details=error.details,
        evidence=error.evidence,
    )


def _invocation_result(message: str) -> ToolingResult[BaseModel, BaseModel]:
    return ToolingResult[BaseModel, BaseModel](
        ok=False,
        code=ResultCode.TOOLING_INVALID_INVOCATION,
        state=OperationState.FAILED,
        message=message[:300],
    )


def _unexpected_result(exception_type: str | None = None) -> ToolingResult[BaseModel, BaseModel]:
    suffix = f" Тип исключения: {exception_type[:80]}." if exception_type else ""
    return ToolingResult[BaseModel, BaseModel](
        ok=False,
        code=ResultCode.TOOLING_UNEXPECTED,
        state=OperationState.UNKNOWN,
        message=(
            "Операция завершилась непредвиденной ошибкой; постусловие не подтверждено."
            + suffix
        ),
    )


def _render_json(result: ToolingResult[BaseModel, BaseModel], stdout: TextIO) -> None:
    stdout.write(result.model_dump_json(exclude_none=True) + "\n")
    stdout.flush()


def _short_sha(value: str | None) -> str:
    if not value:
        return "не создан"
    return value[:12] + "…"


def _coderabbit_progress_callback(stream: TextIO):
    """Создать Rich-представление bounded heartbeat CodeRabbit."""

    try:
        from rich.console import Console
        from rich.text import Text

        console = Console(file=stream, no_color=True, force_terminal=False, highlight=False)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        console = None

    phase_labels = {
        "preflight": "предпроверка",
        "clone_ready": "clone готов",
        "provider_preflight": "предпроверка provider",
        "provider_started": "provider запущен",
        "provider_running": "provider выполняется",
        "provider_finished": "provider завершён",
        "timeout": "тайм-аут",
        "output_truncated": "вывод усечён",
        "rate_limited": "ограничение provider",
        "provider_failed": "ошибка provider",
        "parse_failed": "ошибка разбора",
        "complete": "завершено",
    }

    def emit(event: CodeRabbitProgress) -> None:
        line = (
            f"CodeRabbit | этап {phase_labels.get(event.phase, event.phase)} | "
            f"цикл {event.cycle_id[:16]} | "
            f"бюджет {event.substantive_iterations}/3 | {event.message}"
        )
        if console is not None:
            console.print(Text(line))
        else:
            stream.write(line + "\n")
            stream.flush()

    return emit


def _render_delivery_validation_preview(
    console: Any, result: ToolingResult[BaseModel, BaseModel]
) -> bool:
    """Показать bounded read-only preview для успешного delivery validate."""

    details = result.details
    evidence = result.evidence
    snapshot = getattr(evidence, "snapshot", None)
    changes = tuple(getattr(details, "changes", ()))
    if (
        not result.ok
        or getattr(details, "phase", None) is not DeliveryPhase.VALIDATED
        or snapshot is None
        or not changes
    ):
        return False

    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    summary = Table.grid(expand=True, padding=(0, 1))
    summary.add_column(no_wrap=True)
    summary.add_column(overflow="fold")
    summary.add_row(
        Text("Репозиторий"), Text(snapshot.repository.slug)
    )
    summary.add_row(Text("Ветка"), Text(snapshot.branch))
    summary.add_row(Text("Local HEAD"), Text(_short_sha(snapshot.head_sha)))
    summary.add_row(
        Text("Base"),
        Text(f"{snapshot.base_branch} @ {_short_sha(snapshot.base_sha)}"),
    )
    remote_label = f"{snapshot.remote_name}/{snapshot.remote_branch} @ {_short_sha(snapshot.remote_sha)}"
    summary.add_row(Text("Remote"), Text(remote_label))
    summary.add_row(
        Text("Файлы"),
        Text(str(getattr(details, "target_count", len(changes)))),
    )
    summary.add_row(Text("SHA-256"), Text("подтверждён"))
    summary.add_row(Text("Состояние Git"), Text("совместимо"))
    console.print(Panel(summary, title="Delivery Package", expand=True))

    console.print(Text("Изменения:"))
    preview_limit = 20
    for change in changes[:preview_limit]:
        console.print(Text(f"  {change.change} {change.path}"))
    target_count = int(getattr(details, "target_count", len(changes)))
    hidden_count = max(0, target_count - preview_limit)
    if hidden_count:
        console.print(Text(f"  … ещё {hidden_count} target paths."))
    console.print(Text("Изменения не применены."))
    return True


def _render_human(
    result: ToolingResult[BaseModel, BaseModel],
    stdout: TextIO,
    stderr: TextIO,
    *,
    no_color: bool,
    verbose: bool,
    delivery_validation_preview: bool = False,
) -> None:
    stream = stdout if result.ok else stderr

    def render_verbose(console: Any) -> None:
        if not verbose:
            return
        console.print(f"код: {result.code.value}")
        console.print(f"состояние: {result.state.value}")
        if result.operation_id:
            console.print(f"идентификатор операции: {result.operation_id}")
        for label, model in (("детали", result.details), ("доказательства", result.evidence)):
            if model is not None:
                console.print(
                    f"{label}: "
                    + json.dumps(
                        model.model_dump(mode="json", exclude_none=True),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )

    def status_label(status: CapabilityStatus) -> str:
        return {
            CapabilityStatus.READY: "готово",
            CapabilityStatus.NOT_CONFIGURED: "не настроено",
            CapabilityStatus.UNAVAILABLE: "недоступно",
            CapabilityStatus.UNSUPPORTED: "не поддерживается",
            CapabilityStatus.FAILED: "ошибка",
            CapabilityStatus.UNKNOWN: "неизвестно",
        }[status]

    def check_label(name: str) -> str:
        return {
            "repository": "Репозиторий",
            "project_markers": "Маркеры проекта",
            "python": "Python",
            "git": "Git",
            "runtime": "Среда выполнения",
            "uv": "uv",
            "project_environment": "Окружение проекта",
            "deploy_config": "Конфигурация",
            "console_script": "Консольная команда azur",
            "console_path": "PATH",
            "adb": "ADB",
            "docker": "Docker/PostgreSQL",
            "external_integrations": "Внешние интеграции",
        }.get(name, name)

    def integration_label(value: object) -> str:
        return {
            "READY": "готово",
            "NOT_CONFIGURED": "не настроено",
            "UNAVAILABLE": "недоступно",
            "UNAUTHENTICATED": "нет аутентификации",
            "RATE_LIMITED": "ограничение провайдера",
            "INCOMPATIBLE": "несовместимо",
            "DEGRADED": "ограничено",
            "UNKNOWN": "неизвестно",
        }.get(str(getattr(value, "value", value)), "неизвестно")

    try:
        from rich.console import Console
        from rich.text import Text

        is_tty = bool(getattr(stream, "isatty", lambda: False)())
        console = Console(
            file=stream,
            no_color=no_color or not is_tty or bool(os.environ.get("NO_COLOR")),
            force_terminal=False,
            highlight=False,
        )

        checks = getattr(result.details, "checks", None)
        integrations = getattr(result.details, "integrations", None)
        if integrations is not None:
            from rich.table import Table

            table = Table(title="Внешние интеграции AzurPilot", expand=True)
            table.add_column("Интеграция", no_wrap=True)
            table.add_column("Состояние", no_wrap=True)
            table.add_column("Маршрут", no_wrap=True)
            table.add_column("Результат", overflow="fold")
            for item in integrations:
                value = str(getattr(getattr(item, "state", None), "value", "UNKNOWN"))
                marker = "✓" if value == "READY" else "⚠"
                evidence = getattr(item, "evidence", None)
                route = getattr(evidence, "route", "direct")
                table.add_row(
                    str(getattr(getattr(item, "name", None), "value", "неизвестно")),
                    f"{marker} {integration_label(value)}",
                    str(route),
                    str(getattr(item, "message", "Состояние не подтверждено.")),
                )
            console.print(table)
            cycle = getattr(result.details, "coderabbit_cycle", None)
            if cycle is not None:
                cycle_table = Table(title="Цикл ревью CodeRabbit", expand=True)
                cycle_table.add_column("Поле", no_wrap=True)
                cycle_table.add_column("Значение", overflow="fold")
                cycle_rows = (
                    ("цикл", cycle.cycle_id),
                    ("статус", cycle.cycle_status),
                    (
                        "бюджет",
                        f"{cycle.substantive_iterations}/{cycle.substantive_budget}",
                    ),
                    ("provider", cycle.provider_state),
                    ("ограничение с", cycle.rate_limited_at or "не наблюдалось"),
                    ("повторить не ранее", cycle.retry_not_before or "не задано"),
                    ("источник retry", cycle.retry_source),
                    (
                        "последний проверенный head",
                        cycle.last_reviewed_head or "не наблюдался",
                    ),
                    ("сохранённых циклов", str(cycle.previous_cycles_retained)),
                )
                for label, value in cycle_rows:
                    cycle_table.add_row(Text(str(label)), Text(str(value)))
                console.print(cycle_table)
            findings = tuple(getattr(result.details, "findings", ()))
            if findings:
                severity_labels = {
                    "critical": "критический",
                    "major": "высокий",
                    "minor": "средний",
                    "trivial": "незначительный",
                    "info": "информация",
                }
                disposition_labels = {
                    "confirmed": "подтверждено",
                    "partially confirmed": "частично подтверждено",
                    "false positive": "ложное срабатывание",
                    "insufficient evidence": "недостаточно данных",
                }
                findings_table = Table(
                    title="Сводка замечаний CodeRabbit", expand=True
                )
                findings_table.add_column("№", justify="right", no_wrap=True)
                findings_table.add_column("Уровень", no_wrap=True)
                findings_table.add_column("Путь", overflow="fold")
                findings_table.add_column("Воздействие", overflow="fold")
                findings_table.add_column("Классификация", overflow="fold")
                findings_table.add_column("Решение", overflow="fold")
                for index, finding in enumerate(findings, start=1):
                    severity = str(getattr(finding, "severity", "info"))
                    disposition = str(getattr(finding, "disposition", ""))
                    findings_table.add_row(
                        Text(str(index)),
                        Text(severity_labels.get(severity, severity)),
                        Text(str(getattr(finding, "path", "не указан"))),
                        Text(str(getattr(finding, "message", "не указано"))),
                        Text(disposition_labels.get(disposition, disposition or "не классифицировано")),
                        Text(str(getattr(finding, "resolution", "не указано"))),
                    )
                console.print(findings_table)
            console.print(f"{'✓' if result.ok else '✗'} {result.message}")
        elif checks is not None:
            from rich.table import Table

            table = Table(title="AzurPilot Doctor", expand=True)
            table.add_column("Проверка", no_wrap=True)
            table.add_column("Состояние", no_wrap=True)
            table.add_column("Результат", overflow="fold")
            for check in checks:
                state = status_label(check.status)
                marker = (
                    "✓"
                    if check.status is CapabilityStatus.READY
                    else "?"
                    if check.status is CapabilityStatus.UNKNOWN
                    else "⚠"
                )
                table.add_row(check_label(check.name), f"{marker} {state}", check.message)
            console.print(table)
            console.print(
                f"{'✓' if result.ok else '✗'} {result.message}"
            )
        else:
            if isinstance(
                result.details,
                (McpLifecycleDetails, McpStatusDetails, McpVersionDetails),
            ):
                servers = (
                    result.details.services
                    if isinstance(result.details, McpLifecycleDetails)
                    else result.details.servers
                )
                from rich.table import Table

                table = Table(title="AzurPilot MCP", expand=True)
                table.add_column("Backend", no_wrap=True)
                table.add_column("Версия", no_wrap=True)
                table.add_column("Состояние", no_wrap=True)
                table.add_column("Transport", overflow="fold")
                table.add_column("Revision", no_wrap=True)
                for server in servers:
                    table.add_row(
                        str(getattr(server, "server_name", "unknown")),
                        str(
                            getattr(server, "observed_version", None)
                            or getattr(server, "expected_version", "unknown")
                        ),
                        str(getattr(server, "status", "unknown")),
                        ", ".join(getattr(server, "routes", ())) or "не наблюдается",
                        _short_sha(getattr(server, "contract_revision", None)),
                    )
                console.print(table)
                for field, label in (
                    ("source_state", "Source"),
                    ("runtime_state", "Runtime"),
                    ("plugin_state", "Plugin"),
                    ("plugin_source_state", "Plugin source"),
                    ("session_state", "Session"),
                ):
                    value = getattr(result.details, field, None)
                    if value is not None:
                        console.print(f"{label}: {value}")
                console.print(f"{'✓' if result.ok else '✗'} {result.message}")
            elif delivery_validation_preview:
                console.print(f"{'✓' if result.ok else '✗'} {result.message}")
                _render_delivery_validation_preview(console, result)
            else:
                console.print(f"{'✓' if result.ok else '✗'} {result.message}")

        for warning in result.warnings:
            console.print(f"⚠ {warning.message}")
        render_verbose(console)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        stream.write(f"{'✓' if result.ok else '✗'} {result.message}\n")
        for warning in result.warnings:
            stream.write(f"⚠ {warning.message}\n")
        if verbose:
            stream.write(f"код: {result.code.value}\n")
            stream.write(f"состояние: {result.state.value}\n")
            if result.operation_id:
                stream.write(f"идентификатор операции: {result.operation_id}\n")
        stream.flush()


def _dispatch(
    args: argparse.Namespace,
    services: ServiceContainer,
    *,
    progress_stream: TextIO | None = None,
) -> ToolingResult[BaseModel, BaseModel]:
    root = getattr(args, "repository_root", None)
    command = args.command
    if command == "doctor":
        if getattr(args, "full", False):
            return services.doctor.run(root, include_external_integrations=True)
        return services.doctor.run(root)
    if command == "start":
        return services.lifecycle.start(
            root,
            timeout_seconds=args.timeout,
            open_browser=getattr(args, "browser", False),
            foreground=args.foreground,
        )
    if command == "stop":
        return services.lifecycle.stop(root, timeout_seconds=args.timeout)
    if command == "build":
        return services.build.build(
            root,
            timeout_seconds=args.timeout,
            create_shortcut=getattr(args, "shortcut", None),
        )
    if command == "repair":
        return services.repair.repair(
            root,
            diagnostic_only=args.diagnostic_only,
            repair_shortcut=getattr(args, "repair_shortcut", False),
            shortcut_only=getattr(args, "shortcut_only", False),
            timeout_seconds=args.timeout,
        )
    if command == "update":
        return services.update.update(
            root,
            expected_branch=args.expected_branch,
            remote_name=args.remote,
            remote_branch=args.remote_branch,
            expected_origin_url=args.expected_origin_url,
            timeout_seconds=args.timeout,
        )
    if command == "delivery":
        if args.delivery_command == "validate":
            return services.delivery.validate(args.manifest, root)
        if args.delivery_command == "publish":
            return services.delivery.publish(args.manifest, root)
        if args.delivery_command == "status":
            return services.delivery.status(args.operation_id, root)
        if args.delivery_command == "recover":
            return services.delivery.recover(args.operation_id, root)
    if command == "pr":
        if args.pr_command == "prepare":
            return services.pull_request.prepare(args.spec, root)
        if args.pr_command == "publish":
            return services.pull_request.publish(args.spec, root)
        if args.pr_command == "verify":
            return services.pull_request.verify(args.number, args.spec, root)
    if command == "mcp":
        if args.mcp_command == "status":
            return services.mcp.status(root)
        if args.mcp_command == "versions":
            return services.mcp.versions(root)
        if args.mcp_command == "reconcile":
            source = bool(getattr(args, "source", False))
            bump = getattr(args, "bump", None)
            if bump is not None and not source:
                raise CliInvocationError(
                    "Параметр --bump допускается только вместе с --source."
                )
            return services.mcp.reconcile(
                root,
                source=source,
                bump=bump,
            )
        if args.mcp_command == "start":
            return services.mcp.start(root)
        if args.mcp_command == "stop":
            return services.mcp.stop(root)
        if args.mcp_command == "restart":
            return services.mcp.restart(root)
    if command == "integrations":
        target = args.integration_target
        if target == "status":
            return services.integrations.status(root)
        if target == "doctor":
            return services.integrations.doctor(root)
        action = args.integration_action
        if target == IntegrationName.SEMGREP.value and action == "scan":
            integration_root = services.integrations.resolve_root(root)
            paths = tuple(
                item.strip()
                for raw in getattr(args, "paths", ())
                for item in raw.split(",")
                if item.strip()
            )
            scope_count = sum((bool(args.changed), bool(args.staged), bool(paths)))
            if scope_count == 0:
                raise CliInvocationError(
                    "Semgrep scan требует --staged, --changed или --paths."
                )
            if scope_count > 1:
                raise CliInvocationError(
                    "Semgrep scan принимает только один scope: --staged, --changed или --paths."
                )
            if args.scan_base and not args.changed:
                raise CliInvocationError("--base разрешён только вместе с --changed.")
            if args.changed:
                if not args.scan_base:
                    raise CliInvocationError("--changed требует --base с exact SHA.")
                from .tooling.git import GitClient

                end_sha = GitClient(integration_root).head()
                scope = AnalysisScope(
                    paths=paths,
                    mode="committed_range",
                    git_range=GitRange(start_sha=args.scan_base, end_sha=end_sha),
                )
            else:
                scope = AnalysisScope(paths=paths, mode="staged")
            return services.integrations.scan(scope, root)
        if target == IntegrationName.CODERABBIT.value and action == "review":
            integration_root = services.integrations.resolve_root(root)
            head = args.head
            if head is None:
                from .tooling.git import GitClient

                head = GitClient(integration_root).head()
            return services.integrations.review(
                base_sha=args.base,
                head_sha=head,
                repository_root=root,
                progress_callback=(
                    _coderabbit_progress_callback(progress_stream or sys.stderr)
                    if not getattr(args, "json", False)
                    else None
                ),
            )
        if (
            target == IntegrationName.CODERABBIT.value
            and action == "cycle"
            and args.coderabbit_cycle_action == "start"
        ):
            return services.integrations.start_coderabbit_cycle(
                base_sha=args.base,
                repository_root=root,
            )
        if action == "status":
            return services.integrations.status_one(target, root)
        if action == "doctor" or action == "probe":
            return services.integrations.probe(target, root)
    raise CliInvocationError(f"неизвестная команда: {command}")


def main(
    argv: Sequence[str] | None = None,
    *,
    services: ServiceContainer | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Выполнить CLI и вернуть exit code вместо немедленного `sys.exit`."""

    args_list = list(argv) if argv is not None else sys.argv[1:]
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    for stream in (stdout, stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    json_mode = _json_requested(args_list)
    parser = build_parser()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            args = parser.parse_args(args_list)
    except SystemExit as exit_signal:
        return int(exit_signal.code or 0)
    except CliInvocationError as error:
        result = _invocation_result(str(error))
        if json_mode:
            _render_json(result, stdout)
        else:
            parser.print_usage(file=stderr)
            stderr.write(f"Ошибка вызова: {error}\n")
        return int(exit_code_for(result.code))

    json_mode = bool(getattr(args, "json", False))
    verbose = bool(getattr(args, "verbose", False) or getattr(args, "debug", False))
    try:
        if json_mode:
            # Сервисные границы могут использовать сторонние библиотеки с выводом.
            # Машинный режим обязан вернуть ровно один документ в stdout.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                result = _dispatch(
                    args,
                    services or ServiceContainer.create(),
                    progress_stream=stderr,
                )
        else:
            result = _dispatch(
                args,
                services or ServiceContainer.create(),
                progress_stream=stderr,
            )
    except CliInvocationError as error:
        result = _invocation_result(str(error))
    except ToolingError as error:
        result = _error_result(error)
    except KeyboardInterrupt:
        result = ToolingResult[BaseModel, BaseModel](
            ok=False,
            code=ResultCode.TOOLING_CANCELLED,
            state=OperationState.UNKNOWN,
            message="Операция прервана пользователем; итоговое состояние требует проверки.",
        )
    except Exception as error:  # noqa: BLE001 - CLI обязан вернуть ограниченный envelope ошибки
        result = _unexpected_result(type(error).__name__)

    if json_mode:
        _render_json(result, stdout)
    else:
        _render_human(
            result,
            stdout,
            stderr,
            no_color=bool(getattr(args, "no_color", False)),
            verbose=verbose,
            delivery_validation_preview=(
                args.command == "delivery"
                and args.delivery_command == "validate"
            ),
        )
    return int(exit_code_for(result.code, result.ok))


__all__ = ["ServiceContainer", "build_parser", "main"]
