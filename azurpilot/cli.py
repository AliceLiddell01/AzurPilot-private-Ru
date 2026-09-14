"""Публичный `azur` CLI: argparse parser и presentation adapter."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO

from pydantic import BaseModel

from .tooling.bootstrap import BuildService
from .tooling.contracts import (
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
    """Ошибка argv без побочного вывода argparse в machine mode."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliInvocationError(message)


@dataclass
class ServiceContainer:
    """Injectable service boundary для CLI tests и будущих transport adapters."""

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
        help="явно указать validated repository root",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="вывести один machine-readable JSON report",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="отключить ANSI и Rich colors",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="показать operation id и дополнительные детали",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="синоним --verbose для operator diagnostics",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="azur",
        description="Безопасное cross-platform управление checkout AzurPilot.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    doctor = subparsers.add_parser(
        "doctor", help="read-only проверка project-bound capabilities"
    )
    _add_common_options(doctor, suppress_defaults=True)

    start = subparsers.add_parser(
        "start", help="запустить WebUI после ownership/readiness preflight"
    )
    _add_common_options(start, suppress_defaults=True)
    start.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="общий readiness deadline",
    )
    start.add_argument(
        "--browser",
        action="store_true",
        help="открыть WebUI после подтверждённого readiness",
    )
    start.add_argument(
        "--foreground", action="store_true", help="удерживать CLI до остановки backend"
    )

    stop = subparsers.add_parser(
        "stop", help="остановить только exact owned WebUI process tree"
    )
    _add_common_options(stop, suppress_defaults=True)
    stop.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="deadline остановки",
    )

    build = subparsers.add_parser(
        "build", help="подготовить Python environment без Git update"
    )
    _add_common_options(build, suppress_defaults=True)
    build.add_argument(
        "--timeout",
        type=float,
        default=30 * 60,
        metavar="SECONDS",
        help="общий bootstrap deadline",
    )
    build.add_argument(
        "--shortcut", action="store_true", help="запросить optional shortcut capability"
    )

    repair = subparsers.add_parser(
        "repair", help="диагностировать и транзакционно восстановить environment"
    )
    _add_common_options(repair, suppress_defaults=True)
    repair.add_argument(
        "--diagnostic-only", action="store_true", help="не выполнять mutation"
    )
    repair.add_argument(
        "--timeout",
        type=float,
        default=30 * 60,
        metavar="SECONDS",
        help="общий repair deadline",
    )

    update = subparsers.add_parser(
        "update", help="выполнить только проверенный fast-forward из upstream"
    )
    _add_common_options(update, suppress_defaults=True)
    update.add_argument(
        "--expected-branch", default=None, help="ожидаемая рабочая ветка"
    )
    update.add_argument("--remote", default=None, help="имя Git remote")
    update.add_argument("--remote-branch", default=None, help="имя remote branch")
    update.add_argument(
        "--timeout",
        type=float,
        default=30 * 60,
        metavar="SECONDS",
        help="общий update deadline",
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


def _unexpected_result() -> ToolingResult[BaseModel, BaseModel]:
    return ToolingResult[BaseModel, BaseModel](
        ok=False,
        code=ResultCode.TOOLING_UNEXPECTED,
        state=OperationState.UNKNOWN,
        message="Операция завершилась непредвиденной ошибкой; postcondition не подтверждён.",
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
    try:
        from rich.console import Console

        is_tty = bool(getattr(stdout, "isatty", lambda: False)())
        console = Console(
            file=stdout,
            no_color=no_color or not is_tty or bool(os.environ.get("NO_COLOR")),
            force_terminal=False,
        )
        style = "green" if result.ok else "red"
        console.print(
            f"[{style}]{'OK' if result.ok else 'ERROR'}[/{style}] {result.message} [{result.code.value}]"
        )
        if result.operation_id and verbose:
            console.print(f"operation_id: {result.operation_id}")
        for warning in result.warnings:
            console.print(
                f"[yellow]WARN[/yellow] {warning.message} [{warning.code.value}]"
            )
    except ImportError, OSError, RuntimeError, TypeError, ValueError:
        stream = stdout if result.ok else stderr
        stream.write(
            f"{'OK' if result.ok else 'ERROR'}: {result.message} [{result.code.value}]\n"
        )
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
            open_browser=args.browser,
            foreground=args.foreground,
        )
    if command == "stop":
        return services.lifecycle.stop(root, timeout_seconds=args.timeout)
    if command == "build":
        return services.build.build(
            root, timeout_seconds=args.timeout, create_shortcut=args.shortcut
        )
    if command == "repair":
        return services.repair.repair(
            root, diagnostic_only=args.diagnostic_only, timeout_seconds=args.timeout
        )
    if command == "update":
        return services.update.update(
            root,
            expected_branch=args.expected_branch,
            remote_name=args.remote,
            remote_branch=args.remote_branch,
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
            stderr.write(f"Ошибка invocation: {error}\n")
        return int(exit_code_for(result.code))

    json_mode = bool(getattr(args, "json", False))
    verbose = bool(getattr(args, "verbose", False) or getattr(args, "debug", False))
    try:
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
    except Exception:  # noqa: BLE001 - CLI обязан вернуть bounded unexpected envelope
        result = _unexpected_result()

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
