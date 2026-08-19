"""Запуск CI-команд с heartbeat и сохранением полного текстового лога."""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence, TextIO


def normalize_exit_code(return_code: int) -> int:
    """Нормализовать завершение по сигналу к shell-совместимому коду."""

    if return_code < 0:
        return 128 + abs(return_code)
    return return_code


def run_command(
    command: Sequence[str],
    *,
    label: str,
    heartbeat_seconds: float,
    log_file: Path,
    output: TextIO = sys.stdout,
) -> int:
    """Запустить команду, транслировать вывод и сообщать о длительной тишине."""

    if not command:
        raise ValueError("Команда для запуска не задана.")
    if not label.strip():
        raise ValueError("Название CI-операции не задано.")
    if heartbeat_seconds < 0:
        raise ValueError("Интервал heartbeat не может быть отрицательным.")

    log_file.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    last_output_at = started_at
    last_heartbeat_at = started_at
    timestamp_lock = threading.Lock()
    stream_lock = threading.Lock()

    with log_file.open("w", encoding="utf-8", errors="replace", newline="") as log_handle:

        def emit(text: str) -> None:
            with stream_lock:
                output.write(text)
                if not text.endswith("\n"):
                    output.write("\n")
                output.flush()
                log_handle.write(text)
                if not text.endswith("\n"):
                    log_handle.write("\n")
                log_handle.flush()

        emit(f"[ci] {label}: запуск.")

        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        if process.stdout is None:
            process.terminate()
            raise RuntimeError("Не удалось получить stdout дочернего процесса.")

        def forward_output() -> None:
            nonlocal last_output_at
            try:
                for line in process.stdout:
                    with timestamp_lock:
                        last_output_at = time.monotonic()
                    emit(line)
            except (OSError, ValueError):
                return

        reader = threading.Thread(
            target=forward_output,
            name="ci-output-forwarder",
            daemon=True,
        )
        reader.start()

        poll_interval = 0.1
        if heartbeat_seconds > 0:
            poll_interval = min(1.0, max(heartbeat_seconds / 4.0, 0.05))

        try:
            while True:
                try:
                    return_code = process.wait(timeout=poll_interval)
                    break
                except subprocess.TimeoutExpired:
                    if heartbeat_seconds == 0:
                        continue

                    now = time.monotonic()
                    with timestamp_lock:
                        silent_for = now - last_output_at
                        since_heartbeat = now - last_heartbeat_at

                    if silent_for < heartbeat_seconds or since_heartbeat < heartbeat_seconds:
                        continue

                    elapsed = int(now - started_at)
                    silent = int(silent_for)
                    emit(
                        f"[heartbeat] {label}: процесс работает {elapsed} с; "
                        f"новых строк нет {silent} с."
                    )
                    with timestamp_lock:
                        last_heartbeat_at = now
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            reader.join(timeout=5.0)
            process.stdout.close()

        normalized_exit_code = normalize_exit_code(return_code)
        elapsed = int(time.monotonic() - started_at)
        emit(
            f"[ci] {label}: завершено за {elapsed} с, "
            f"код выхода {normalized_exit_code}."
        )
        return normalized_exit_code


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Запуск CI-команды с heartbeat и сохранением текстового лога.",
    )
    parser.add_argument("--label", required=True, help="Название текущей CI-операции.")
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=60.0,
        help="Интервал тишины перед heartbeat. Ноль отключает heartbeat.",
    )
    parser.add_argument("--log-file", type=Path, required=True, help="Путь к полному логу.")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Команда после разделителя --.")
    args = parser.parse_args(argv)

    if args.command and args.command[0] == "--":
        args.command = args.command[1:]

    if not args.command:
        parser.error("После -- необходимо указать команду.")

    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return run_command(
        args.command,
        label=args.label,
        heartbeat_seconds=args.heartbeat_seconds,
        log_file=args.log_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
