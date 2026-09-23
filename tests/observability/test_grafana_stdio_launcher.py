from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from azurpilot.integrations import grafana_stdio as target
from azurpilot.integrations.config import IntegrationConfig


def _patch_repository_and_topology(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(
        target.RepositoryResolver,
        "resolve",
        lambda _self: SimpleNamespace(path=root),
    )
    monkeypatch.setattr(
        "azurpilot.integrations.adapters._executable", lambda _command: "docker"
    )
    inspect_payload = (
        json.dumps(
            {
                "com.docker.compose.project.config_files": str(
                    root / "infrastructure" / "observability" / "compose.yaml"
                ),
                "com.docker.compose.project.working_dir": str(
                    root / "infrastructure" / "observability"
                ),
            }
        )
        + "\t"
        + json.dumps({"3000/tcp": [{"HostPort": "3000"}]})
        + "\t"
        + json.dumps({"azurpilot-infrastructure_default": {}})
        + "\n"
    )

    def fake_docker(_root, _executable, arguments):
        return "grafana-id\n" if arguments[0] == "ps" else inspect_payload

    monkeypatch.setattr("azurpilot.integrations.adapters._docker_readonly", fake_docker)


def test_launcher_reuses_adapter_for_network_endpoint_and_credential(
    monkeypatch, tmp_path: Path
):
    _patch_repository_and_topology(monkeypatch, tmp_path)
    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE", raising=False)
    token = "fixture-grafana-launcher-token"
    credential_file = tmp_path / "grafana-token"
    credential_file.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setattr(
        target,
        "load_integration_config",
        lambda _root: IntegrationConfig(
            values={"grafana": {"credential_file": str(credential_file)}}
        ),
    )

    root, command, environment = target.resolve_child_command()

    assert root == tmp_path
    assert command[0] == "docker"
    assert command[command.index("--network") + 1] == (
        "azurpilot-infrastructure_default"
    )
    assert environment["GRAFANA_URL"] == "http://grafana:3000"
    assert "localhost" not in " ".join(command)
    assert token not in " ".join(command)
    assert environment["GRAFANA_SERVICE_ACCOUNT_TOKEN"] == token


def test_launcher_forwards_stdio_and_child_exit_code(
    monkeypatch, tmp_path: Path, capsys
):
    observed: dict[str, object] = {}

    def fake_resolve():
        return (
            tmp_path,
            ("docker", "run", "--rm", "-i"),
            {"GRAFANA_URL": "http://grafana:3000"},
        )

    class FakeProcess:
        returncode = 23

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 23

        def terminate(self) -> None:
            raise AssertionError("завершившийся child не должен завершаться повторно")

        def kill(self) -> None:
            raise AssertionError("завершившийся child не должен принудительно завершаться")

    def fake_popen(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(target, "resolve_child_command", fake_resolve)
    monkeypatch.setattr(target.subprocess, "Popen", fake_popen)

    assert target.main() == 23
    assert observed["command"] == ("docker", "run", "--rm", "-i")
    assert observed["kwargs"] == {
        "cwd": tmp_path,
        "env": {"GRAFANA_URL": "http://grafana:3000"},
    }
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_launcher_fails_closed_without_stdout_or_secret_diagnostics(
    monkeypatch, capsys
):
    token = "fixture-launcher-secret"

    def fail_to_resolve():
        raise target.GrafanaLauncherError(token)

    monkeypatch.setattr(target, "resolve_child_command", fail_to_resolve)

    assert target.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert token not in captured.err
