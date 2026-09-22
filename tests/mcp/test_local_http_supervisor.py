from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

import module.mcp_shared.local_http_supervisor as supervisor_module
from azurpilot.tooling.coordination import FileLock
from azurpilot.tooling.process import ProcessIdentity
from module.mcp_shared.local_http_supervisor import (
    LocalHttpService,
    LocalHttpSupervisor,
    LocalHttpSupervisorError,
    LocalHttpSupervisorStopOutcome,
    _identity_from_marker,
    _identity_to_marker,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_DEV_TOKEN_ENV = "AZURPILOT_DEV_LOCAL_MCP_TOKEN"
_GAME_TOKEN_ENV = "AZURPILOT_GAME_LOCAL_MCP_TOKEN"
_TEST_MODULE_PREFIX = "test_local_mcp_"


def _process_identity(pid: int) -> dict[str, object] | None:
    try:
        return _identity_to_marker(ProcessIdentity.capture(pid))
    except (OSError, psutil.Error):
        return None


def _identity_matches(process: psutil.Process, expected: dict[str, object]) -> bool:
    identity = _identity_from_marker(expected)
    return identity is not None and identity.matches(process)


def _try_lock(path: Path) -> FileLock | None:
    lock = FileLock(path)
    return lock if lock.acquire(timeout_seconds=0) else None


def _release_lock(handle: FileLock) -> None:
    handle.release()

pytestmark = pytest.mark.xdist_group(name="local-http-supervisor")

_SERVICE_TEMPLATE = """
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


SERVER_NAME = {server_name!r}
PORT = {port}
IGNORE_TERM = os.environ.get("TEST_LOCAL_MCP_IGNORE_TERM") == "1"
CRASH_AFTER_READY = os.environ.get("TEST_LOCAL_MCP_CRASH_AFTER_READY") == SERVER_NAME
SPAWN_CHILD = os.environ.get("TEST_LOCAL_MCP_SPAWN_CHILD") == SERVER_NAME
CRASH_DELAY_SECONDS = float(
    os.environ.get("TEST_LOCAL_MCP_CRASH_DELAY_SECONDS", "0")
)
stopping = False
crash_at = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global crash_at
        if self.path != "/ready":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(
            {{
                "ok": True,
                "code": "LOCAL_MCP_READY",
                "server_name": SERVER_NAME,
                "transport": "local_http",
            }}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if CRASH_AFTER_READY and CRASH_DELAY_SECONDS > 0 and crash_at is None:
            crash_at = time.monotonic() + CRASH_DELAY_SECONDS

    def log_message(self, _format, *_args):
        return


def _handle_signal(_signum, _frame):
    global stopping
    if not IGNORE_TERM:
        stopping = True


signal.signal(signal.SIGTERM, _handle_signal)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _handle_signal)

server = HTTPServer(("127.0.0.1", PORT), Handler)
server.timeout = 0.05
if SPAWN_CHILD:
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("test_local_mcp_worker.py")),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
if CRASH_AFTER_READY:
    crash_after_ready = True
else:
    crash_after_ready = False

while not stopping:
    server.handle_request()
    if crash_after_ready:
        if CRASH_DELAY_SECONDS <= 0 or (
            crash_at is not None and time.monotonic() >= crash_at
        ):
            crash_after_ready = False
            os._exit(17)
server.server_close()
"""

_RUNNER_TEMPLATE = """
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import module.mcp_shared.local_http_supervisor as supervisor_module
from module.mcp_shared.local_http_supervisor import (
    LocalHttpService,
    LocalHttpSupervisor,
    LocalHttpSupervisorError,
)


root = Path(sys.argv[1])
service_specs = json.loads(sys.argv[2])
services = tuple(LocalHttpService(**spec) for spec in service_specs)
if os.environ.get("TEST_LOCAL_MCP_STOP_TIMEOUT"):
    supervisor_module.STOP_TIMEOUT_SECONDS = float(
        os.environ["TEST_LOCAL_MCP_STOP_TIMEOUT"]
    )
supervisor = LocalHttpSupervisor(
    root,
    python_executable=Path(sys.executable),
    services=services,
    startup_timeout_seconds=5,
    allow_test_environment=True,
)
try:
    result = supervisor.serve()
except LocalHttpSupervisorError as exc:
    print(type(exc).__name__, file=sys.stderr)
    raise SystemExit(2) from None
raise SystemExit(0)
"""


def _prepare_test_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='test'\n", encoding="utf-8"
    )


def _supervisor(tmp_path: Path) -> LocalHttpSupervisor:
    _prepare_test_project(tmp_path)
    return LocalHttpSupervisor(tmp_path, python_executable=sys.executable)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _write_service(
    tmp_path: Path, module_name: str, server_name: str, port: int
) -> None:
    (tmp_path / f"{module_name}.py").write_text(
        _SERVICE_TEMPLATE.format(server_name=server_name, port=port),
        encoding="utf-8",
    )
    (tmp_path / "test_local_mcp_worker.py").write_text(
        "import time\ntime.sleep(30)\n", encoding="utf-8"
    )


def _write_runner(tmp_path: Path) -> Path:
    runner = tmp_path / "supervisor_runner.py"
    runner.write_text(_RUNNER_TEMPLATE, encoding="utf-8")
    return runner


def _spec(
    *,
    module_name: str,
    server_name: str,
    port: int,
    token_env_var: str,
) -> dict[str, object]:
    return {
        "name": server_name,
        "module": (
            module_name
            if module_name.startswith(_TEST_MODULE_PREFIX)
            else f"{_TEST_MODULE_PREFIX}{module_name}"
        ),
        "port": port,
        "token_env_var": token_env_var,
    }


def _launch(
    tmp_path: Path,
    specs: list[dict[str, object]],
    *,
    crash_after_ready: str | None = None,
    crash_delay_seconds: float | None = None,
    ignore_term: bool = False,
    spawn_child: str | None = None,
    stop_timeout: float | None = None,
) -> subprocess.Popen[str]:
    _prepare_test_project(tmp_path)
    environment = os.environ.copy()
    environment[_DEV_TOKEN_ENV] = "dev-test-token"
    environment[_GAME_TOKEN_ENV] = "game-test-token"
    environment["TEST_LOCAL_MCP_CRASH_AFTER_READY"] = crash_after_ready or ""
    environment["TEST_LOCAL_MCP_CRASH_DELAY_SECONDS"] = (
        str(crash_delay_seconds) if crash_delay_seconds is not None else "0"
    )
    environment["TEST_LOCAL_MCP_IGNORE_TERM"] = "1" if ignore_term else "0"
    environment["TEST_LOCAL_MCP_SPAWN_CHILD"] = spawn_child or ""
    if stop_timeout is not None:
        environment["TEST_LOCAL_MCP_STOP_TIMEOUT"] = str(stop_timeout)
    python_path = str(REPOSITORY_ROOT)
    if environment.get("PYTHONPATH"):
        python_path += os.pathsep + environment["PYTHONPATH"]
    environment["PYTHONPATH"] = python_path

    runner = _write_runner(tmp_path)
    kwargs: dict[str, object] = {
        "cwd": str(REPOSITORY_ROOT),
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        [sys.executable, str(runner), str(tmp_path), json.dumps(specs)],
        **kwargs,
    )


def _wait_until(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Ожидаемое состояние supervisor не наступило")
        time.sleep(0.05)


def _observer(tmp_path: Path, specs: list[dict[str, object]]) -> LocalHttpSupervisor:
    _prepare_test_project(tmp_path)
    services = tuple(LocalHttpService(**spec) for spec in specs)
    return LocalHttpSupervisor(
        tmp_path,
        python_executable=sys.executable,
        services=services,
        startup_timeout_seconds=5,
    )


def _stale_supervisor(tmp_path: Path) -> LocalHttpSupervisor:
    return _observer(
        tmp_path,
        [
            _spec(
                module_name="test_stale_marker",
                server_name="azurpilot-dev",
                port=_free_port(),
                token_env_var=_DEV_TOKEN_ENV,
            )
        ],
    )


def _write_valid_stale_marker(observer: LocalHttpSupervisor) -> dict[str, object]:
    current = _process_identity(os.getpid())
    assert current is not None
    stale = dict(current)
    stale["created_at"] = float(current["created_at"]) + 1.0
    services = [
        {
            "name": service.name,
            "port": service.port,
            "token_env_var": service.token_env_var,
            "process": dict(stale),
            "launcher_process": dict(stale),
        }
        for service in observer.services
    ]
    supervisor_launcher = dict(stale)
    supervisor_launcher["pid"] = 2_147_000_000
    supervisor_marker = dict(stale)
    supervisor_marker["launcher_process"] = supervisor_launcher
    payload: dict[str, object] = {
        "schema_version": 1,
        "repository_root": str(observer.repository_root),
        "python_executable": str(observer.python_executable),
        "supervisor": supervisor_marker,
        "services": services,
    }
    observer.marker_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _wait_ready(observer: LocalHttpSupervisor) -> dict[str, object]:
    _wait_until(lambda: observer.status().get("code") == "LOCAL_MCP_SUPERVISOR_READY")
    return observer.status()


def _finish_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=5)


def _cleanup_running_supervisor(
    process: subprocess.Popen[str], observer: LocalHttpSupervisor
) -> None:
    # Marker может остаться даже после неожиданного завершения launcher;
    # cleanup должен идти по exact identity, а не зависеть от poll().
    observer.stop()
    _finish_process(process)


def _assert_ports_closed(ports: list[int]) -> None:
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            assert probe.connect_ex(("127.0.0.1", port)) != 0


def _identity_is_gone(identity: dict[str, object]) -> bool:
    try:
        process = psutil.Process(int(identity["pid"]))
    except (psutil.Error, KeyError, TypeError, ValueError):
        return True
    return not _identity_matches(process, identity)


def _set_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_DEV_TOKEN_ENV, "dev-test-token")
    monkeypatch.setenv(_GAME_TOKEN_ENV, "game-test-token")


def _test_owned_processes() -> list[str]:
    leftovers = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = tuple(str(item) for item in (process.info["cmdline"] or ()))
        except psutil.Error, OSError, TypeError:
            continue
        if any("test_local_mcp_" in item for item in command):
            leftovers.append(f"{process.info['pid']}: {' '.join(command)}")
    return leftovers


@pytest.fixture(autouse=True)
def _assert_test_processes_are_clean() -> Iterator[None]:
    assert not _test_owned_processes()
    yield
    assert not _test_owned_processes()


def test_supervisor_lock_allows_only_one_owner(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    first = _try_lock(supervisor.lock_path)
    assert first is not None
    try:
        assert _try_lock(supervisor.lock_path) is None
    finally:
        _release_lock(first)
    second = _try_lock(supervisor.lock_path)
    assert second is not None
    _release_lock(second)


def test_supervisor_validates_both_bearer_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    monkeypatch.setenv(_DEV_TOKEN_ENV, "dev-test-token")
    monkeypatch.delenv(_GAME_TOKEN_ENV, raising=False)

    with pytest.raises(LocalHttpSupervisorError, match=_GAME_TOKEN_ENV):
        supervisor._validate()


def test_supervisor_commands_are_direct_project_python_http_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    _set_tokens(monkeypatch)
    supervisor._validate()

    assert supervisor._command(
        LocalHttpService(
            "azurpilot-game",
            "module.game_mcp.local_http",
            8776,
            _GAME_TOKEN_ENV,
        )
    ) == [sys.executable, "-u", "-m", "module.game_mcp.local_http"]


def test_supervisor_real_services_readiness_status_marker_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_tokens(monkeypatch)
    specs = [
        _spec(
            module_name="test_local_mcp_dev",
            server_name="azurpilot-dev",
            port=_free_port(),
            token_env_var=_DEV_TOKEN_ENV,
        ),
        _spec(
            module_name="test_local_mcp_game",
            server_name="azurpilot-game",
            port=_free_port(),
            token_env_var=_GAME_TOKEN_ENV,
        ),
    ]
    for item in specs:
        _write_service(tmp_path, item["module"], item["name"], item["port"])

    process = _launch(tmp_path, specs)
    observer = _observer(tmp_path, specs)
    try:
        status = _wait_ready(observer)
        assert {item["server_name"] for item in status["services"]} == {
            "azurpilot-dev",
            "azurpilot-game",
        }
        assert all(item["alive"] and item["ready"] for item in status["services"])
        marker = observer.marker_path.read_text(encoding="utf-8")
        assert "dev-test-token" not in marker
        assert "game-test-token" not in marker
        assert _DEV_TOKEN_ENV in marker and _GAME_TOKEN_ENV in marker

        result = observer.stop_result()
        assert result.outcome is LocalHttpSupervisorStopOutcome.EXACT_LIVE_OWNER_STOPPED
        assert result.ok is True
        _finish_process(process)
        assert observer.status()["code"] == "LOCAL_MCP_SUPERVISOR_STOPPED"
        assert not observer.marker_path.exists()
        lock = _try_lock(observer.lock_path)
        assert lock is not None
        _release_lock(lock)
        _assert_ports_closed([item["port"] for item in specs])
    finally:
        _cleanup_running_supervisor(process, observer)


def test_status_does_not_publish_readiness_metadata_for_dead_owned_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_test_project(tmp_path)
    service = LocalHttpService(
        "azurpilot-dev",
        "test_local_mcp_dead",
        _free_port(),
        _DEV_TOKEN_ENV,
    )
    observer = LocalHttpSupervisor(
        tmp_path,
        python_executable=sys.executable,
        services=(service,),
    )
    supervisor_identity = _process_identity(os.getpid())
    assert supervisor_identity is not None
    dead_service_identity = dict(supervisor_identity)
    dead_service_identity["created_at"] = -1.0
    observer.marker_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_root": str(observer.repository_root),
                "python_executable": str(observer.python_executable),
                "supervisor": supervisor_identity,
                "services": [
                    {
                        "name": service.name,
                        "port": service.port,
                        "token_env_var": service.token_env_var,
                        "process": dead_service_identity,
                        "launcher_process": supervisor_identity,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def unexpected_ready_payload(item: LocalHttpService) -> dict[str, object]:
        calls.append(item.name)
        return {"server_version": "rogue"}

    monkeypatch.setattr(
        LocalHttpSupervisor,
        "_ready_payload",
        staticmethod(unexpected_ready_payload),
    )

    status = observer.status()

    assert status["code"] == "LOCAL_MCP_SUPERVISOR_DEGRADED"
    assert calls == []
    service_status = status["services"][0]
    assert service_status["alive"] is False
    assert service_status["ready"] is False
    assert "server_version" not in service_status


def test_supervisor_cleanup_removes_exact_unrecorded_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_tokens(monkeypatch)
    spec = _spec(
        module_name="test_descendant_service",
        server_name="azurpilot-dev",
        port=_free_port(),
        token_env_var=_DEV_TOKEN_ENV,
    )
    _write_service(tmp_path, spec["module"], spec["name"], spec["port"])
    process = _launch(tmp_path, [spec], spawn_child=spec["name"])
    observer = _observer(tmp_path, [spec])
    child_identities: list[dict[str, object]] = []
    try:
        _wait_ready(observer)
        marker = json.loads(observer.marker_path.read_text(encoding="utf-8"))
        runtime_pid = marker["services"][0]["process"]["pid"]

        def collect_worker_identities() -> bool:
            nonlocal child_identities
            try:
                runtime = psutil.Process(runtime_pid)
                child_identities = [
                    identity
                    for child in runtime.children(recursive=True)
                    if (identity := _process_identity(child.pid)) is not None
                    and any(
                        "test_local_mcp_worker.py" in item
                        for item in identity["command"]
                    )
                ]
            except (psutil.Error, OSError, TypeError, ValueError):
                child_identities = []
            return bool(child_identities)

        _wait_until(collect_worker_identities)
        assert observer.stop() is True
        _finish_process(process)
        _wait_until(lambda: all(_identity_is_gone(item) for item in child_identities))
        assert not _test_owned_processes()
    finally:
        _cleanup_running_supervisor(process, observer)


def test_supervisor_cleans_first_child_after_partial_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_tokens(monkeypatch)
    first_port = _free_port()
    second_port = _free_port()
    first = _spec(
        module_name="test_partial_first",
        server_name="azurpilot-dev",
        port=first_port,
        token_env_var=_DEV_TOKEN_ENV,
    )
    second = _spec(
        module_name="test_partial_second",
        server_name="azurpilot-game",
        port=second_port,
        token_env_var=_GAME_TOKEN_ENV,
    )
    _write_service(tmp_path, first["module"], first["name"], first["port"])
    _write_service(tmp_path, second["module"], second["name"], second["port"])
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", second_port))
    occupied.listen()
    supervisor = _observer(tmp_path, [first, second])
    try:
        with pytest.raises(LocalHttpSupervisorError, match="azurpilot-game"):
            supervisor.serve()
        _assert_ports_closed([first_port])
        lock = _try_lock(supervisor.lock_path)
        assert lock is not None
        _release_lock(lock)
    finally:
        occupied.close()


def test_supervisor_child_crash_after_readiness_cleans_marker_and_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_tokens(monkeypatch)
    spec = _spec(
        module_name="test_crashing_service",
        server_name="azurpilot-dev",
        port=_free_port(),
        token_env_var=_DEV_TOKEN_ENV,
    )
    _write_service(tmp_path, spec["module"], spec["name"], spec["port"])
    process = _launch(
        tmp_path,
        [spec],
        crash_after_ready=spec["name"],
        crash_delay_seconds=1.0,
    )
    observer = _observer(tmp_path, [spec])
    try:
        _wait_ready(observer)
        _wait_until(lambda: process.poll() is not None, timeout=8)
        _finish_process(process)
        assert process.returncode == 2
        assert not observer.marker_path.exists()
        assert observer.status()["code"] == "LOCAL_MCP_SUPERVISOR_STOPPED"
        _assert_ports_closed([spec["port"]])
    finally:
        _cleanup_running_supervisor(process, observer)


def test_supervisor_port_collision_fails_closed_without_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_tokens(monkeypatch)
    spec = _spec(
        module_name="test_collision_service",
        server_name="azurpilot-dev",
        port=_free_port(),
        token_env_var=_DEV_TOKEN_ENV,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", spec["port"]))
    listener.listen()
    supervisor = _observer(tmp_path, [spec])
    try:
        with pytest.raises(LocalHttpSupervisorError, match="уже занят"):
            supervisor.serve()
        assert not supervisor.marker_path.exists()
        lock = _try_lock(supervisor.lock_path)
        assert lock is not None
        _release_lock(lock)
    finally:
        listener.close()


def test_supervisor_invalid_marker_and_pid_reuse_are_not_owned(
    tmp_path: Path,
) -> None:
    supervisor = _supervisor(tmp_path)
    supervisor.marker_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_root": str(tmp_path.absolute()),
                "supervisor": {
                    "pid": 2_147_000_000,
                    "created_at": 0.0,
                    "executable": "C:/foreign/python.exe",
                    "command": ["foreign"],
                    "cwd": "C:/foreign",
                },
                "services": [],
            }
        ),
        encoding="utf-8",
    )
    assert supervisor.status()["code"] == "LOCAL_MCP_SUPERVISOR_MARKER_INVALID"

    current = _process_identity(os.getpid())
    assert current is not None
    mismatched = dict(current)
    mismatched["created_at"] = float(current["created_at"]) + 1.0
    supervisor.marker_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_root": str(tmp_path.absolute()),
                "supervisor": mismatched,
                "services": [],
            }
        ),
        encoding="utf-8",
    )
    result = supervisor.stop_result()
    assert result.outcome is LocalHttpSupervisorStopOutcome.INVALID_MARKER
    assert supervisor.stop() is False
    assert supervisor.marker_path.exists()
    assert psutil.Process(os.getpid()).is_running()


def test_supervisor_recovers_valid_same_repository_stale_marker(
    tmp_path: Path,
) -> None:
    supervisor = _stale_supervisor(tmp_path)
    payload = _write_valid_stale_marker(supervisor)
    validated = supervisor._validated_marker_identities(payload)
    assert not isinstance(validated, LocalHttpSupervisorStopOutcome)
    _supervisor_identity, identities = validated
    assert any(identity.pid == 2_147_000_000 for identity in identities)

    assert supervisor.status()["code"] == "LOCAL_MCP_SUPERVISOR_STALE"
    result = supervisor.stop_result()

    assert result.outcome is LocalHttpSupervisorStopOutcome.STALE_RECORDED_OWNER_RECOVERED
    assert result.marker_removed is True
    assert result.postcondition_confirmed is True
    assert supervisor.status()["code"] == "LOCAL_MCP_SUPERVISOR_STOPPED"
    assert psutil.Process(os.getpid()).is_running()


def test_supervisor_absent_marker_does_not_claim_removal(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)

    result = supervisor.stop_result()

    assert result.outcome is LocalHttpSupervisorStopOutcome.ALREADY_STOPPED
    assert result.marker_present is False
    assert result.marker_removed is False


def test_supervisor_rejects_foreign_repository_marker(
    tmp_path: Path,
) -> None:
    supervisor = _stale_supervisor(tmp_path)
    payload = _write_valid_stale_marker(supervisor)
    payload["repository_root"] = "C:/foreign/repository"
    supervisor.marker_path.write_text(json.dumps(payload), encoding="utf-8")

    result = supervisor.stop_result()

    assert result.outcome is LocalHttpSupervisorStopOutcome.OWNERSHIP_MISMATCH
    assert result.marker_removed is False
    assert supervisor.marker_path.exists()
    assert psutil.Process(os.getpid()).is_running()


def test_supervisor_fails_closed_for_access_denied_stale_identity_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _stale_supervisor(tmp_path)
    _write_valid_stale_marker(supervisor)

    def denied(_identity: ProcessIdentity) -> str:
        raise psutil.AccessDenied()

    monkeypatch.setattr(
        supervisor_module.ProcessController,
        "inspect_state",
        staticmethod(denied),
    )

    result = supervisor.stop_result()

    assert result.outcome is LocalHttpSupervisorStopOutcome.UNKNOWN_RECOVERY
    assert result.marker_removed is False
    assert supervisor.marker_path.exists()


def test_supervisor_preserves_marker_after_exact_termination_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _stale_supervisor(tmp_path)
    payload = _write_valid_stale_marker(supervisor)
    services = list(payload["services"])
    first = dict(services[0])
    launcher = dict(first["launcher_process"])
    launcher["pid"] = 2_147_000_000
    first["launcher_process"] = launcher
    services[0] = first
    payload["services"] = services
    supervisor.marker_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        supervisor_module.LocalHttpSupervisor,
        "_terminate_exact_identity",
        staticmethod(lambda _identity: False),
    )

    result = supervisor.stop_result()

    assert result.outcome is LocalHttpSupervisorStopOutcome.TERMINATION_FAILED
    assert result.marker_removed is False
    assert supervisor.marker_path.exists()


def test_supervisor_reports_postcondition_failure_after_safe_marker_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _stale_supervisor(tmp_path)
    _write_valid_stale_marker(supervisor)
    monkeypatch.setattr(supervisor, "_stopped_postcondition", lambda: False)

    result = supervisor.stop_result()

    assert result.outcome is LocalHttpSupervisorStopOutcome.POSTCONDITION_FAILED
    assert result.marker_removed is True
    assert not supervisor.marker_path.exists()


def test_supervisor_keeps_marker_when_foreign_port_remains(
    tmp_path: Path,
) -> None:
    spec = _spec(
        module_name="test_foreign_port",
        server_name="azurpilot-dev",
        port=_free_port(),
        token_env_var=_DEV_TOKEN_ENV,
    )
    supervisor = _observer(tmp_path, [spec])
    _write_valid_stale_marker(supervisor)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", int(spec["port"])))
    listener.listen()
    try:
        result = supervisor.stop_result()
    finally:
        listener.close()

    assert result.outcome is LocalHttpSupervisorStopOutcome.PORT_CONFLICT
    assert result.marker_removed is False
    assert supervisor.marker_path.exists()


def test_supervisor_rejects_marker_race_without_removing_new_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _stale_supervisor(tmp_path)
    payload = _write_valid_stale_marker(supervisor)
    original_remove = supervisor._remove_recorded_marker

    def change_marker_before_remove(recorded: object) -> bool:
        changed = dict(payload)
        services = list(changed["services"])
        first = dict(services[0])
        first["port"] = int(first["port"]) + 1
        services[0] = first
        changed["services"] = services
        supervisor.marker_path.write_text(json.dumps(changed), encoding="utf-8")
        return original_remove(recorded)

    monkeypatch.setattr(
        supervisor,
        "_remove_recorded_marker",
        change_marker_before_remove,
    )

    result = supervisor.stop_result()

    assert result.outcome is LocalHttpSupervisorStopOutcome.MARKER_CHANGED
    assert result.marker_removed is False
    assert supervisor.marker_path.exists()


def test_supervisor_recovers_stale_owner_with_exact_live_service_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tokens(monkeypatch)
    spec = _spec(
        module_name="test_stale_live_descendant",
        server_name="azurpilot-dev",
        port=_free_port(),
        token_env_var=_DEV_TOKEN_ENV,
    )
    _write_service(tmp_path, spec["module"], spec["name"], spec["port"])
    process = _launch(tmp_path, [spec])
    observer = _observer(tmp_path, [spec])
    try:
        _wait_ready(observer)
        process.kill()
        _finish_process(process)
        _wait_until(lambda: observer.status()["code"] == "LOCAL_MCP_SUPERVISOR_STALE")

        result = observer.stop_result()

        assert result.outcome is LocalHttpSupervisorStopOutcome.STALE_RECORDED_OWNER_RECOVERED
        assert result.postcondition_confirmed is True
        assert observer.status()["code"] == "LOCAL_MCP_SUPERVISOR_STOPPED"
        _assert_ports_closed([spec["port"]])
    finally:
        _cleanup_running_supervisor(process, observer)


def test_supervisor_forced_termination_cleans_uncooperative_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_tokens(monkeypatch)
    spec = _spec(
        module_name="test_uncooperative_service",
        server_name="azurpilot-dev",
        port=_free_port(),
        token_env_var=_DEV_TOKEN_ENV,
    )
    _write_service(tmp_path, spec["module"], spec["name"], spec["port"])
    process = _launch(
        tmp_path,
        [spec],
        ignore_term=True,
        stop_timeout=0.2,
    )
    observer = _observer(tmp_path, [spec])
    monkeypatch.setattr(supervisor_module, "STOP_TIMEOUT_SECONDS", 0.5)
    try:
        _wait_ready(observer)
        assert observer.stop() is True
        _finish_process(process)
        assert not observer.marker_path.exists()
        _assert_ports_closed([spec["port"]])
    finally:
        _cleanup_running_supervisor(process, observer)


def test_repeated_launcher_cannot_start_duplicate_exact_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_tokens(monkeypatch)
    spec = _spec(
        module_name="test_duplicate_service",
        server_name="azurpilot-dev",
        port=_free_port(),
        token_env_var=_DEV_TOKEN_ENV,
    )
    _write_service(tmp_path, spec["module"], spec["name"], spec["port"])
    first = _launch(tmp_path, [spec])
    observer = _observer(tmp_path, [spec])
    second = None
    try:
        _wait_ready(observer)
        second = _launch(tmp_path, [spec])
        stdout, stderr = _finish_process(second)
        assert second.returncode == 0
        assert stdout == ""
        assert stderr == ""
        assert observer.status()["code"] == "LOCAL_MCP_SUPERVISOR_READY"
        assert observer.stop() is True
        _finish_process(first)
    finally:
        if second is not None and second.poll() is None:
            second.kill()
            _finish_process(second)
        _cleanup_running_supervisor(first, observer)
