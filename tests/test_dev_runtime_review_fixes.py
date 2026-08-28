from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_tools import dev_runtime as cli_module
from module.dev_runtime import DevEnvironment, DevSessionManager, ProcessBackend
from module.dev_runtime import diagnostics as diagnostics_module
from module.dev_runtime import process as process_module


def _environment(tmp_path: Path) -> DevEnvironment:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True, exist_ok=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python = root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python"
    )
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    return DevEnvironment(repository_root=root, python_executable=python)


def test_cli_converts_manager_value_error_to_structured_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BrokenManager:
        def status(self):
            raise ValueError("synthetic unsafe state")

    monkeypatch.setattr(cli_module, "DevSessionManager", BrokenManager)
    monkeypatch.setattr(sys, "argv", ["dev_runtime.py", "status"])

    exit_code = cli_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["code"] == "DEV_CLI_FAILED"
    assert payload["state"] == "failed"
    assert "ValueError" in payload["message"]


def test_find_by_session_fails_closed_when_token_identity_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    session_id = "review-fix-session"
    fake = SimpleNamespace(
        info={
            "pid": 7788,
            "create_time": 10.0,
            "exe": None,
            "cwd": None,
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

    with pytest.raises(RuntimeError, match="executable/cwd"):
        ProcessBackend().find_by_session(environment, session_id)


def test_project_python_check_requires_same_supported_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    manager = DevSessionManager(
        environment,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
    )
    monkeypatch.setattr(diagnostics_module.sys, "version_info", (3, 14, 6))
    monkeypatch.setattr(diagnostics_module.sys, "executable", str(environment.python_executable))

    assert manager._project_python_is_supported() is True

    other = environment.repository_root / "other-python"
    monkeypatch.setattr(diagnostics_module.sys, "executable", str(other))
    assert manager._project_python_is_supported() is False
