from __future__ import annotations

import re
from pathlib import Path

import pytest

import dev_tools.integration_contract_gate as gate
from azurpilot.integrations.adapters import DOCKER_HUB_BLOCKED_TOOLS
from azurpilot.integrations.config import DEFAULTS, SHARED_MCP_ENDPOINTS
from tests.support.paths import REPOSITORY_ROOT


def test_permanent_direct_integration_contract_is_ready():
    payload = gate.check(REPOSITORY_ROOT)

    assert payload["ok"] is True
    assert payload["code"] == "INTEGRATION_CONTRACT_READY"
    assert tuple(payload["families"]) == gate.EXPECTED_FAMILIES
    assert all(value == "ready" for value in payload["checks"].values())
    assert payload["errors"] == []


def test_permanent_contract_has_no_retired_profile_paths():
    assert gate.RETIRED_PROFILE_PATHS
    assert all(
        not (REPOSITORY_ROOT / relative).exists()
        for relative in gate.RETIRED_PROFILE_PATHS
    )


def test_contract_rejects_empty_docker_hub_denylist(tmp_path: Path):
    source = (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert DOCKER_HUB_BLOCKED_TOOLS

    mutated, replacements = re.subn(
        r"(?m)^disabled_tools = \[[^\n]*\]$",
        "disabled_tools = []",
        source,
        count=1,
    )
    assert replacements == 1

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(mutated, encoding="utf-8")

    payload = gate.check(tmp_path)

    assert payload["checks"]["codex_config"] == "drift"
    assert (
        ".codex/config.toml: dockerhub_direct denylist расходится с adapter contract"
        in payload["errors"]
    )


def test_contract_rejects_registration_that_owns_provider_process(tmp_path: Path):
    """Регистрация обязана подключаться к общему HTTP service, а не запускать provider."""

    source = (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
    endpoint = SHARED_MCP_ENDPOINTS["grafana"]
    mutated, replacements = re.subn(
        rf'(?m)^url = "{re.escape(endpoint)}"$',
        'command = "docker"\nargs = ["run", "mcp/grafana"]',
        source,
        count=1,
    )
    assert replacements == 1

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(mutated, encoding="utf-8")

    payload = gate.check(tmp_path)

    assert payload["checks"]["codex_config"] == "drift"
    assert (
        ".codex/config.toml: grafana_direct не должен владеть provider "
        "process-ом (command)"
    ) in payload["errors"]
    assert (
        ".codex/config.toml: grafana_direct не должен владеть provider "
        "process-ом (args)"
    ) in payload["errors"]
    assert (
        ".codex/config.toml: grafana_direct расходится с общим HTTP endpoint"
    ) in payload["errors"]


def test_contract_rejects_registration_without_caller_token(tmp_path: Path):
    """Регистрация обязана предъявлять caller token общего HTTP service."""

    source = (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
    caller_env = str(DEFAULTS["grafana"]["caller_token_env"])
    declaration = f'bearer_token_env_var = "{caller_env}"\n'
    assert source.count(declaration) == 1
    mutated = source.replace(declaration, "", 1)

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(mutated, encoding="utf-8")

    payload = gate.check(tmp_path)

    assert payload["checks"]["codex_config"] == "drift"
    assert (
        ".codex/config.toml: grafana_direct обязан предъявлять caller token "
        "общего сервиса"
    ) in payload["errors"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Канонический launcher: UV RUN python -m azurpilot", True),
        ("UV RUN и python -m azurpilot запрещены как обход", False),
        ("Используй azur mcp; uv run разрешён для тестов", False),
        ("uv run --locked azur integrations coderabbit status", True),
        ("uv run pytest -k azurpilot-development", False),
        ("uv run\npython -m azurpilot integrations coderabbit status", False),
    ],
)
def test_operator_launcher_detection_uses_tokens_and_negation(
    text: str, expected: bool
) -> None:
    assert gate._contains_prohibited_operator_launcher(text) is expected


_GITHUB_CREDENTIAL_NAME = "GITHUB_PERSONAL_" + "ACCESS_" + "TOKEN"
_GITHUB_PORTS = '    ports:\n      - "127.0.0.1:8779:8082/tcp"\n'
_READ_ONLY_LINE = '      - "--read-only"\n'


def _mutated_repository(tmp_path: Path, old: str, new: str) -> Path:
    """Скопировать Compose owner и Codex config в корень с намеренной поломкой."""

    source = (REPOSITORY_ROOT / gate._COMPOSE_PATH).read_text(encoding="utf-8")
    assert source.count(old) == 1
    compose = tmp_path / gate._COMPOSE_PATH
    compose.parent.mkdir(parents=True, exist_ok=True)
    compose.write_text(source.replace(old, new, 1), encoding="utf-8")

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(exist_ok=True)
    (codex_dir / "config.toml").write_text(
        (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return tmp_path


def _tools_flag_line(tools: tuple[str, ...]) -> str:
    return '      - "--tools=' + ",".join(tools) + '"' + "\n"


def test_contract_declares_github_read_only_catalog():
    assert len(gate.GITHUB_MCP_READ_ONLY_TOOLS) == 8
    assert "github-mcp" in gate.SHARED_MCP_SERVICES


def test_contract_rejects_github_service_without_server_side_read_only(tmp_path: Path):
    """GitHub MCP обязан ограничивать каталог серверно, а не только клиентом."""

    root = _mutated_repository(
        tmp_path, _READ_ONLY_LINE, '      - "--disable-write"' + "\n"
    )

    payload = gate.check(root)

    assert any(
        "github-mcp обязан запускать provider с server-side --read-only" in error
        for error in payload["errors"]
    )


def test_contract_rejects_github_tool_catalog_widening(tmp_path: Path):
    """Ни toolsets, ни mutation tool не могут заменить exact read-only allowlist."""

    root = _mutated_repository(
        tmp_path,
        _tools_flag_line(gate.GITHUB_MCP_READ_ONLY_TOOLS),
        '      - "--toolsets=all"' + "\n",
    )

    payload = gate.check(root)

    assert any(
        "обязан ограничивать каталог exact --tools, а не toolsets" in error
        for error in payload["errors"]
    )

    widened = (*gate.GITHUB_MCP_READ_ONLY_TOOLS, "create_repository")
    root = _mutated_repository(
        tmp_path / "widened",
        _tools_flag_line(gate.GITHUB_MCP_READ_ONLY_TOOLS),
        _tools_flag_line(widened),
    )

    payload = gate.check(root)

    assert any(
        "обязан публиковать ровно exact read-only allowlist без mutation tools"
        in error
        for error in payload["errors"]
    )


def test_contract_rejects_github_credential_inside_container(tmp_path: Path):
    """GitHub identity принадлежит клиенту и не должна попадать в container."""

    credential_line = "    environment:\n      " + _GITHUB_CREDENTIAL_NAME + ': ""\n'
    root = _mutated_repository(
        tmp_path, _GITHUB_PORTS, credential_line + _GITHUB_PORTS
    )

    payload = gate.check(root)

    assert any(
        "github-mcp не должен получать GitHub credential" in error
        for error in payload["errors"]
    )


def test_contract_rejects_github_readiness_contract_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Readiness общих services обязан совпадать с loopback публикацией Compose."""

    root = _mutated_repository(
        tmp_path,
        '      - "127.0.0.1:8779:8082/tcp"' + "\n",
        '      - "127.0.0.1:8781:8082/tcp"' + "\n",
    )

    payload = gate.check(root)

    assert any(
        "обязан публиковаться только как 127.0.0.1:8779:8082/tcp" in error
        for error in payload["errors"]
    )

    monkeypatch.setattr(gate, "SHARED_MCP_EXTERNAL_READINESS", dict())
    payload = gate.check(root)

    assert any(
        "readiness-контракт github-mcp должен совпадать с loopback публикацией Compose"
        in error
        for error in payload["errors"]
    )
