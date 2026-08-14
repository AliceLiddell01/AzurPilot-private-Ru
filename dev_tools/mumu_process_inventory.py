"""Read-only диагностика процессов MuMu для безопасного Stage 2 recovery.

Скрипт не завершает процессы, не запускает и не останавливает эмулятор,
не меняет конфигурацию AzurPilot и не выполняет ADB-команды. Он только
разрешает выбранный MuMu instance по уже сохранённому serial и собирает
процессный inventory Windows для последующей классификации ownership.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import psutil


MUMU_PROCESS_HINT = re.compile(r"(?i)(mumu|nemu|muvm)")


@dataclass
class ProcessRow:
    pid: int
    ppid: int
    name: str
    executable: str
    command_line: str
    relationship: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собрать read-only process inventory выбранного MuMu instance.",
    )
    parser.add_argument(
        "--repository",
        default=r"C:\AzurPilot",
        help="Путь к рабочему дереву AzurPilot.",
    )
    parser.add_argument(
        "--config",
        default="ap",
        help="Имя JSON-конфигурации без расширения.",
    )
    parser.add_argument(
        "--serial",
        default="",
        help="Явный ADB serial. Если не задан, читается Alas.Emulator.Serial из config/<name>.json.",
    )
    return parser.parse_args()


def load_config_serial(repository: Path, config_name: str) -> str:
    config_path = repository / "config" / f"{config_name}.json"
    if not config_path.is_file():
        raise RuntimeError(f"Конфигурация не найдена: {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8"))
    try:
        serial = data["Alas"]["Emulator"]["Serial"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"В {config_path} отсутствует Alas.Emulator.Serial."
        ) from exc

    serial = str(serial).strip()
    if not serial or serial == "auto":
        raise RuntimeError(
            "Alas.Emulator.Serial должен быть задан явно для process inventory; "
            "автовыбор при destructive recovery недопустим."
        )
    return serial


def mask_personal_path(value: str) -> str:
    if not value:
        return ""

    result = value
    home = str(Path.home())
    if home and home not in {".", os.path.sep}:
        result = result.replace(home, "%USERPROFILE%")
        result = result.replace(home.replace("\\", "/"), "%USERPROFILE%")
    return result


def command_line_of(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline())
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ""


def executable_of(proc: psutil.Process) -> str:
    try:
        return proc.exe()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ""


def name_of(proc: psutil.Process) -> str:
    try:
        return proc.name()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ""


def classify_relationship(
    *,
    name: str,
    executable: str,
    command_line: str,
    instance_name: str,
    instance_id: int | None,
) -> str:
    identity_searchable = f"{name} {executable} {command_line}"
    if instance_name and instance_name.casefold() in identity_searchable.casefold():
        return "selected-instance-token"

    if instance_id is not None:
        id_patterns = (
            rf"(?i)(?:^|\s)-v\s+{instance_id}(?:\s|$)",
            rf"(?i)(?:^|\s)--instance(?:=|\s+){instance_id}(?:\s|$)",
        )
        if any(re.search(pattern, command_line) for pattern in id_patterns):
            return "selected-instance-id-token"

    # Generic MuMu relevance is based only on the executable identity. Otherwise
    # a tool such as python.exe becomes a false positive merely because its script
    # argument contains the word "mumu".
    process_identity = f"{name} {executable}"
    if MUMU_PROCESS_HINT.search(process_identity):
        return "mumu-related-unclassified"

    return "unrelated"


def resolve_instance(repository: Path, serial: str):
    sys.path.insert(0, str(repository))

    from module.device.platform.emulator_windows import EmulatorManager
    from module.device.platform.platform_base import serial_to_id

    manager = EmulatorManager()
    instances = list(manager.all_emulator_instances)

    exact = [instance for instance in instances if instance.serial == serial]
    if len(exact) == 1:
        return exact[0], instances
    if len(exact) > 1:
        raise RuntimeError(
            f"Serial {serial} неоднозначно соответствует {len(exact)} instances."
        )

    instance_id = serial_to_id(serial)
    if instance_id is not None:
        by_id = [
            instance
            for instance in instances
            if getattr(instance, "MuMuPlayer12_id", None) == instance_id
        ]
        if len(by_id) == 1:
            by_id[0].serial = serial
            return by_id[0], instances
        if len(by_id) > 1:
            raise RuntimeError(
                f"MuMu instance id {instance_id} неоднозначно соответствует {len(by_id)} instances."
            )

    rendered = ", ".join(str(instance) for instance in instances)
    raise RuntimeError(
        f"Не удалось однозначно разрешить MuMu instance для serial {serial}. "
        f"Обнаружены: {rendered or 'нет'}"
    )


def collect_processes(instance) -> list[ProcessRow]:
    rows: list[ProcessRow] = []
    instance_id = getattr(instance, "MuMuPlayer12_id", None)

    for proc in psutil.process_iter():
        try:
            pid = proc.pid
            ppid = proc.ppid()
            name = name_of(proc)
            executable = executable_of(proc)
            command_line = command_line_of(proc)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue

        relationship = classify_relationship(
            name=name,
            executable=executable,
            command_line=command_line,
            instance_name=instance.name,
            instance_id=instance_id,
        )
        if relationship == "unrelated":
            continue

        rows.append(
            ProcessRow(
                pid=pid,
                ppid=ppid,
                name=name,
                executable=mask_personal_path(executable),
                command_line=mask_personal_path(command_line),
                relationship=relationship,
            )
        )

    rows.sort(key=lambda row: (row.relationship, row.name.casefold(), row.pid))
    return rows


def render_text(report: dict) -> str:
    lines = [
        "AzurPilot MuMu process inventory",
        f"Timestamp UTC: {report['timestamp_utc']}",
        f"Configured serial: {report['configured_serial']}",
        f"Detected emulator type: {report['instance']['type']}",
        f"Instance name: {report['instance']['name']}",
        f"Instance id: {report['instance']['id']}",
        f"Instance path: {report['instance']['path']}",
        "",
        "Processes:",
    ]

    if not report["processes"]:
        lines.append("  <MuMu-related processes not found>")
        return "\n".join(lines) + "\n"

    for row in report["processes"]:
        lines.extend(
            [
                f"  [{row['relationship']}]",
                f"    PID: {row['pid']}",
                f"    Parent PID: {row['ppid']}",
                f"    Name: {row['name']}",
                f"    Executable: {row['executable']}",
                f"    Command line: {row['command_line']}",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    repository = Path(args.repository).resolve()
    if not repository.is_dir():
        raise RuntimeError(f"Каталог репозитория не существует: {repository}")

    serial = args.serial.strip() or load_config_serial(repository, args.config)
    instance, all_instances = resolve_instance(repository, serial)

    if getattr(instance, "type", "") != "MuMuPlayer12":
        raise RuntimeError(
            f"Stage 2 inventory предназначен для современного MuMu family; получен {instance.type}."
        )
    if getattr(instance, "MuMuPlayer12_id", None) is None:
        raise RuntimeError(
            f"Не удалось получить instance id из имени {instance.name!r}; fail closed."
        )

    rows = collect_processes(instance)
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "configured_serial": serial,
        "instance": {
            "type": instance.type,
            "name": instance.name,
            "id": instance.MuMuPlayer12_id,
            "path": mask_personal_path(instance.path),
        },
        "discovered_instances": [
            {
                "serial": item.serial,
                "type": item.type,
                "name": item.name,
                "id": getattr(item, "MuMuPlayer12_id", None),
                "path": mask_personal_path(item.path),
            }
            for item in all_instances
        ],
        "processes": [asdict(row) for row in rows],
    }

    output_root = Path(tempfile.gettempdir()) / "AzurPilot-MuMu-Inventory"
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = output_root / f"mumu-process-inventory-{stamp}.json"
    text_path = output_root / f"mumu-process-inventory-{stamp}.txt"

    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    text_path.write_text(render_text(report), encoding="utf-8")

    print(render_text(report), end="")
    print(f"JSON: {json_path}")
    print(f"TXT: {text_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
