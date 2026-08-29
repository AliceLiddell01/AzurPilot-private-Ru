"""Тонкая точка входа для проверки Dev Runtime Foundation."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

with redirect_stdout(sys.stderr):
    from module.dev_runtime import DevResult, DevSessionManager, DevStatusKind


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Локальная диагностика AzurPilot Dev Runtime"
    )
    parser.add_argument(
        "command",
        choices=(
            "preflight",
            "doctor",
            "status",
            "smoke",
            "cleanup",
            "list",
            "plan",
            "task-smoke",
        ),
        help="Диагностика, план задач или проверка жизненного цикла фиксированной dev-сессии профиля ap",
    )
    parser.add_argument(
        "--task",
        dest="tasks",
        action="append",
        default=[],
        help="Корневая задача; параметр можно указывать несколько раз в task-aware командах",
    )
    parser.add_argument(
        "--exclude-task",
        dest="excluded_tasks",
        action="append",
        default=[],
        help="Исключаемая задача; параметр можно указывать несколько раз",
    )
    parser.add_argument(
        "--preserve-task-state",
        action="store_true",
        help="Явно сохранить scheduler-state после проверки task-aware режима",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    task_commands = {"plan", "task-smoke"}
    if args.command not in task_commands and (
        args.tasks or args.excluded_tasks or args.preserve_task_state
    ):
        result = DevResult(
            ok=False,
            code="DEV_CLI_ARGUMENTS_INVALID",
            message=f"Команда {args.command} не принимает task-параметры",
            state=DevStatusKind.FAILED.value,
        )
    else:
        with redirect_stdout(sys.stderr):
            manager = DevSessionManager()
            try:
                if args.command == "list":
                    result = manager.list_tasks()
                elif args.command == "plan":
                    result = manager.plan(
                        root_tasks=args.tasks,
                        excluded_tasks=args.excluded_tasks,
                    )
                elif args.command == "task-smoke":
                    result = manager.task_smoke(
                        root_tasks=args.tasks,
                        excluded_tasks=args.excluded_tasks,
                        preserve_task_state=args.preserve_task_state,
                    )
                elif args.command == "cleanup":
                    result = manager.cleanup()
                else:
                    result = getattr(manager, args.command)()
            except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                result = DevResult(
                    ok=False,
                    code="DEV_CLI_FAILED",
                    message=f"Операция Dev Runtime завершилась исключением {type(exc).__name__}",
                    state=DevStatusKind.FAILED.value,
                )

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
