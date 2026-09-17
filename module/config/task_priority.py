"""Модуль разбора приоритетов задач.

Предоставляет функцию parse_task_priority для разбора текстовой настройки приоритетов задач пользователя,
поддерживает нормализацию полноширинных разделителей Unicode, удаление комментариев и дедупликацию.
"""

import re
from typing import Any, Iterable

from module.config.deep import deep_get, deep_iter

PRIORITY_SEPARATOR = "\n> "


def parse_task_priority(value: Any) -> list[str]:
    """Разобрать текст приоритетов задач и вернуть список имён задач без дубликатов."""
    if not value:
        return []

    text = str(value)
    text = re.sub(r"[＞﹥›˃ᐳ❯]", ">", text)
    tasks = []
    seen = set()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        for raw_task in line.split(">"):
            task = raw_task.strip()
            if not task or task in seen:
                continue
            seen.add(task)
            tasks.append(task)
    return tasks


def format_task_priority(tasks: Iterable[str]) -> str:
    """Отформатировать список имён задач в строку приоритетов для файла конфигурации."""
    return PRIORITY_SEPARATOR.join(str(task).strip() for task in tasks if str(task).strip())


def normalize_task_priority(value: Any) -> str:
    """Нормализовать текст приоритетов, удалив комментарии, пустые строки и повторяющиеся задачи."""
    return format_task_priority(parse_task_priority(value))


def get_scheduler_tasks(args: dict[str, Any]) -> list[str]:
    """Извлечь из структуры args.json задачи, фактически участвующие в планировании."""
    tasks = []
    for path, data in deep_iter(args, depth=3):
        if path[-2:] != ["Scheduler", "Command"]:
            continue
        if not isinstance(data, dict):
            continue
        command = data.get("value")
        if isinstance(command, str) and command and command not in tasks:
            tasks.append(command)
    return tasks


def _insert_by_default_neighbors(ordered: list[str], task: str, default_order: list[str]) -> None:
    if task in ordered:
        return

    try:
        default_index = default_order.index(task)
    except ValueError:
        ordered.append(task)
        return

    prev_task = None
    for candidate in reversed(default_order[:default_index]):
        if candidate in ordered:
            prev_task = candidate
            break

    next_task = None
    for candidate in default_order[default_index + 1:]:
        if candidate in ordered:
            next_task = candidate
            break

    if prev_task is not None:
        ordered.insert(ordered.index(prev_task) + 1, task)
    elif next_task is not None:
        ordered.insert(ordered.index(next_task), task)
    else:
        ordered.append(task)


def merge_task_priority(
        current: Any,
        default: Any,
        available_tasks: Iterable[str] | None = None,
) -> str:
    """Объединить пользовательские приоритеты с приоритетами по умолчанию, дополнив новыми задачами согласно порядку по умолчанию."""
    default_order = parse_task_priority(default)
    available = list(available_tasks or default_order)
    available_set = set(available)

    current_order = [
        task
        for task in parse_task_priority(current)
        if task in available_set
    ]
    ordered = list(dict.fromkeys(current_order))

    default_available = [task for task in default_order if task in available_set]
    for task in default_available:
        if task not in ordered:
            _insert_by_default_neighbors(ordered, task, default_available)

    for task in available:
        if task not in ordered:
            _insert_by_default_neighbors(ordered, task, default_available)

    return format_task_priority(ordered)


def task_priority_from_config(config: dict[str, Any], args: dict[str, Any]) -> str:
    """Получить сохраняемую и отображаемую строку приоритетов задач на основе конфигурации и шаблона args."""
    current = deep_get(config, "General.YukikazeTaskManager.TaskPriorityAdjustment")
    default = deep_get(args, "General.YukikazeTaskManager.TaskPriorityAdjustment.value")
    return merge_task_priority(current, default, get_scheduler_tasks(args))
