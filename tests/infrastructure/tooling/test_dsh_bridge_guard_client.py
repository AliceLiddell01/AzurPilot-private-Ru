"""Поведение стража клиента AzurPilot Harness на точном контракте DeepSeek Harness.

Тесты запускают настоящий `azurpilot-bridge-guard.mjs` в `node` с подставным
контекстом плагина и подставным владельцем проверки. Подставной контекст даёт
именно тот контракт, которым пользуется страж: монотонный `ctx.tools.guard()`,
каталог `ctx.tools.schemas()` и `ctx.effect()`. Поэтому проверяются реальные
решения стража и настоящий документ аттестации, который читает запускатель.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import azurpilot.tooling.dsh as dsh_tooling
from azurpilot.dsh import (
    AZUR_EXECUTABLE_ENV_VAR,
    BRIDGE_GUARD_PATH,
    CHECKOUT_ROOT_ENV_VAR,
    IDENTITY_ENV_VARS,
    READINESS_FILE_ENV_VAR,
    READINESS_NONCE_ENV_VAR,
    READINESS_TIMEOUT_ENV_VAR,
    SOURCE_REVISION_ENV_VAR,
)
from azurpilot.tooling.contracts import (
    DshBridgeFamilyRecord,
    DshBridgeGeneration,
)
from azurpilot.tooling.dsh import DshBridgeService
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.result import ResultCode
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_IDENTITY_PROTOCOL,
    BRIDGE_ROUTES,
    BridgeSourceIdentity,
    bridge_endpoint,
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node недоступен: контракт стража проверяется только при наличии runtime",
)

REVISION = "3" * 40
HARNESS = """
import { pathToFileURL } from 'node:url'
import { readFileSync, writeFileSync } from 'node:fs'

const [guardPath, specPath] = process.argv.slice(2)
const spec = JSON.parse(readFileSync(specPath, 'utf8'))

const guards = []
const disposers = []
const announced = []
const ctx = {
  logger: { info: (message) => announced.push(String(message)) },
  effect: (factory) => {
    disposers.push(factory())
  },
  tools: {
    guard: (check) => guards.push(check),
    schemas: () => spec.tools.map((name) => ({ name })),
  },
}

let applyError = null
try {
  const guard = await import(pathToFileURL(guardPath).href)
  await guard.apply(ctx)
} catch (error) {
  applyError = String(error?.message ?? error)
}

const decide = (name) => {
  const check = guards[0]
  return check === undefined ? 'нет регистрации' : (check({ name }) ?? null)
}
const callsAt = () =>
  readFileSync(spec.counter, 'utf8').split('\\n').filter(Boolean).length
const counts = [callsAt()]

const decisions = {
  dev: decide('mcp__azurpilot-dev__dev_get_contract'),
  game: decide('mcp__azurpilot-game__game_get_contract'),
  github: decide('mcp__github__search_code'),
}
counts.push(callsAt())
for (let index = 0; index < 3; index += 1) decide('mcp__azurpilot-dev__dev_get_contract')
counts.push(callsAt())

for (const dispose of disposers) dispose()
await new Promise((resolve) => setTimeout(resolve, 500))
counts.push(callsAt())

const document = spec.attestation
let attestation = null
try {
  attestation = JSON.parse(readFileSync(document, 'utf8'))
} catch {
  attestation = null
}

process.stdout.write(
  JSON.stringify({
    guards: guards.length,
    disposers: disposers.length,
    announced: announced.length,
    applyError,
    decisions,
    counts,
    attestation,
  }),
)
"""


def _identity(server_name: str) -> BridgeSourceIdentity:
    return BridgeSourceIdentity(
        identity_protocol=BRIDGE_IDENTITY_PROTOCOL,
        server_name=server_name,
        server_version="1.0.0",
        source_revision=REVISION,
        source_set_digest="a" * 64,
        contract_revision="c" * 64,
        tool_catalog_sha256="d" * 64,
        capability_catalog_sha256="e" * 64,
    )


def _run_guard(
    tmp_path: Path,
    *,
    verdict: str,
    verdict_code: int = 0,
    tools: tuple[str, ...] = (
        "mcp__azurpilot-dev__dev_get_contract",
        "mcp__azurpilot-game__game_get_contract",
    ),
    environment: bool = True,
    readiness_timeout_ms: int | None = None,
) -> dict[str, object]:
    """Запустить настоящий страж в node с подставным владельцем проверки."""

    verdict_file = tmp_path / "verdict.json"
    verdict_file.write_text(verdict, encoding="utf-8")
    counter = tmp_path / "counter.log"
    counter.write_text("", encoding="utf-8")
    attestation = tmp_path / "ready.json"
    executable = tmp_path / "azur"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf 'call\\n' >> {counter}\n"
        f"cat {verdict_file}\n"
        f"exit {verdict_code}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    which_node = shutil.which("node") or "/usr/bin/node"
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "tools": list(tools),
                "counter": str(counter),
                "attestation": str(attestation),
            }
        ),
        encoding="utf-8",
    )
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")

    environ = {
        # PATH клиента — обычный PATH пользовательской сессии: подставной
        # владелец проверки использует обычные утилиты оболочки.
        "PATH": os.pathsep.join(
            [str(Path(which_node).parent), "/usr/local/bin", "/usr/bin", "/bin"]
        ),
        "HOME": str(tmp_path),
        AZUR_EXECUTABLE_ENV_VAR: str(executable),
        READINESS_FILE_ENV_VAR: str(attestation),
        READINESS_NONCE_ENV_VAR: "nonce-current",
    }
    if readiness_timeout_ms is not None:
        environ[READINESS_TIMEOUT_ENV_VAR] = str(readiness_timeout_ms)
    if environment:
        environ.update(
            {
                CHECKOUT_ROOT_ENV_VAR: str(tmp_path),
                SOURCE_REVISION_ENV_VAR: REVISION,
                IDENTITY_ENV_VARS["azurpilot-dev"]: "идентичность-dev",
                IDENTITY_ENV_VARS["azurpilot-game"]: "идентичность-game",
            }
        )

    completed = subprocess.run(
        ["node", str(harness), str(BRIDGE_GUARD_PATH), str(spec)],
        env=environ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _verdict(*families: tuple[str, str]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "ok": all(status == "ready" for _, status in families),
            "code": "OK",
            "state": "ready",
            "message": "вердикт владельца проверки",
            "details": {
                "checkout_root": "/tmp/checkout",
                "recorded_revision": REVISION,
                "current_revision": REVISION,
                "bridge_endpoint": bridge_endpoint(),
                "families": [
                    {
                        "server_name": server,
                        "status": status,
                        "reason_code": (
                            "AZURPILOT_DSH_SOURCE_MATCHES"
                            if status == "ready"
                            else "AZURPILOT_DSH_SOURCE_DRIFT"
                        ),
                        "mismatched_fields": []
                        if status == "ready"
                        else ["source_revision"],
                        "message": f"состояние {status}",
                    }
                    for server, status in families
                ],
            },
        },
        ensure_ascii=False,
    )


def test_guard_allows_calls_while_generation_matches(tmp_path: Path) -> None:
    outcome = _run_guard(
        tmp_path,
        verdict=_verdict(
            ("azurpilot-dev", "ready"),
            ("azurpilot-game", "ready"),
        ),
    )

    assert outcome["applyError"] is None
    assert outcome["decisions"] == {"dev": None, "game": None, "github": None}
    assert outcome["guards"] == 1
    assert outcome["disposers"] == 1
    attestation = outcome["attestation"]
    assert isinstance(attestation, dict)
    assert attestation["nonce"] == "nonce-current"
    assert attestation["guard_installed"] is True
    assert attestation["verification"] == "ready"
    assert attestation["mcp_families"] == ["azurpilot-dev", "azurpilot-game"]
    assert attestation["source_revision"] == REVISION


def test_guard_narrows_denial_to_the_drifted_family(tmp_path: Path) -> None:
    outcome = _run_guard(
        tmp_path,
        verdict=_verdict(
            ("azurpilot-dev", "drift"),
            ("azurpilot-game", "ready"),
        ),
        verdict_code=1,
    )

    decisions = outcome["decisions"]
    assert isinstance(decisions, dict)
    assert decisions["dev"] is not None
    assert "AZURPILOT_DSH_SOURCE_DRIFT" in str(decisions["dev"])
    assert decisions["game"] is None
    assert decisions["github"] is None
    attestation = outcome["attestation"]
    assert isinstance(attestation, dict)
    assert attestation["verification"] == "drift"


def test_guard_denies_both_families_on_shared_drift(tmp_path: Path) -> None:
    outcome = _run_guard(
        tmp_path,
        verdict=_verdict(
            ("azurpilot-dev", "drift"),
            ("azurpilot-game", "drift"),
        ),
        verdict_code=1,
    )

    decisions = outcome["decisions"]
    assert isinstance(decisions, dict)
    assert decisions["dev"] is not None
    assert decisions["game"] is not None
    assert decisions["github"] is None


@pytest.mark.parametrize(
    "verdict",
    [
        pytest.param("не json", id="владелец-вернул-не-json"),
        pytest.param("", id="владелец-вернул-пустой-ответ"),
        pytest.param(
            json.dumps({"schema_version": 1, "ok": True, "details": {}}),
            id="владелец-без-семейств",
        ),
    ],
)
def test_guard_stays_fail_closed_on_unavailable_verdict(
    tmp_path: Path, verdict: str
) -> None:
    outcome = _run_guard(tmp_path, verdict=verdict, verdict_code=1)

    decisions = outcome["decisions"]
    assert isinstance(decisions, dict)
    assert decisions["dev"] is not None
    assert decisions["game"] is not None
    attestation = outcome["attestation"]
    assert isinstance(attestation, dict)
    assert attestation["verification"] == "unavailable"


def test_guard_denies_when_launcher_environment_is_missing(tmp_path: Path) -> None:
    outcome = _run_guard(tmp_path, verdict=_verdict(), environment=False)

    decisions = outcome["decisions"]
    assert isinstance(decisions, dict)
    assert "AZURPILOT_DSH_GUARD_NOT_PREPARED" in str(decisions["dev"])
    assert "AZURPILOT_DSH_GUARD_NOT_PREPARED" in str(decisions["game"])
    assert "AZURPILOT_DSH_GUARD_NOT_PREPARED" in str(outcome["applyError"])
    attestation = outcome["attestation"]
    assert isinstance(attestation, dict)
    assert attestation["guard_installed"] is False
    assert attestation["reason_code"] == "AZURPILOT_DSH_GUARD_NOT_PREPARED"


def test_guard_denies_when_mandatory_client_is_absent(tmp_path: Path) -> None:
    """Неактивация обязательного клиента закрывает сессию, а не ослабляет запрет."""

    outcome = _run_guard(
        tmp_path,
        verdict=_verdict(
            ("azurpilot-dev", "ready"),
            ("azurpilot-game", "ready"),
        ),
        tools=("mcp__azurpilot-dev__dev_get_contract",),
        readiness_timeout_ms=1500,
    )

    assert "AZURPILOT_DSH_MCP_CLIENT_UNAVAILABLE" in str(outcome["applyError"])
    decisions = outcome["decisions"]
    assert isinstance(decisions, dict)
    assert "AZURPILOT_DSH_MCP_CLIENT_UNAVAILABLE" in str(decisions["dev"])
    attestation = outcome["attestation"]
    assert isinstance(attestation, dict)
    assert attestation["guard_installed"] is False


def test_guard_verifies_outside_the_call_path_and_stops_on_dispose(
    tmp_path: Path,
) -> None:
    """Вызов инструмента не запускает проверку, а dispose не оставляет процесс."""

    outcome = _run_guard(
        tmp_path,
        verdict=_verdict(
            ("azurpilot-dev", "ready"),
            ("azurpilot-game", "ready"),
        ),
    )

    counts = outcome["counts"]
    assert isinstance(counts, list)
    assert counts[0] >= 1, "проверка владельца должна пройти до аттестации"
    # Решения guard синхронные: три дополнительных вызова ничего не запускают.
    assert counts[1] == counts[0]
    assert counts[2] == counts[0]
    # После остановки плагина фоновый watchdog не продолжает работу.
    assert counts[3] == counts[0]


def test_guard_attestation_is_accepted_by_the_launcher(tmp_path: Path) -> None:
    """Документ, который пишет страж, подтверждает запуск продукта."""

    outcome = _run_guard(
        tmp_path,
        verdict=_verdict(
            ("azurpilot-dev", "ready"),
            ("azurpilot-game", "ready"),
        ),
    )
    channel = dsh_tooling._ReadinessChannel(
        directory=tmp_path,
        document=tmp_path / "ready.json",
        nonce="nonce-current",
    )
    routes = {route.server_name: route for route in BRIDGE_ROUTES.values()}
    generation = DshBridgeGeneration(
        checkout_root=str(tmp_path),
        source_revision=REVISION,
        source_state="modified",
        profile=dsh_tooling.DEFAULT_DSH_PROFILE,
        dsh_package=dsh_tooling.DEFAULT_DSH_PACKAGE,
        overlay_path="overlay",
        guard_path=str(BRIDGE_GUARD_PATH),
        bridge_endpoint=bridge_endpoint(),
        families=tuple(
            DshBridgeFamilyRecord(
                server_name=name,
                endpoint=routes[name].bridge_url,
                identity=_identity(name),
            )
            for name in ("azurpilot-dev", "azurpilot-game")
        ),
    )
    service = DshBridgeService()
    document = (tmp_path / "ready.json").read_text(encoding="utf-8")

    service._require_readiness(channel, generation, document)

    wrong_nonce = json.loads(document)
    wrong_nonce["nonce"] = "чужое-значение"
    with pytest.raises(ToolingError) as failure:
        service._require_readiness(channel, generation, json.dumps(wrong_nonce))
    assert failure.value.code is ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE
