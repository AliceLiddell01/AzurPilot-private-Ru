from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_tools import dev_runtime as cli_module
from module.dev_runtime import (
    DevEnvironment,
    DevSession,
    DevSessionManager,
    DevSessionState,
    ProcessBackend,
    ProcessIdentity,
)
from module.dev_runtime import contracts as contracts_module
from module.dev_runtime import diagnostics as diagnostics_module
from module.dev_runtime import process as process_module


def _environment(tmp_path: Path) -> DevEnvironment:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True, exist_ok=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    return DevEnvironment(repository_root=root, python_executable=python)


def _identity(
    environment: DevEnvironment,
    session_id: str,
    *,
    pid: int = 7101,
) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        created_at=71.0,
        executable=str(environment.python_executable),
        command_line=tuple(ProcessBackend.expected_command(environment, session_id)),
        cwd=str(environment.repository_root),
    )


def test_process_identity_contract_is_bound_to_exact_session(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    identity = _identity(environment, "session-a")

    assert ProcessBackend.identity_belongs_to_session(
        environment, "session-a", identity
    )
    assert not ProcessBackend.identity_belongs_to_session(
        environment, "session-b", identity
    )

    payload = DevSession(
        session_id="session-b",
        state=DevSessionState.RUNNING,
        repository_root=str(environment.repository_root),
        created_at="2026-08-29T00:00:00+00:00",
        updated_at="2026-08-29T00:00:00+00:00",
        process=identity,
    ).as_dict()
    with pytest.raises(ValueError, match="другой DevSession"):
        DevSession.from_dict(payload)


def test_capture_rejects_pid_reuse_after_launch_expectation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    backend = ProcessBackend()
    backend._launch_expectations[7102] = (environment, "expected-session")
    foreign = _identity(environment, "foreign-session", pid=7102)

    class FakeProcess:
        def status(self):
            return "running"

        def create_time(self):
            return foreign.created_at

        def exe(self):
            return foreign.executable

        def cmdline(self):
            return list(foreign.command_line)

        def cwd(self):
            return foreign.cwd

    monkeypatch.setattr(process_module.psutil, "Process", lambda _pid: FakeProcess())

    assert backend.capture(7102) is None


def test_force_stop_fails_if_owned_child_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    backend = ProcessBackend()
    identity = _identity(environment, "owned-session", pid=7103)
    backend._launch_expectations[identity.pid] = (environment, "owned-session")
    match_results = iter((True, True))

    monkeypatch.setattr(backend, "matches", lambda _identity: next(match_results))

    child = SimpleNamespace(pid=7201, kill=lambda: None)
    root = SimpleNamespace(
        pid=identity.pid,
        children=lambda recursive: [child],
        kill=lambda: None,
    )
    monkeypatch.setattr(process_module.psutil, "Process", lambda _pid: root)
    monkeypatch.setattr(
        process_module.psutil,
        "wait_procs",
        lambda processes, timeout: ([root], [child]),
    )

    assert backend.force_stop(identity) is False


def test_process_stop_helpers_fail_closed_on_ownership_probe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    backend = ProcessBackend()
    identity = _identity(environment, "owned-session", pid=7104)
    backend._launch_expectations[identity.pid] = (environment, "owned-session")

    def broken_matches(_identity):
        raise RuntimeError("synthetic ownership probe failure")

    monkeypatch.setattr(backend, "matches", broken_matches)

    assert backend.request_stop(identity) is False
    assert backend.wait_exit(identity, 0) is False
    assert backend.force_stop(identity) is False


def test_find_by_session_rejects_same_token_with_wrong_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    session_id = "ambiguous-session"
    fake = SimpleNamespace(
        info={
            "pid": 7105,
            "create_time": 71.05,
            "exe": str(environment.python_executable),
            "cwd": str(environment.repository_root),
            "cmdline": [
                str(environment.python_executable),
                str(environment.repository_root / "gui.py"),
                "--dev-session-id",
                session_id,
                "--run",
                "ap",
            ],
        }
    )
    monkeypatch.setattr(process_module.psutil, "process_iter", lambda attrs: [fake])

    with pytest.raises(RuntimeError, match="полная process identity"):
        ProcessBackend().find_by_session(environment, session_id)


def test_acceptance_cli_exposes_only_diagnostics_and_smoke() -> None:
    parser = cli_module._parser()

    for command in ("preflight", "doctor", "status", "smoke"):
        assert parser.parse_args([command]).command == command
    for command in ("start", "stop", "recover"):
        with pytest.raises(SystemExit):
            parser.parse_args([command])


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv symlink regression")
def test_dev_environment_keeps_venv_symlink_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir()
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable))

    monkeypatch.setattr(contracts_module.sys, "executable", str(python))
    environment = DevEnvironment.current(root)

    assert environment.python_executable == Path(os.path.abspath(python))
    assert environment.python_executable != python.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv symlink regression")
def test_project_python_check_accepts_venv_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir()
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable))
    environment = DevEnvironment(root, python)
    manager = DevSessionManager(
        environment,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
    )

    monkeypatch.setattr(diagnostics_module.sys, "version_info", (3, 14, 6))
    monkeypatch.setattr(diagnostics_module.sys, "executable", str(python))

    assert manager._project_python_is_supported() is True


def test_readiness_rejects_ap_worker_outside_devsession_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    identity = _identity(environment, "tree-session", pid=7301)
    registry = environment.repository_root / "cache" / "webui-workers.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "owner_pid": 7302,
                "owner_created_at": 73.02,
                "workers": {"ap": {"pid": 7303, "created_at": 73.03}},
            }
        ),
        encoding="utf-8",
    )

    class TreeBackend:
        def is_descendant(self, child_pid: int, _parent: ProcessIdentity) -> bool:
            return child_pid == 7302

        def listens_on(self, pid: int, host: str, port: int) -> bool:
            return pid == 7302 and host == environment.host and port == environment.port

    manager = DevSessionManager(
        environment,
        process_backend=TreeBackend(),
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
    )

    from module.webui import worker_registry

    monkeypatch.setattr(worker_registry, "process_matches", lambda _record: True)
    monkeypatch.setattr(diagnostics_module, "_http_ready", lambda _host, _port: True)

    ready, reason = manager._default_readiness_probe(environment, identity)

    assert ready is False
    assert "рабочий процесс профиля ap не принадлежит дереву DevSession" in reason
