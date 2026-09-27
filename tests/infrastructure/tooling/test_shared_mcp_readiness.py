"""Контракт готовности общих внешних MCP HTTP services.

GitHub MCP публикуется образом без shell, поэтому его readiness подтверждает
repository-owned loopback probe: endpoint обязан отказать запросу без bearer.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from azurpilot.tooling.contracts import CapabilityStatus
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.infrastructure import (
    DOCKERHUB_MCP_BUILD_INPUTS,
    DOCKERHUB_MCP_BUILD_LABEL,
    DOCKERHUB_MCP_IMAGE_TAG_ENVIRONMENT_KEY,
    SHARED_MCP_CALLER_TOKEN_ENVIRONMENT_KEYS,
    SHARED_MCP_EXTERNAL_READINESS,
    SHARED_MCP_PROFILE,
    SHARED_MCP_SERVICES,
    InfrastructureService,
)

_COMPOSE_RELATIVE = Path("infrastructure") / "observability" / "compose.yaml"


def _repository(root: Path) -> Path:
    """Подготовить минимальный корень репозитория для чтения состояния."""

    compose = root / _COMPOSE_RELATIVE
    compose.parent.mkdir(parents=True)
    compose.write_text("name: azurpilot-infrastructure\n", encoding="utf-8")
    for relative in DOCKERHUB_MCP_BUILD_INPUTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(relative.as_posix().encode("utf-8"))
    (root / ".env").write_text(
        "".join(
            f"{name}={'x' * 12}\n"
            for name in SHARED_MCP_CALLER_TOKEN_ENVIRONMENT_KEYS
        ),
        encoding="utf-8",
    )
    return root


def _record(
    service: str, *, health: str, build_tag: str | None = None
) -> dict[str, object]:
    record: dict[str, object] = {
        "Name": f"azurpilot-infrastructure-{service}-1",
        "Service": service,
        "State": "running",
        "Health": health,
    }
    if build_tag is not None:
        record["Labels"] = f"{DOCKERHUB_MCP_BUILD_LABEL}={build_tag}"
    return record


def _records(root: Path, *, health: str) -> list[dict[str, object]]:
    tag = InfrastructureService._dockerhub_mcp_image_tag(root)
    return [
        _record(
            service,
            health=health,
            build_tag=tag if service == "dockerhub-mcp" else None,
        )
        for service in SHARED_MCP_SERVICES
    ]


class _Runner:
    """Дубль runner-а: отдаёт заранее заданный вывод `compose ps`."""

    def __init__(
        self, records: list[dict[str, object]], *, fail_actions: tuple[str, ...] = ()
    ) -> None:
        self.records = records
        self.fail_actions = set(fail_actions)
        self.specs: list[object] = []

    def run(self, spec: object) -> SimpleNamespace:
        self.specs.append(spec)
        argv = tuple(getattr(spec, "argv", ()))
        if any(action in self.fail_actions for action in argv):
            return SimpleNamespace(
                ok=False,
                stdout="",
                stderr="",
                timed_out=False,
            )
        return SimpleNamespace(
            ok=True,
            stdout="\n".join(json.dumps(item) for item in self.records),
            stderr="",
            timed_out=False,
        )


@pytest.fixture(autouse=True)
def _docker_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        InfrastructureService, "_docker", staticmethod(lambda: Path("/bin/true"))
    )


def test_readiness_registry_uses_only_declared_shared_services():
    assert SHARED_MCP_EXTERNAL_READINESS
    assert set(SHARED_MCP_EXTERNAL_READINESS) <= set(SHARED_MCP_SERVICES)
    assert SHARED_MCP_PROFILE == "external-mcp"


def test_caller_auth_probe_accepts_only_fail_closed_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SHARED_MCP_EXTERNAL_READINESS["github-mcp"]

    def _harden(*, status: int) -> None:
        def _deny(_request: object, timeout: float = 0.0) -> object:
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8779/mcp", status, "Denied", {}, None
            )

        monkeypatch.setattr(
            "urllib.request.build_opener",
            lambda *_handlers: SimpleNamespace(open=_deny),
        )

    # Отказ вызывающему без bearer подтверждает, что caller auth включена.
    _harden(status=401)
    assert InfrastructureService._caller_auth_probe(target) is True

    _harden(status=403)
    assert InfrastructureService._caller_auth_probe(target) is True

    # Любой другой HTTP-ответ отказом caller auth не является.
    for status in (404, 500):
        _harden(status=status)
        assert InfrastructureService._caller_auth_probe(target) is False

    class _OpenResponse:
        """Открытый ответ без отказа: он не подтверждает readiness."""

        def __enter__(self) -> object:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    def _allow(_request: object, timeout: float = 0.0) -> object:
        return _OpenResponse()

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *_handlers: SimpleNamespace(open=_allow),
    )
    assert InfrastructureService._caller_auth_probe(target) is False

    def _unreachable(_request: object, timeout: float = 0.0) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *_handlers: SimpleNamespace(open=_unreachable),
    )
    assert InfrastructureService._caller_auth_probe(target) is False

    def _broken(_request: object, timeout: float = 0.0) -> object:
        raise OSError("network unreachable")

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *_handlers: SimpleNamespace(open=_broken),
    )
    assert InfrastructureService._caller_auth_probe(target) is False


def test_shared_status_requires_loopback_probe_without_healthcheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    records = _records(root, health="")
    records[0]["Health"] = "healthy"
    records[1]["Health"] = "healthy"
    service = InfrastructureService(_Runner(records))

    monkeypatch.setattr(
        InfrastructureService, "_caller_auth_probe", staticmethod(lambda *a, **k: False)
    )
    failed = service.shared_mcp_status(root)

    assert failed.state is CapabilityStatus.FAILED
    assert failed.services == ("grafana-mcp", "dockerhub-mcp")
    assert any(
        "github-mcp: проверка аутентификации вызывающего клиента" in line
        for line in failed.diagnostics
    )

    monkeypatch.setattr(
        InfrastructureService, "_caller_auth_probe", staticmethod(lambda *a, **k: True)
    )
    ready = service.shared_mcp_status(root)

    assert ready.state is CapabilityStatus.READY
    assert ready.services == SHARED_MCP_SERVICES
    assert ready.diagnostics == ()
    assert ready.build_confirmed is False


def test_shared_start_builds_before_starting_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start обязан пересобрать repository-owned Docker Hub image до `up`."""

    root = _repository(tmp_path)
    records = _records(root, health="healthy")
    runner = _Runner(records)
    service = InfrastructureService(runner)
    monkeypatch.setattr(
        InfrastructureService, "_caller_auth_probe", staticmethod(lambda *a, **k: True)
    )

    outcome = service.ensure_shared_mcp_started(root)

    assert outcome.state is CapabilityStatus.READY
    assert outcome.build_confirmed is True
    commands = [tuple(getattr(spec, "argv", ())) for spec in runner.specs]
    build_index = next(index for index, argv in enumerate(commands) if "build" in argv)
    up_indices = [index for index, argv in enumerate(commands) if "up" in argv]
    assert len(up_indices) == 2
    up_index = up_indices[0]
    assert build_index < up_index
    assert commands[build_index][commands[build_index].index("build") :] == (
        "build",
        "--pull",
        "dockerhub-mcp",
    )
    assert commands[up_index][commands[up_index].index("up") :] == (
        "up",
        "--detach",
        "--wait",
        "dockerhub-mcp",
    )
    assert commands[up_indices[1]][commands[up_indices[1]].index("up") :] == (
        "up",
        "--detach",
        "--wait",
        "grafana-mcp",
        "github-mcp",
    )
    build_environment = getattr(runner.specs[build_index], "env", {})
    assert build_environment[DOCKERHUB_MCP_IMAGE_TAG_ENVIRONMENT_KEY] == (
        InfrastructureService._dockerhub_mcp_image_tag(root)
    )


def test_shared_start_fails_closed_when_repository_build_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Healthy старый container не может скрыть ошибку актуальной сборки."""

    root = _repository(tmp_path)
    records = _records(root, health="healthy")
    runner = _Runner(records, fail_actions=("build",))
    service = InfrastructureService(runner)

    with pytest.raises(ToolingError):
        service.ensure_shared_mcp_started(root)

    commands = [tuple(getattr(spec, "argv", ())) for spec in runner.specs]
    assert any("build" in argv for argv in commands)
    assert not any("up" in argv for argv in commands)


def test_shared_status_rejects_stale_dockerhub_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Изменение входа при том же upstream commit не маскируется старым image."""

    root = _repository(tmp_path)
    records = _records(root, health="healthy")
    old_tag = InfrastructureService._dockerhub_mcp_image_tag(root)
    (root / DOCKERHUB_MCP_BUILD_INPUTS[1]).write_bytes(b"changed build input\n")
    assert InfrastructureService._dockerhub_mcp_image_tag(root) != old_tag
    monkeypatch.setattr(
        InfrastructureService, "_caller_auth_probe", staticmethod(lambda *a, **k: True)
    )

    outcome = InfrastructureService(_Runner(records)).shared_mcp_status(root)

    assert outcome.state is CapabilityStatus.FAILED
    assert "dockerhub-mcp" not in outcome.services
    assert any("происхождение образа" in line for line in outcome.diagnostics)


def test_shared_start_does_not_confirm_build_when_readiness_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Успешный build не становится подтверждением при сломанном readiness."""

    root = _repository(tmp_path)
    records = _records(root, health="healthy")
    runner = _Runner(records)
    monkeypatch.setattr(
        InfrastructureService, "_caller_auth_probe", staticmethod(lambda *a, **k: False)
    )

    outcome = InfrastructureService(runner).ensure_shared_mcp_started(root)

    assert outcome.state is CapabilityStatus.FAILED
    assert outcome.build_confirmed is False
