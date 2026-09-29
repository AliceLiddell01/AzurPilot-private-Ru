"""Публичный `azur` CLI: разбор аргументов argparse и адаптер представления."""

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
from .integrations.contracts import IntegrationName, SharedMcpDetails
from .tooling.application_state import ApplicationStateService
from .tooling.bootstrap import BuildService
from .tooling.bot_runtime import BotRuntimeService
from .tooling.contracts import (
    AnalysisScope,
    CapabilityStatus,
    GitRange,
    McpBridgeAcceptanceDetails,
    McpBridgeStatusDetails,
    McpImpactDetails,
    McpLifecycleDetails,
    McpStatusDetails,
    McpVersionDetails,
    OperationState,
    ResultCode,
    ToolingResult,
    ToolingWarning,
    WarningCode,
    exit_code_for,
)
from .tooling.docker import DockerDeploymentService
from .tooling.doctor import DoctorService
from .tooling.dsh import (
    DEFAULT_DSH_PACKAGE,
    DEFAULT_DSH_PROFILE,
    DshBridgeService,
    load_generation,
)
from .tooling.errors import ToolingError
from .tooling.lifecycle import LifecycleService
from .tooling.mcp import McpService
from .tooling.repair import RepairService


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
    docker: DockerDeploymentService
    mcp: McpService
    application_state: ApplicationStateService
    integrations: IntegrationService
    bot_runtime: BotRuntimeService
    dsh: DshBridgeService

    @classmethod
    def create(cls) -> ServiceContainer:
        mcp = McpService()
        integrations = IntegrationService()
        return cls(
            doctor=DoctorService(integrations=integrations),
            lifecycle=LifecycleService(),
            build=BuildService(),
            repair=RepairService(),
            docker=DockerDeploymentService(),
            mcp=mcp,
            application_state=ApplicationStateService(),
            integrations=integrations,
            bot_runtime=BotRuntimeService(),
            dsh=DshBridgeService(),
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


def _add_webui_start_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="общий срок проверки готовности",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        default=argparse.SUPPRESS,
        help="открыть WebUI после подтверждённой готовности",
    )
    parser.add_argument(
        "--no-browser",
        dest="browser",
        action="store_false",
        default=argparse.SUPPRESS,
        help="не открывать WebUI автоматически",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="удерживать CLI до остановки службы WebUI",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="azur",
        description="Безопасное кроссплатформенное управление рабочей копией AzurPilot.",
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
        help="добавить дорогую проверку только для чтения внешних интеграций",
    )

    start = subparsers.add_parser(
        "start", help="устаревший псевдоним webui start"
    )
    _add_common_options(start, suppress_defaults=True)
    _add_webui_start_options(start)

    stop = subparsers.add_parser(
        "stop", help="устаревший псевдоним webui stop"
    )
    _add_common_options(stop, suppress_defaults=True)
    stop.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="срок остановки",
    )

    bot = subparsers.add_parser("bot", help="управлять средой выполнения бота без графического интерфейса")
    _add_common_options(bot, suppress_defaults=True)
    bot_actions = bot.add_subparsers(dest="bot_command", required=True)
    for action, help_text, default_timeout in (
        ("start", "запустить среду выполнения бота без WebUI", 30.0),
        ("stop", "штатно остановить среду выполнения бота и его рабочие процессы", 120.0),
    ):
        action_parser = bot_actions.add_parser(action, help=help_text)
        _add_common_options(action_parser, suppress_defaults=True)
        action_parser.add_argument(
            "--timeout",
            type=float,
            default=default_timeout,
            metavar="SECONDS",
            help="общий ограниченный срок операции",
        )
    bot_status = bot_actions.add_parser("status", help="прочитать состояние среды выполнения бота")
    _add_common_options(bot_status, suppress_defaults=True)

    webui = subparsers.add_parser("webui", help="управлять только WebUI")
    _add_common_options(webui, suppress_defaults=True)
    webui_actions = webui.add_subparsers(dest="webui_command", required=True)
    webui_start = webui_actions.add_parser("start", help="запустить WebUI")
    _add_common_options(webui_start, suppress_defaults=True)
    _add_webui_start_options(webui_start)
    webui_stop = webui_actions.add_parser("stop", help="остановить только WebUI")
    _add_common_options(webui_stop, suppress_defaults=True)
    webui_stop.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="срок штатной остановки WebUI",
    )
    webui_status = webui_actions.add_parser("status", help="прочитать только жизненный цикл WebUI")
    _add_common_options(webui_status, suppress_defaults=True)

    build = subparsers.add_parser(
        "build", help="подготовить окружение Python без обновления Git"
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

    deploy = subparsers.add_parser(
        "deploy", help="выполнить явное типизированное развёртывание"
    )
    deploy_subparsers = deploy.add_subparsers(
        dest="deploy_command", required=True, metavar="TARGET"
    )
    deploy_docker = deploy_subparsers.add_parser(
        "docker", help="собрать образ и запустить контейнер через Docker CLI"
    )
    _add_common_options(deploy_docker, suppress_defaults=True)
    deploy_docker.add_argument("--image", default=None, help="имя образа Docker")
    deploy_docker.add_argument("--container", default=None, help="имя контейнера Docker")
    deploy_docker.add_argument("--port", type=int, default=None, help="локальный порт WebUI")
    deploy_docker.add_argument(
        "--source",
        default=None,
        help="полный контекст сборки Docker внутри репозитория; относительный путь от корня",
    )
    deploy_docker.add_argument(
        "--replace",
        action="store_true",
        help="явно заменить только указанный существующий контейнер с откатом",
    )
    deploy_docker.add_argument(
        "--timeout",
        type=float,
        default=20 * 60,
        metavar="SECONDS",
        help="общий срок развёртывания",
    )
    deploy_docker.add_argument(
        "--readiness-timeout",
        type=float,
        default=180.0,
        metavar="SECONDS",
        help="срок подтверждения готовности контейнера",
    )

    mcp = subparsers.add_parser(
        "mcp", help="проверить и согласовать исходники и среду выполнения MCP проекта"
    )
    mcp_subparsers = mcp.add_subparsers(
        dest="mcp_command", required=True, metavar="ACTION"
    )
    for action in ("status", "versions", "accept", "start", "stop", "restart"):
        command = mcp_subparsers.add_parser(
            action,
            help={
                "status": "прочитать состояние исходников, среды выполнения, плагина и сессии",
                "versions": "прочитать версии и хеши канонического комплекта MCP",
                "accept": "проверить MCP новой независимой клиентской сессией только для чтения",
                "start": "запустить принадлежащую проекту службу управления MCP на loopback",
                "stop": "остановить принадлежащую проекту службу управления MCP на loopback",
                "restart": "перезапустить принадлежащую проекту службу управления MCP на loopback",
            }[action],
        )
        _add_common_options(command, suppress_defaults=True)
    bridge = mcp_subparsers.add_parser(
        "bridge", help="управлять отдельным мостом Windows MCP для Dev/Game"
    )
    bridge_subparsers = bridge.add_subparsers(
        dest="mcp_bridge_command", required=True, metavar="ACTION"
    )
    for action, action_help in (
        ("status", "прочитать состояние моста Windows MCP и целевых служб"),
        ("configure", "безопасно задать отдельный токен клиента через stdin"),
        ("start", "запустить только принадлежащий проекту мост Windows MCP"),
        ("stop", "остановить только принадлежащий проекту мост Windows MCP"),
        ("restart", "перезапустить только мост Windows MCP без перезапуска Dev/Game"),
        ("accept", "проверить оба маршрута моста новой сессией MCP только для чтения"),
    ):
        command = bridge_subparsers.add_parser(action, help=action_help)
        _add_common_options(command, suppress_defaults=True)
        if action == "configure":
            command.add_argument(
                "--stdin-token",
                action="store_true",
                required=True,
                help="прочитать одну строку токена из stdin, не выводя его значение",
            )
    impact = mcp_subparsers.add_parser(
        "impact", help="определить влияние итогового набора изменений на MCP"
    )
    _add_common_options(impact, suppress_defaults=True)
    impact.add_argument("--base", required=True, help="точный SHA базы")
    sync = mcp_subparsers.add_parser(
        "sync", help="согласовать исходники MCP, среду выполнения проекта и приёмку новым клиентом"
    )
    _add_common_options(sync, suppress_defaults=True)
    sync.add_argument("--base", required=True, help="точный SHA базы проверяемого варианта")
    reconcile = mcp_subparsers.add_parser(
        "reconcile", help="согласовать комплект исходников или среду выполнения проекта"
    )
    _add_common_options(reconcile, suppress_defaults=True)
    reconcile.add_argument(
        "--source",
        action="store_true",
        help="обновить отслеживаемый канонический манифест и производные метаданные плагина",
    )
    reconcile.add_argument(
        "--bump",
        choices=("auto", "patch", "minor", "major"),
        default=None,
        help="явная политика SemVer сервера для подтверждённого изменения контракта",
    )

    dsh = subparsers.add_parser(
        "dsh",
        help="запустить DeepSeek Harness обычным Linux-клиентом моста Windows MCP",
    )
    dsh_subparsers = dsh.add_subparsers(
        dest="dsh_command", required=True, metavar="ACTION"
    )
    for action, action_help in (
        (
            "prepare",
            "собрать генерацию клиента и подтвердить оба маршрута моста новой сессией",
        ),
        (
            "launch",
            "подготовить генерацию и запустить обычный DeepSeek Harness",
        ),
        (
            "verify",
            "сверить текущий checkout с генерацией запущенной сессии клиента",
        ),
    ):
        command = dsh_subparsers.add_parser(action, help=action_help)
        _add_common_options(command, suppress_defaults=True)
        if action == "verify":
            command.add_argument(
                "--generation",
                metavar="PATH",
                default=None,
                help="конверт `azur dsh prepare --json` вместо переменных окружения",
            )
            continue
        command.add_argument(
            "--profile",
            default=DEFAULT_DSH_PROFILE,
            metavar="NAME",
            help="профиль DeepSeek Harness для запуска клиента",
        )
        command.add_argument(
            "--package",
            default=DEFAULT_DSH_PACKAGE,
            metavar="SPEC",
            help="точная спецификация пакета DeepSeek Harness",
        )
        if action == "launch":
            command.add_argument(
                "--dsh-arg",
                dest="dsh_arguments",
                action="append",
                default=[],
                metavar="ARG",
                help="дополнительный аргумент обычного запуска DeepSeek Harness",
            )

    app = subparsers.add_parser(
        "app", help="запросить типизированное состояние приложения без запуска WebUI"
    )
    app_subparsers = app.add_subparsers(
        dest="app_command", required=True, metavar="ACTION"
    )
    app_state = app_subparsers.add_parser(
        "state", help="прочитать зарегистрированное состояние приложения"
    )
    _add_common_options(app_state, suppress_defaults=True)
    app_state.add_argument("state_id", metavar="STATE_ID")
    app_state.add_argument("--profile", required=True, metavar="PROFILE")

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
                else "выполнить ограниченные проверки интеграций только для чтения"
            ),
        )
        _add_common_options(command, suppress_defaults=True)
    shared_mcp = integration_subparsers.add_parser(
        "shared-mcp", help="управлять общими внешними службами MCP HTTP"
    )
    shared_mcp_subparsers = shared_mcp.add_subparsers(
        dest="integration_shared_mcp_action", required=True, metavar="ACTION"
    )
    for action, action_help in (
        ("status", "прочитать состояние общих служб MCP HTTP"),
        ("start", "запустить общие службы MCP HTTP"),
        ("stop", "остановить общие службы MCP HTTP"),
    ):
        command = shared_mcp_subparsers.add_parser(action, help=action_help)
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
                    else "выполнить ограниченную проверку только для чтения"
                ),
            )
            _add_common_options(command, suppress_defaults=True)
        if name is IntegrationName.SEMGREP:
            scan = provider_subparsers.add_parser(
                "scan", help="выполнить только явно ограниченное сканирование Semgrep"
            )
            _add_common_options(scan, suppress_defaults=True)
            scope_group = scan.add_mutually_exclusive_group(required=False)
            scope_group.add_argument(
                "--staged", action="store_true", help="взять только пути из индекса Git"
            )
            scope_group.add_argument(
                "--changed", action="store_true", help="взять пути из диапазона Git base..HEAD"
            )
            scan.add_argument(
                "--base",
                dest="scan_base",
                default=None,
                help="точный SHA базы для --changed",
            )
            scope_group.add_argument(
                "--paths",
                action="append",
                default=[],
                metavar="PATH",
                help="явный путь относительно корня репозитория; параметр можно повторять",
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



def _render_human(
    result: ToolingResult[BaseModel, BaseModel],
    stdout: TextIO,
    stderr: TextIO,
    *,
    no_color: bool,
    verbose: bool,
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
            CapabilityStatus.NOT_CHECKED: "не проверено",
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

        if isinstance(result.details, SharedMcpDetails):
            render_verbose(console)
            console.print(result.message)
            for service in result.details.services:
                console.print(f"запущен: {service}")
            for diagnostic in result.details.diagnostics:
                console.print(f"диагностика: {diagnostic}")
            return

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

            findings = tuple(getattr(result.details, "findings", ()))
            if findings:
                severity_labels = {
                    "critical": "критический",
                    "major": "высокий",
                    "minor": "средний",
                    "trivial": "незначительный",
                    "info": "информация",
                }
                for index, finding in enumerate(findings, start=1):
                    severity = str(getattr(finding, "severity", "info"))
                    location = str(getattr(finding, "path", "не указан"))
                    line = getattr(finding, "line", None)
                    line_end = getattr(finding, "line_end", None)
                    if line:
                        location += (
                            f":{line}"
                            if line_end is None or line_end == line
                            else f":{line}-{line_end}"
                        )
                    finding_table = Table(
                        title=f"Замечание {index}: {severity_labels.get(severity, severity)}",
                        show_header=False,
                        box=None,
                        expand=True,
                    )
                    finding_table.add_column("Поле", style="bold", no_wrap=True)
                    finding_table.add_column("Значение", overflow="fold")
                    finding_table.add_row("Расположение", Text(location))
                    finding_table.add_row("Заголовок", Text(str(getattr(finding, "title", None) or "не указано")))
                    finding_table.add_row("Воздействие", Text(str(getattr(finding, "message", "не указано"))))
                    resolution = getattr(finding, "resolution", None)
                    finding_table.add_row("Рекомендация", Text(str(resolution or "не указано")))
                    codegen_instructions = getattr(finding, "codegen_instructions", None)
                    if codegen_instructions:
                        finding_table.add_row(
                            "Контекст исправления агента",
                            Text(str(codegen_instructions)),
                        )
                    suggestions = tuple(getattr(finding, "suggestions", ()))
                    if suggestions:
                        finding_table.add_row(
                            "Предложения",
                            Text("\n".join(str(item) for item in suggestions)),
                        )
                    console.print(finding_table)

            console.print(f"{'✓' if result.ok else '✗'} {result.message}")
        elif checks is not None:
            from rich.table import Table

            table = Table(title="Диагностика AzurPilot", expand=True)
            table.add_column("Проверка", no_wrap=True)
            table.add_column("Состояние", no_wrap=True)
            table.add_column("Результат", overflow="fold")
            for check in checks:
                state = status_label(check.status)
                marker = (
                    "✓"
                    if check.status is CapabilityStatus.READY
                    else "?"
                    if check.status in {
                        CapabilityStatus.NOT_CHECKED,
                        CapabilityStatus.UNKNOWN,
                    }
                    else "⚠"
                )
                table.add_row(check_label(check.name), f"{marker} {state}", check.message)
            console.print(table)
            console.print(
                f"{'✓' if result.ok else '✗'} {result.message}"
            )
        else:
            if isinstance(result.details, McpBridgeStatusDetails):
                from rich.table import Table

                details = result.details
                console.print(
                    f"Мост Windows MCP: {details.state}; {details.endpoint}; "
                    f"аутентификация клиента={details.caller_authentication}"
                )
                process = details.process
                console.print(
                    "Владение: "
                    + (
                        "подтверждено"
                        if process.ownership_confirmed
                        else "не подтверждено"
                    )
                    + f"; PID процесса управления={process.supervisor_pid or '—'}"
                    + f"; PID моста={process.process_pid or '—'}"
                )
                table = Table(title="Серверы MCP", expand=True)
                table.add_column("Маршрут", no_wrap=True)
                table.add_column("Сервер", no_wrap=True)
                table.add_column("Состояние", no_wrap=True)
                table.add_column("Ревизия", no_wrap=True)
                table.add_column("Причина", overflow="fold")
                for upstream in details.upstreams:
                    table.add_row(
                        upstream.route,
                        upstream.server_name,
                        upstream.status,
                        _short_sha(
                            upstream.identity.source_revision
                            if upstream.identity is not None
                            else None
                        ),
                        upstream.reason_code,
                    )
                console.print(table)
                console.print(f"{'✓' if result.ok else '✗'} {result.message}")
            elif isinstance(result.details, McpBridgeAcceptanceDetails):
                from rich.table import Table

                table = Table(title="Приёмка моста Windows MCP", expand=True)
                table.add_column("Режим", no_wrap=True)
                table.add_column("Сервер", no_wrap=True)
                table.add_column("Протокол", no_wrap=True)
                table.add_column("Состояние", no_wrap=True)
                table.add_column("Вызовы только для чтения", overflow="fold")
                table.add_column("Причина", overflow="fold")
                for mode, routes in (
                    ("Совместимость", result.details.routes),
                    ("Современный (auto)", result.details.modern_routes),
                ):
                    for route in routes:
                        table.add_row(
                            mode,
                            route.server_name or "не определён",
                            route.protocol_version or "не определён",
                            route.acceptance_state,
                            ", ".join(route.called_tools) or "—",
                            route.reason_code,
                        )
                console.print(table)
                console.print(f"{'✓' if result.ok else '✗'} {result.message}")
            if isinstance(result.details, McpImpactDetails):
                from rich.table import Table

                details = result.details
                console.print(
                    f"Влияние MCP: {details.status} "
                    f"(base={details.base_sha}, head={details.head_sha})"
                )
                table = Table(title="Кандидатные пути → наборы исходников MCP", expand=True)
                table.add_column("Путь", overflow="fold")
                table.add_column("Наборы исходников", overflow="fold")
                table.add_column("Затронутые серверы", overflow="fold")
                for item in details.path_impacts:
                    table.add_row(
                        item.path,
                        ", ".join(item.source_sets) or "—",
                        ", ".join(item.affected_servers) or "—",
                    )
                console.print(table)
                if details.generated_artifacts:
                    console.print(
                        "Сгенерированные артефакты: "
                        + ", ".join(details.generated_artifacts)
                    )
                console.print(f"{'✓' if result.ok else '✗'} {result.message}")
            elif isinstance(
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
                table.add_column("Сервер", no_wrap=True)
                table.add_column("Версия", no_wrap=True)
                table.add_column("Состояние", no_wrap=True)
                table.add_column("Транспорт", overflow="fold")
                table.add_column("Ревизия", no_wrap=True)
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
                    ("source_state", "Исходники"),
                    ("runtime_state", "Среда выполнения"),
                    ("source_reconciled", "Исходники согласованы"),
                    ("runtime_ready", "Среда выполнения готова"),
                    ("plugin_state", "Плагин"),
                    ("plugin_source_state", "Исходники плагина"),
                    ("session_state", "Сессия"),
                ):
                    value = getattr(result.details, field, None)
                    if value is not None:
                        if isinstance(value, bool):
                            value = "да" if value else "нет"
                        console.print(f"{label}: {value}")
                console.print(f"{'✓' if result.ok else '✗'} {result.message}")
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


def _with_legacy_lifecycle_warning(
    result: ToolingResult,
    command: str,
    canonical: str,
) -> ToolingResult:
    warning = ToolingWarning(
        code=WarningCode.TOOLING_LEGACY_COMPATIBILITY,
        message=f"{command} устарела; используйте {canonical}.",
    )
    return result.model_copy(update={"warnings": (*result.warnings, warning)})


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
    if command == "bot":
        service = services.bot_runtime
        if args.bot_command == "status":
            return service.status(root)
        if args.bot_command == "start":
            return service.start(root, timeout_seconds=args.timeout)
        if args.bot_command == "stop":
            return service.stop(root, timeout_seconds=args.timeout)
    if command == "webui":
        if args.webui_command == "status":
            return services.lifecycle.inspect(root)
        if args.webui_command == "start":
            return services.lifecycle.start(
                root,
                timeout_seconds=args.timeout,
                open_browser=getattr(args, "browser", False),
                foreground=args.foreground,
            )
        if args.webui_command == "stop":
            return services.lifecycle.stop(root, timeout_seconds=args.timeout)
    if command == "start":
        result = services.lifecycle.start(
            root,
            timeout_seconds=args.timeout,
            open_browser=getattr(args, "browser", False),
            foreground=args.foreground,
        )
        return _with_legacy_lifecycle_warning(result, "azur start", "azur webui start")
    if command == "stop":
        result = services.lifecycle.stop(root, timeout_seconds=args.timeout)
        return _with_legacy_lifecycle_warning(result, "azur stop", "azur webui stop")
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
    if command == "deploy" and args.deploy_command == "docker":
        return services.docker.deploy(
            root,
            image=args.image,
            container=args.container,
            port=args.port,
            source=args.source,
            replace=args.replace,
            timeout_seconds=args.timeout,
            readiness_timeout_seconds=args.readiness_timeout,
        )
    if command == "mcp":
        if args.mcp_command == "bridge":
            if args.mcp_bridge_command == "configure":
                if sys.stdin.isatty():
                    raise CliInvocationError(
                        "Передайте токен клиента через канал stdin с --stdin-token; не указывайте его в argv."
                    )
                token = sys.stdin.readline(4098).removesuffix("\n").removesuffix("\r")
                if len(token.encode("utf-8")) > 4096 or sys.stdin.readline(1):
                    raise CliInvocationError(
                        "stdin должен содержать ровно одну ограниченную строку токена вызывающего клиента."
                    )
                return services.mcp.bridge(
                    args.mcp_bridge_command,
                    root,
                    caller_token=token,
                )
            return services.mcp.bridge(args.mcp_bridge_command, root)
        if args.mcp_command == "impact":
            return services.mcp.impact(root, base_commit=args.base)
        if args.mcp_command == "sync":
            return services.mcp.sync(root, base_commit=args.base)
        if args.mcp_command == "status":
            return services.mcp.status(root)
        if args.mcp_command == "versions":
            return services.mcp.versions(root)
        if args.mcp_command == "accept":
            return services.mcp.accept(root)
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
    if command == "dsh":
        if args.dsh_command == "prepare":
            return services.dsh.prepare(
                root,
                profile=args.profile,
                dsh_package=args.package,
            )
        if args.dsh_command == "launch":
            return services.dsh.launch(
                root,
                profile=args.profile,
                dsh_package=args.package,
                dsh_arguments=tuple(args.dsh_arguments),
                progress_stream=progress_stream,
            )
        if args.dsh_command == "verify":
            if args.generation:
                return services.dsh.verify_generation(
                    load_generation(args.generation), root
                )
            return services.dsh.verify(root)
    if command == "app" and args.app_command == "state":
        return services.application_state.read(args.state_id, args.profile)
    if command == "integrations":
        target = args.integration_target
        if target == "status":
            return services.integrations.status(root)
        if target == "doctor":
            return services.integrations.doctor(root)
        if target == "shared-mcp":
            return services.integrations.shared_mcp(
                args.integration_shared_mcp_action, root
            )
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
                    "Сканирование Semgrep требует --staged, --changed или --paths."
                )
            if scope_count > 1:
                raise CliInvocationError(
                    "Сканирование Semgrep принимает только одну область изменений: --staged, --changed или --paths."
                )
            if args.scan_base and not args.changed:
                raise CliInvocationError("--base разрешён только вместе с --changed.")
            if args.changed:
                if not args.scan_base:
                    raise CliInvocationError("--changed требует --base с точным SHA.")
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
    """Выполнить CLI и вернуть код завершения вместо немедленного вызова `sys.exit`."""

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
    except Exception as error:  # noqa: BLE001 - CLI обязан вернуть ограниченный конверт результата ошибки
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
        )
    return int(exit_code_for(result.code, result.ok))


__all__ = ["ServiceContainer", "build_parser", "main"]
