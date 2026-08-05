from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
SENSITIVE_MARKERS = (
    "-----BEGIN " + "PRIVATE KEY-----",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "github_pat_",
    "ghp_",
    "discord.com/api/webhooks/",
    "hooks.slack.com/services/",
)


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _config_snapshot() -> dict[str, bytes]:
    if not CONFIG_DIR.exists():
        return {}
    return {
        path.relative_to(ROOT).as_posix(): path.read_bytes()
        for path in sorted(CONFIG_DIR.iterdir())
        if path.is_file()
    }


def _wait_for_http(port: int, process: subprocess.Popen[bytes], timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"WebUI завершилась до готовности с кодом {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status != 200:
                    raise RuntimeError(f"WebUI вернула HTTP {response.status}")
                return response.read()
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"WebUI не стала доступна за {timeout:.0f} с: {last_error}")


def _stop_process_tree(process: subprocess.Popen[bytes]) -> list[int]:
    try:
        parent = psutil.Process(process.pid)
    except psutil.NoSuchProcess:
        process.wait(timeout=5)
        return []

    targets = parent.children(recursive=True) + [parent]
    for target in reversed(targets):
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=8)
    for target in alive:
        try:
            target.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=5)
    process.wait(timeout=5)
    return [target.pid for target in alive if target.is_running()]


def run(timeout: float = 45) -> dict[str, object]:
    before = _config_snapshot()
    port = _free_local_port()
    with tempfile.TemporaryDirectory(prefix="azurpilot-webui-smoke-") as temporary:
        environment = os.environ.copy()
        environment.update(
            {
                "AZURPILOT_NTP_DISABLE": "1",
                "AZURPILOT_WORKER_REGISTRY_FILE": str(
                    Path(temporary) / "webui-workers.json"
                ),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        stdout_path = Path(temporary) / "stdout.log"
        stderr_path = Path(temporary) / "stderr.log"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "gui.py",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=environment,
                stdout=stdout,
                stderr=stderr,
            )
            body = b""
            failure: Exception | None = None
            try:
                body = _wait_for_http(port, process, timeout)
            except Exception as exc:
                failure = exc
            alive = _stop_process_tree(process)

        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
        combined = stdout_text + "\n" + stderr_text
        sensitive = [marker for marker in SENSITIVE_MARKERS if marker.lower() in combined.lower()]

    after = _config_snapshot()
    result = {
        "http_ok": bool(body) and b"pywebio" in body.lower(),
        "process_exit_code": process.returncode,
        "remaining_processes": alive,
        "config_unchanged": before == after,
        "artifact_sensitive_markers": sensitive,
    }
    errors = []
    if failure is not None:
        errors.append(str(failure))
    if not result["http_ok"]:
        errors.append("Ответ WebUI не содержит загрузчик PyWebIO")
    if alive:
        errors.append(f"После остановки остались процессы: {alive}")
    if before != after:
        errors.append("WebUI изменила config во время изолированного smoke-теста")
    if sensitive:
        errors.append(f"В smoke-артефактах найдены чувствительные маркеры: {sensitive}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Изолированный Windows/WebUI smoke-тест Stage 6")
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    print(json.dumps(run(args.timeout), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
