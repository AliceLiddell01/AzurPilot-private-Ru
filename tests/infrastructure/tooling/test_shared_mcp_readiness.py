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
from azurpilot.tooling.infrastructure import (
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
    (root / ".env").write_text(
        "".join(
            f"{name}={'x' * 12}\n"
            for name in SHARED_MCP_CALLER_TOKEN_ENVIRONMENT_KEYS
        ),
        encoding="utf-8",
    )
    return root


def _record(service: str, *, health: str) -> dict[str, object]:
    return {
        "Name": f"azurpilot-infrastructure-{service}-1",
        "Service": service,
        "State": "running",
        "Health": health,
    }


class _Runner:
    """Дубль runner-а: отдаёт заранее заданный вывод `compose ps`."""

    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records
        self.specs: list[object] = []

    def run(self, spec: object) -> SimpleNamespace:
        self.specs.append(spec)
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

    def _deny(_request: object, timeout: float = 0.0) -> object:
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8779/mcp", 401, "Unauthorized", {}, None
        )

    monkeypatch.setattr("urllib.request.urlopen", _deny)
    assert InfrastructureService._caller_auth_probe(target) is True

    class _OpenResponse:
        """Открытый ответ без отказа: он не подтверждает readiness."""

        def __enter__(self) -> object:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    def _allow(_request: object, timeout: float = 0.0) -> object:
        return _OpenResponse()

    monkeypatch.setattr("urllib.request.urlopen", _allow)
    assert InfrastructureService._caller_auth_probe(target) is False

    def _unreachable(_request: object, timeout: float = 0.0) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _unreachable)
    assert InfrastructureService._caller_auth_probe(target) is False


def test_shared_status_requires_loopback_probe_without_healthcheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    records = [_record(service, health="") for service in SHARED_MCP_SERVICES]
    records[0]["Health"] = "healthy"
    records[1]["Health"] = "healthy"
    service = InfrastructureService(_Runner(records))

    monkeypatch.setattr(
        InfrastructureService, "_caller_auth_probe", staticmethod(lambda *a, **k: False)
    )
    failed = service.shared_mcp_status(root)

    assert failed.state is CapabilityStatus.FAILED
    assert failed.services == ("grafana-mcp", "dockerhub-mcp")
    assert any("github-mcp: loopback caller-auth probe" in line for line in failed.diagnostics)

    monkeypatch.setattr(
        InfrastructureService, "_caller_auth_probe", staticmethod(lambda *a, **k: True)
    )
    ready = service.shared_mcp_status(root)

    assert ready.state is CapabilityStatus.READY
    assert ready.services == SHARED_MCP_SERVICES
    assert ready.diagnostics == ()
