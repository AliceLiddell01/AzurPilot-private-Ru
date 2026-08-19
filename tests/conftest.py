from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import IO

import pytest

_PARALLEL_ENV = "AZURPILOT_PYTEST_PARALLEL"
_MAX_SHARDS_ENV = "AZURPILOT_PYTEST_MAX_SHARDS"
_CHILD_ENV = "AZURPILOT_PYTEST_PARALLEL_CHILD"
_MISSING = object()
_PIL_BEFORE: dict[str, tuple[object, object]] = {}


def _parallel_requested() -> bool:
    explicit = os.environ.get(_PARALLEL_ENV)
    if explicit is not None:
        return explicit.strip().lower() not in {"", "0", "false", "no", "off"}
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def _parallel_shard_count(file_count: int) -> int:
    cpu_count = max(1, os.cpu_count() or 1)
    raw_limit = os.environ.get(_MAX_SHARDS_ENV, "4")
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise pytest.UsageError(f"{_MAX_SHARDS_ENV} должен быть целым числом") from exc
    if limit < 1:
        raise pytest.UsageError(f"{_MAX_SHARDS_ENV} должен быть больше нуля")
    return min(cpu_count, limit, file_count)


def _test_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("test_*.py") if path.is_file()))


def _file_weight(path: Path) -> tuple[int, int]:
    content = path.read_bytes()
    test_count = content.count(b"def test_") + content.count(b"async def test_")
    return max(test_count, 1), len(content)


def _stable_tiebreak(path: Path) -> int:
    digest = hashlib.blake2b(path.as_posix().encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big")


def _split_test_files(files: tuple[Path, ...], count: int) -> tuple[tuple[Path, ...], ...]:
    weighted = sorted(
        ((path, *_file_weight(path)) for path in files),
        key=lambda item: (-item[1], -item[2], _stable_tiebreak(item[0])),
    )
    shards: list[list[Path]] = [[] for _ in range(count)]
    scores = [0] * count
    sizes = [0] * count

    for path, tests, size in weighted:
        index = min(range(count), key=lambda item: (scores[item], sizes[item], item))
        shards[index].append(path)
        scores[index] += tests
        sizes[index] += size

    return tuple(tuple(sorted(shard)) for shard in shards)


def _parallel_invocation_args(config: pytest.Config) -> tuple[list[str], Path] | None:
    args = [str(arg) for arg in config.invocation_params.args]
    target_indexes = [index for index, arg in enumerate(args) if arg == "tests"]
    if len(target_indexes) != 1:
        return None
    if any("::" in arg for arg in args):
        return None

    target_index = target_indexes[0]
    test_root = Path(args[target_index])
    base_args = args[:target_index] + args[target_index + 1 :]
    return base_args, test_root


def _is_webui_fake_pil(module: object) -> bool:
    if module is _MISSING or module is None:
        return False
    if getattr(module, "__file__", None) is not None:
        return False
    image_module = getattr(module, "Image", None)
    image_type = getattr(image_module, "Image", None)
    return getattr(image_type, "__name__", "") == "MockPILImage"


def _restore_module(name: str, previous: object) -> None:
    if previous is _MISSING:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = previous


def _stop_parallel_processes(
    processes: list[tuple[subprocess.Popen, Path, IO[str], int]],
) -> None:
    """Остановить и дождаться уже запущенных pytest-процессов после сбоя."""

    for process, _, _, _ in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    for process, _, _, _ in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                continue
            try:
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass


def pytest_cmdline_main(config: pytest.Config) -> int | None:
    if os.environ.get(_CHILD_ENV) == "1" or not _parallel_requested():
        return None

    invocation = _parallel_invocation_args(config)
    if invocation is None:
        return None
    base_args, test_root = invocation
    files = _test_files(test_root)
    if not files:
        return None

    shard_count = _parallel_shard_count(len(files))
    if shard_count <= 1:
        return None
    shards = _split_test_files(files, shard_count)

    child_args = list(base_args)
    if not any(arg.startswith("--durations") for arg in child_args):
        child_args.append("--durations=10")

    print(
        "AzurPilot pytest: параллельный запуск по test-файлам, "
        f"shard-ов: {shard_count}, файлов: {len(files)}"
    )
    processes: list[tuple[subprocess.Popen, Path, IO[str], int]] = []
    log_handles: list[IO[str]] = []
    exit_codes: list[int] = []
    with tempfile.TemporaryDirectory(prefix="azurpilot-pytest-") as temp_dir:
        temp_root = Path(temp_dir)
        try:
            for index, shard in enumerate(shards):
                log_path = temp_root / f"shard-{index}.log"
                log_handle = log_path.open("w", encoding="utf-8")
                log_handles.append(log_handle)
                env = os.environ.copy()
                env[_CHILD_ENV] = "1"
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        *child_args,
                        *(path.as_posix() for path in shard),
                    ],
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                processes.append((process, log_path, log_handle, len(shard)))

            for process, _, _, _ in processes:
                exit_codes.append(process.wait())
        except BaseException:
            _stop_parallel_processes(processes)
            raise
        finally:
            for log_handle in log_handles:
                log_handle.close()

        for index, ((_, log_path, _, file_count), exit_code) in enumerate(
            zip(processes, exit_codes, strict=True)
        ):
            print(
                f"\n===== pytest shard {index + 1}/{shard_count}, "
                f"files={file_count}, exit={exit_code} ====="
            )
            print(log_path.read_text(encoding="utf-8"), end="")

    failures = [code for code in exit_codes if code != int(pytest.ExitCode.OK)]
    if failures:
        return int(pytest.ExitCode.TESTS_FAILED)
    return int(pytest.ExitCode.OK)


def pytest_collectstart(collector: pytest.Collector) -> None:
    if isinstance(collector, pytest.Module):
        _PIL_BEFORE[collector.nodeid] = (
            sys.modules.get("PIL", _MISSING),
            sys.modules.get("PIL.Image", _MISSING),
        )


def pytest_collectreport(report: pytest.CollectReport) -> None:
    previous = _PIL_BEFORE.pop(report.nodeid, None)
    if previous is None:
        return

    # WebUI намеренно подменяет PIL перед импортом PyWebIO. В production это
    # process-global оптимизация, но между test-модулями состояние протекать не должно.
    current_pil = sys.modules.get("PIL", _MISSING)
    if not _is_webui_fake_pil(current_pil):
        return

    previous_pil, previous_image = previous
    _restore_module("PIL", previous_pil)
    _restore_module("PIL.Image", previous_image)
