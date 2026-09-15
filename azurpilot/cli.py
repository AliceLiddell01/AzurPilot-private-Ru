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

from .tooling.bootstrap import BuildService
from .tooling.contracts import (
    CapabilityStatus,
    OperationState,
    ResultCode,
    ToolingResult,
    exit_code_for,
)
from .tooling.doctor import DoctorService
from .tooling.errors import ToolingError
from .tooling.lifecycle import LifecycleService
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

    @classmethod
    def create(cls) -> ServiceContainer:
        return cls(
            doctor=DoctorService(),
            lifecycle=LifecycleService(),
            build=BuildService(),
            repair=RepairService(),
            update=UpdateService(),
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
        }.get(name, name)

    try:
        from rich.console import Console

        is_tty = bool(getattr(stream, "isatty", lambda: False)())
        console = Console(
            file=stream,
            no_color=no_color or not is_tty or bool(os.environ.get("NO_COLOR")),
            force_terminal=False,
            highlight=False,
        )

        checks = getattr(result.details, "checks", None)
        if checks is not None:
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
    args: argparse.Namespace, services: ServiceContainer
) -> ToolingResult[BaseModel, BaseModel]:
    root = getattr(args, "repository_root", None)
    command = args.command
    if command == "doctor":
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
                result = _dispatch(args, services or ServiceContainer.create())
        else:
            result = _dispatch(args, services or ServiceContainer.create())
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
        )
    return int(exit_code_for(result.code, result.ok))


__all__ = ["ServiceContainer", "build_parser", "main"]
