from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import dev_tools.integration_contract_gate as gate
from azurpilot.integrations.adapters import DOCKER_HUB_BLOCKED_TOOLS
from azurpilot.integrations.config import DEFAULTS, SHARED_MCP_ENDPOINTS
from azurpilot.tooling.infrastructure import DOCKERHUB_MCP_IMAGE_TAG_ENVIRONMENT_KEY
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


def test_dockerhub_mcp_lock_transport_is_available_in_both_install_stages():
    """Закреплённая Git-зависимость доступна в обеих стадиях установки."""

    dockerfile = (
        REPOSITORY_ROOT
        / "infrastructure"
        / "observability"
        / "mcp"
        / "dockerhub-mcp"
        / "Dockerfile"
    ).read_text(encoding="utf-8")
    lockfile = (
        REPOSITORY_ROOT
        / "infrastructure"
        / "observability"
        / "mcp"
        / "dockerhub-mcp"
        / "package-lock.json"
    ).read_text(encoding="utf-8")

    assert "git+ssh://git@github.com/modelcontextprotocol/specification.git#" in lockfile
    assert dockerfile.count("apk add --no-cache") >= 2
    assert dockerfile.count("apk add --no-cache curl git") == 1
    assert dockerfile.count("apk add --no-cache git") == 1
    assert dockerfile.count("git config --global --add") == 4
    assert dockerfile.count('insteadOf "ssh://git@github.com/"') == 2
    assert dockerfile.count('insteadOf "git@github.com:"') == 2
    assert "npm ci --no-audit --no-fund" in dockerfile
    assert "npm ci --omit=dev --no-audit --no-fund" in dockerfile


def test_dockerhub_mcp_image_uses_content_derived_runtime_tag():
    """Compose обязан связывать Docker Hub image с текущими входами сборки."""

    document = yaml.safe_load(
        (REPOSITORY_ROOT / gate._COMPOSE_PATH).read_text(encoding="utf-8")
    )
    service = document["services"]["dockerhub-mcp"]
    image = service["image"]
    label = service["labels"]["azurpilot.dockerhub-mcp.build-tag"]
    marker = f"${{{DOCKERHUB_MCP_IMAGE_TAG_ENVIRONMENT_KEY}:-"

    assert marker in image
    assert marker in label


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


def _mutated_github_block(tmp_path: Path, transform) -> Path:
    """Изменить только блок service github-mcp по границам отступов Compose."""

    source = (REPOSITORY_ROOT / gate._COMPOSE_PATH).read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    start = lines.index("  " + gate.GITHUB_MCP_SERVICE + ":\n")
    end = next(
        index
        for index in range(start + 1, len(lines))
        if lines[index].strip() and not lines[index].startswith("    ")
    )
    mutated = transform(lines[start:end])
    assert mutated != lines[start:end]

    compose = tmp_path / gate._COMPOSE_PATH
    compose.parent.mkdir(parents=True, exist_ok=True)
    compose.write_text(
        "".join(lines[:start] + mutated + lines[end:]), encoding="utf-8"
    )

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(exist_ok=True)
    (codex_dir / "config.toml").write_text(
        (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return tmp_path


def test_contract_declares_github_read_only_catalog():
    """Объявленный allowlist обязан совпасть с фактическим Compose command."""

    document = yaml.safe_load(
        (REPOSITORY_ROOT / gate._COMPOSE_PATH).read_text(encoding="utf-8")
    )
    service = document["services"][gate.GITHUB_MCP_SERVICE]
    flags = [
        item
        for item in service["command"]
        if item.startswith("--tools=")
    ]
    assert len(flags) == 1
    assert tuple(flags[0][len("--tools=") :].split(",")) == gate.GITHUB_MCP_READ_ONLY_TOOLS
    assert sorted(gate.GITHUB_MCP_READ_ONLY_TOOLS) == sorted(
        set(gate.GITHUB_MCP_READ_ONLY_TOOLS)
    )
    assert set(gate.GITHUB_MCP_READ_ONLY_TOOLS).isdisjoint(
        {
            "create_issue",
            "create_repository",
            "merge_pull_request",
            "push_files",
            "update_issue",
        }
    )
    assert gate.SHARED_MCP_EXTERNAL_READINESS[gate.GITHUB_MCP_SERVICE] == (
        "127.0.0.1",
        gate.GITHUB_MCP_PORT,
        "/mcp",
    )
    assert service["ports"] == [f"127.0.0.1:{gate.GITHUB_MCP_PORT}:{gate.GITHUB_MCP_CONTAINER_PORT}/tcp"]


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
        "ровно exact read-only allowlist одним флагом --tools без mutation tools"
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

    monkeypatch.setattr(gate, "SHARED_MCP_EXTERNAL_READINESS", {})
    payload = gate.check(root)

    assert any(
        "readiness-контракт github-mcp должен совпадать с loopback публикацией Compose"
        in error
        for error in payload["errors"]
    )


def test_contract_rejects_repeated_tools_flag_widening(tmp_path: Path):
    """Повторный `--tools=` накапливается provider-ом и расширяет каталог."""

    root = _mutated_repository(
        tmp_path,
        _tools_flag_line(gate.GITHUB_MCP_READ_ONLY_TOOLS),
        _tools_flag_line(gate.GITHUB_MCP_READ_ONLY_TOOLS)
        + _tools_flag_line(("search_repositories",)),
    )

    payload = gate.check(root)

    assert any(
        "ровно exact read-only allowlist одним флагом --tools" in error
        for error in payload["errors"]
    )


def test_contract_rejects_duplicated_tool_inside_allowlist(tmp_path: Path):
    """Каталог обязан быть точным набором без повторов внутри одного флага."""

    duplicated = (*gate.GITHUB_MCP_READ_ONLY_TOOLS, "search_code")
    root = _mutated_repository(
        tmp_path,
        _tools_flag_line(gate.GITHUB_MCP_READ_ONLY_TOOLS),
        _tools_flag_line(duplicated),
    )

    payload = gate.check(root)

    assert any(
        "ровно exact read-only allowlist одним флагом --tools" in error
        for error in payload["errors"]
    )


@pytest.mark.parametrize(
    "key",
    ["env_file", "secrets"],
)
def test_contract_rejects_github_credential_channels(tmp_path: Path, key: str):
    """Credential нельзя провести в container мимо `environment`."""

    injection = f"    {key}:\n      - .env\n" if key == "env_file" else (
        f"    {key}:\n      - github_token\n"
    )
    root = _mutated_repository(
        tmp_path, _GITHUB_PORTS, injection + _GITHUB_PORTS
    )

    payload = gate.check(root)

    assert any(
        f"github-mcp не должен принимать {key}" in error
        for error in payload["errors"]
    )


def test_contract_rejects_network_mode_on_shared_services(tmp_path: Path):
    """network_mode отменяет loopback-публикацию Compose."""

    root = _mutated_repository(
        tmp_path, _GITHUB_PORTS, "    network_mode: host\n" + _GITHUB_PORTS
    )

    payload = gate.check(root)

    assert any(
        "github-mcp обязан жить в loopback-публикации Compose" in error
        for error in payload["errors"]
    )

    grafana_port = '      - "127.0.0.1:8777:8000/tcp"' + "\n"
    root = _mutated_repository(
        tmp_path / "grafana",
        grafana_port,
        "    network_mode: host\n" + grafana_port,
    )

    payload = gate.check(root)

    assert any(
        "grafana-mcp обязан жить в loopback-публикации Compose" in error
        for error in payload["errors"]
    )


def test_contract_rejects_github_runtime_hardening_regression(tmp_path: Path):
    """read-only rootfs требует tmpfs, а сервис обязан перезапускаться."""

    root = _mutated_github_block(
        tmp_path,
        lambda block: [line for line in block if line.strip() != "- /tmp"],
    )
    payload = gate.check(root)

    assert any(
        "обязан иметь tmpfs для временных путей" in error
        for error in payload["errors"]
    )

    root = _mutated_github_block(
        tmp_path / "restart",
        lambda block: [line for line in block if "restart:" not in line],
    )
    payload = gate.check(root)

    assert any(
        "github-mcp обязан перезапускаться как долгоживущий service" in error
        for error in payload["errors"]
    )
