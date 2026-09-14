"""Сравнить loopback OCR transport zerorpc и pyzmq на synthetic workload."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import multiprocessing
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import psutil

from module.ocr.rpc_security import (
    client_uri,
    decode_image_payload,
    encode_image_payload,
    loopback_bind_uri,
)

DEFAULT_RUNS = 7
DEFAULT_ITERATIONS = 25
DEFAULT_STARTUP_RUNS = 3
DEFAULT_BATCH_SIZE = 4
SERVER_TIMEOUT_SECONDS = 5.0
IMAGE_SHAPE = (360, 640, 3)


class BenchmarkError(RuntimeError):
    """Benchmark не смог получить bounded transport evidence."""


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _require_language(lang: str) -> None:
    if lang != "azur_lane":
        raise ValueError(f"Неподдерживаемая synthetic OCR model: {lang!r}")


def _decode_synthetic_image(payload: bytes) -> int:
    return int(decode_image_payload(payload).size)


class _LegacySyntheticService:
    """Минимальный zerorpc endpoint без загрузки OCR-моделей."""

    def hello(self):
        return "hello"

    def ocr(self, lang: str, payload: bytes):
        _require_language(lang)
        return _decode_synthetic_image(payload)

    def ocr_for_single_lines(self, lang: str, payloads: list[bytes]):
        _require_language(lang)
        return [_decode_synthetic_image(payload) for payload in payloads]


def _legacy_server_entry(port: int, ready_event: Any) -> None:
    import zerorpc

    server = zerorpc.Server(_LegacySyntheticService())
    server.bind(loopback_bind_uri(port))
    ready_event.set()
    server.run()


class _NewSyntheticModel:
    """Synthetic model for pyzmq that measures no OCR inference time."""

    def ocr(self, image: np.ndarray):
        return int(image.size)

    def ocr_for_single_lines(self, images: list[np.ndarray]):
        return [int(image.size) for image in images]


def _new_server_entry(port: int, ready_event: Any, stop_event: Any) -> None:
    from types import SimpleNamespace

    from module.ocr.rpc import _OcrRpcServer, _OcrRpcService

    service = _OcrRpcService(SimpleNamespace(azur_lane=_NewSyntheticModel()))
    server = _OcrRpcServer(port, service)
    server.run(stop_event=stop_event, ready_event=ready_event)


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    pid: int
    created_at: float
    executable: str
    command: tuple[str, ...]


def _identity(process: psutil.Process) -> _ProcessIdentity:
    return _ProcessIdentity(
        pid=process.pid,
        created_at=float(process.create_time()),
        executable=str(Path(process.exe()).absolute()),
        command=tuple(str(item) for item in process.cmdline()),
    )


def _identity_matches(process: psutil.Process, expected: _ProcessIdentity) -> bool:
    try:
        actual = _identity(process)
    except OSError, psutil.Error, TypeError, ValueError:
        return False
    return (
        actual.pid == expected.pid
        and abs(actual.created_at - expected.created_at) < 0.01
        and actual.executable.casefold() == expected.executable.casefold()
        and actual.command == expected.command
    )


def _is_running(process: psutil.Process) -> bool:
    try:
        return process.is_running()
    except OSError, psutil.Error:
        return False


def _terminate_owned_tree(
    process: multiprocessing.Process,
    expected_root: _ProcessIdentity,
) -> None:
    """Остановить только exact benchmark process и уже найденных descendants."""

    try:
        root = psutil.Process(process.pid)
    except psutil.NoSuchProcess:
        process.join(timeout=0)
        return
    if not _identity_matches(root, expected_root):
        raise BenchmarkError(
            "Benchmark process identity изменилась; cleanup остановлен fail-closed."
        )

    try:
        descendants = root.children(recursive=True)
    except (OSError, psutil.Error) as exc:
        raise BenchmarkError(
            "Нельзя перечислить exact descendants benchmark process."
        ) from exc
    targets = []
    for candidate in [*descendants, root]:
        try:
            if not candidate.is_running():
                continue
            targets.append((candidate, _identity(candidate)))
        except OSError, psutil.Error:
            # Процесс мог завершиться между enumeration и снятием identity.
            continue
    for candidate, expected in reversed(targets):
        if _identity_matches(candidate, expected):
            candidate.terminate()
    deadline = time.monotonic() + SERVER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if all(not _is_running(candidate) for candidate, _expected in targets):
            break
        time.sleep(0.05)
    for candidate, expected in reversed(targets):
        try:
            if _is_running(candidate) and _identity_matches(candidate, expected):
                candidate.kill()
        except psutil.NoSuchProcess:
            continue
    process.join(timeout=SERVER_TIMEOUT_SECONDS)
    if process.is_alive() or any(
        _is_running(candidate) for candidate, _expected in targets
    ):
        raise BenchmarkError("Benchmark process tree не завершилось в заданный срок.")


@dataclass(slots=True)
class _ServerHandle:
    process: multiprocessing.Process
    root_identity: _ProcessIdentity
    stop_event: Any | None

    def close(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
            self.process.join(timeout=SERVER_TIMEOUT_SECONDS)
        if self.process.is_alive():
            _terminate_owned_tree(self.process, self.root_identity)
        elif self.process.exitcode is None:
            self.process.join(timeout=0)
        if self.process.is_alive():
            raise BenchmarkError("Benchmark server process остался запущен.")


def _wait_ready(
    process: multiprocessing.Process,
    ready_event: Any,
) -> None:
    deadline = time.monotonic() + SERVER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if ready_event.wait(timeout=0.05):
            return
        if not process.is_alive():
            raise BenchmarkError(
                f"Benchmark server завершился до readiness, exitcode={process.exitcode}."
            )
    raise BenchmarkError("Benchmark server не сообщил readiness в заданный срок.")


def _start_server(transport: str) -> tuple[_ServerHandle, Any]:
    context = multiprocessing.get_context("spawn")
    port = _free_port()
    ready_event = context.Event()
    stop_event = context.Event() if transport == "new" else None
    target = _legacy_server_entry if transport == "old" else _new_server_entry
    arguments = (
        (port, ready_event) if transport == "old" else (port, ready_event, stop_event)
    )
    process = context.Process(target=target, args=arguments)
    process.start()
    root_identity: _ProcessIdentity | None = None
    try:
        root_identity = _identity(psutil.Process(process.pid))
        _wait_ready(process, ready_event)
        address = f"127.0.0.1:{port}"
        if transport == "old":
            import zerorpc

            client = zerorpc.Client(timeout=SERVER_TIMEOUT_SECONDS)
            client.connect(client_uri(address))
        else:
            from module.ocr.rpc import _ZmqRpcClient

            client = _ZmqRpcClient(address, timeout=SERVER_TIMEOUT_SECONDS)
        if client.hello() != "hello":
            raise BenchmarkError("Synthetic OCR server вернул неверный hello.")
        assert root_identity is not None
        return _ServerHandle(process, root_identity, stop_event), client
    except Exception as exc:
        if root_identity is None:
            exc.add_note(
                "Очистка benchmark не завершена: identity процесса не подтверждена."
            )
        else:
            try:
                _ServerHandle(process, root_identity, stop_event).close()
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(f"Очистка benchmark завершилась ошибкой: {cleanup_exc}")
        raise


def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _stats(samples: list[float]) -> dict[str, Any]:
    return {
        "runs": len(samples),
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
        "samples_ms": [round(sample, 3) for sample in samples],
    }


def _measure(
    call: Any,
    *,
    runs: int,
    iterations: int,
) -> dict[str, Any]:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        for _ in range(iterations):
            call()
        samples.append((time.perf_counter() - started) * 1000 / iterations)
    return {
        "request_iterations_per_run": iterations,
        "per_request": _stats(samples),
    }


def _benchmark_transport(
    transport: str,
    *,
    repo_root: Path,
    runs: int,
    iterations: int,
    startup_runs: int,
    batch_size: int,
) -> dict[str, Any]:
    image = np.arange(np.prod(IMAGE_SHAPE), dtype=np.uint8).reshape(IMAGE_SHAPE)
    payload = encode_image_payload(image)
    images = [image] * batch_size
    payloads = [payload] * batch_size
    startup_samples = []
    for _ in range(startup_runs):
        started = time.perf_counter()
        handle, client = _start_server(transport)
        try:
            startup_samples.append((time.perf_counter() - started) * 1000)
        finally:
            _close_client(client)
            handle.close()

    handle, client = _start_server(transport)
    try:
        if transport == "old":
            single = lambda: client.ocr("azur_lane", payload)
            batch = lambda: client.ocr_for_single_lines("azur_lane", payloads)
        else:
            single = lambda: client("ocr", "azur_lane", image)
            batch = lambda: client("ocr_for_single_lines", "azur_lane", images)
        expected = int(image.size)
        if single() != expected or batch() != [expected] * batch_size:
            raise BenchmarkError("Synthetic OCR transport вернул неверный результат.")
        for _ in range(3):
            single()
            batch()

        repeated_samples = []
        for _ in range(runs):
            started = time.perf_counter()
            for _ in range(iterations):
                if single() != expected:
                    raise BenchmarkError("Повторный synthetic OCR ответ неверен.")
            repeated_samples.append((time.perf_counter() - started) * 1000)
        measurements = {
            "startup_readiness": {
                **_stats(startup_samples),
                "includes_initial_hello": True,
            },
            "single": _measure(single, runs=runs, iterations=iterations),
            "batch": {
                "batch_size": batch_size,
                **_measure(batch, runs=runs, iterations=iterations),
            },
            "repeated_single_sequence": {
                "requests_per_run": iterations,
                "total": _stats(repeated_samples),
                "median_per_request_ms": round(
                    statistics.median(repeated_samples) / iterations,
                    3,
                ),
            },
        }
    finally:
        _close_client(client)
        handle.close()

    return {
        "schema_version": 1,
        "transport": transport,
        "git_head": _git_head(repo_root),
        "environment": _environment(transport),
        "workload": {
            "kind": "transport-only synthetic ndarray round-trip",
            "image_shape": list(IMAGE_SHAPE),
            "image_dtype": str(image.dtype),
            "encoded_image_bytes": len(payload),
            "runs": runs,
            "iterations": iterations,
            "startup_runs": startup_runs,
            "batch_size": batch_size,
        },
        "measurements": measurements,
    }


def _git_head(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=SERVER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchmarkError("Не удалось получить exact Git HEAD benchmark.") from exc
    return result.stdout.strip()


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _environment(transport: str) -> dict[str, Any]:
    return {
        "transport_dependency": {
            "zerorpc": _package_version("zerorpc") if transport == "old" else None,
            "pyzmq": _package_version("pyzmq"),
        },
        "numpy": _package_version("numpy"),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def _compare(paths: tuple[Path, Path]) -> int:
    old_path, new_path = paths
    old = json.loads(old_path.read_text(encoding="utf-8"))
    new = json.loads(new_path.read_text(encoding="utf-8"))
    if old.get("transport") != "old" or new.get("transport") != "new":
        raise BenchmarkError(
            "Для compare нужны old.json и new.json соответствующих transport."
        )
    print(f"old HEAD: {old['git_head']}")
    print(f"new HEAD: {new['git_head']}")
    for name in ("startup_readiness", "single", "batch"):
        old_value = (
            old["measurements"][name]["median_ms"]
            if name == "startup_readiness"
            else old["measurements"][name]["per_request"]["median_ms"]
        )
        new_value = (
            new["measurements"][name]["median_ms"]
            if name == "startup_readiness"
            else new["measurements"][name]["per_request"]["median_ms"]
        )
        delta = (new_value - old_value) / old_value * 100 if old_value else 0.0
        print(
            f"{name}: old={old_value:.3f} ms, new={new_value:.3f} ms, "
            f"delta={delta:+.1f}%"
        )
    old_repeated = old["measurements"]["repeated_single_sequence"][
        "median_per_request_ms"
    ]
    new_repeated = new["measurements"]["repeated_single_sequence"][
        "median_per_request_ms"
    ]
    delta = (new_repeated - old_repeated) / old_repeated * 100 if old_repeated else 0.0
    print(
        "repeated_single_per_request: "
        f"old={old_repeated:.3f} ms, new={new_repeated:.3f} ms, delta={delta:+.1f}%"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded synthetic benchmark zerorpc vs pyzmq OCR transport"
    )
    parser.add_argument("--transport", choices=("old", "new"))
    parser.add_argument(
        "--compare", nargs=2, type=Path, metavar=("OLD_JSON", "NEW_JSON")
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--startup-runs", type=int, default=DEFAULT_STARTUP_RUNS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.compare is not None:
        if args.transport is not None:
            raise BenchmarkError("--compare нельзя совмещать с --transport.")
        return _compare((args.compare[0], args.compare[1]))
    if args.transport is None:
        raise BenchmarkError(
            "Укажите --transport old/new или --compare OLD_JSON NEW_JSON."
        )
    if not 3 <= args.runs <= 31:
        raise BenchmarkError("--runs должен быть в диапазоне 3..31.")
    if not 1 <= args.iterations <= 1000:
        raise BenchmarkError("--iterations должен быть в диапазоне 1..1000.")
    if not 1 <= args.startup_runs <= 10:
        raise BenchmarkError("--startup-runs должен быть в диапазоне 1..10.")
    if not 1 <= args.batch_size <= 16:
        raise BenchmarkError("--batch-size должен быть в диапазоне 1..16.")
    result = _benchmark_transport(
        args.transport,
        repo_root=args.repo_root.absolute(),
        runs=args.runs,
        iterations=args.iterations,
        startup_runs=args.startup_runs,
        batch_size=args.batch_size,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Benchmark report записан: {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchmarkError as exc:
        print(f"Ошибка benchmark: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
