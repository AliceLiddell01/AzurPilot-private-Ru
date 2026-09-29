from __future__ import annotations

import io
import json
import re
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import azurpilot.tooling.dsh as dsh_tooling
import azurpilot.tooling.dsh_composition as composition
from azurpilot.cli import build_parser
from azurpilot.dsh import (
    AZUR_EXECUTABLE_ENV_VAR,
    BRIDGE_GUARD_PATH,
    BRIDGE_OVERLAY_PATH,
    CHECKOUT_ROOT_ENV_VAR,
    DSH_PACKAGE_ENV_VAR,
    DSH_PROFILE_ENV_VAR,
    IDENTITY_ENV_VARS,
    READINESS_FILE_ENV_VAR,
    READINESS_NONCE_ENV_VAR,
    READINESS_SCHEMA_VERSION,
    READINESS_TIMEOUT_ENV_VAR,
    SOURCE_REVISION_ENV_VAR,
)
from azurpilot.tooling.contracts import (
    DshBridgeFamilyCheck,
    DshBridgeFamilyRecord,
    DshBridgeGeneration,
    DshVerificationDetails,
    OperationState,
)
from azurpilot.tooling.dsh import (
    DEFAULT_DSH_PACKAGE,
    DEFAULT_DSH_PROFILE,
    READINESS_TIMEOUT_SECONDS,
    DshBridgeService,
    load_generation,
)
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.result import ResultCode
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_CALLER_TOKEN_ENV_VAR,
    BRIDGE_IDENTITY_PROTOCOL,
    BRIDGE_ROUTES,
    BridgeSourceIdentity,
    bridge_endpoint,
    serialize_identity,
)

REVISION = "1" * 40
OTHER_REVISION = "2" * 40


class _JavascriptLoader(yaml.SafeLoader):
    """Загрузчик overlay-файла: выражение `!!js` читается как обычная строка."""


_JavascriptLoader.add_constructor(
    "tag:yaml.org,2002:js",
    lambda loader, node: f"!!js {loader.construct_scalar(node)}",
)


def _identity(
    server_name: str,
    *,
    revision: str = REVISION,
    digest_character: str = "a",
) -> BridgeSourceIdentity:
    return BridgeSourceIdentity(
        identity_protocol=BRIDGE_IDENTITY_PROTOCOL,
        server_name=server_name,
        server_version="1.0.0",
        source_revision=revision,
        source_set_digest=digest_character * 64,
        contract_revision="c" * 64,
        tool_catalog_sha256="d" * 64,
        capability_catalog_sha256="e" * 64,
    )


def _generation(
    *,
    revision: str = REVISION,
    dev_digest: str = "a",
    game_digest: str = "b",
) -> DshBridgeGeneration:
    routes = {route.server_name: route for route in BRIDGE_ROUTES.values()}
    return DshBridgeGeneration(
        checkout_root="/tmp/azurpilot-checkout",
        source_revision=revision,
        source_state="clean",
        profile=DEFAULT_DSH_PROFILE,
        dsh_package=DEFAULT_DSH_PACKAGE,
        overlay_path=str(BRIDGE_OVERLAY_PATH),
        guard_path=str(BRIDGE_GUARD_PATH),
        bridge_endpoint=bridge_endpoint(),
        families=(
            DshBridgeFamilyRecord(
                server_name="azurpilot-dev",
                endpoint=routes["azurpilot-dev"].bridge_url,
                identity=_identity(
                    "azurpilot-dev", revision=revision, digest_character=dev_digest
                ),
            ),
            DshBridgeFamilyRecord(
                server_name="azurpilot-game",
                endpoint=routes["azurpilot-game"].bridge_url,
                identity=_identity(
                    "azurpilot-game", revision=revision, digest_character=game_digest
                ),
            ),
        ),
    )


class _FakeResolver:
    """Подменить разрешение корня репозитория без обращения к реальному git."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, repository_root: str | Path | None) -> SimpleNamespace:
        return SimpleNamespace(path=self.root)


def _patch_boundary(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    revision: str,
    dev_digest: str,
    game_digest: str,
) -> DshBridgeService:
    live = {
        "azurpilot-dev": _identity(
            "azurpilot-dev", revision=revision, digest_character=dev_digest
        ),
        "azurpilot-game": _identity(
            "azurpilot-game", revision=revision, digest_character=game_digest
        ),
    }
    monkeypatch.setattr(dsh_tooling, "RepositoryResolver", lambda: _FakeResolver(root))
    monkeypatch.setattr(
        dsh_tooling, "source_snapshot", lambda resolved: (revision, "clean")
    )
    monkeypatch.setattr(dsh_tooling, "load_bridge_bundle", lambda resolved: object())
    monkeypatch.setattr(
        dsh_tooling,
        "McpSourceReconciler",
        lambda: SimpleNamespace(
            build=lambda resolved, requested_bump=None: SimpleNamespace(bundle=object())
        ),
    )
    monkeypatch.setattr(
        dsh_tooling,
        "expected_bridge_identity",
        lambda bundle, server_name, current: live[server_name],
    )
    return DshBridgeService()


def test_cli_exposes_dsh_prepare_launch_and_verify_routes() -> None:
    parser = build_parser()

    prepare = parser.parse_args(["dsh", "prepare", "--json"])
    assert prepare.dsh_command == "prepare"
    assert prepare.profile == DEFAULT_DSH_PROFILE
    assert prepare.package == DEFAULT_DSH_PACKAGE

    launch = parser.parse_args(
        ["dsh", "launch", "--dsh-arg", "headless", "--dsh-arg", "задача"]
    )
    assert launch.dsh_arguments == ["headless", "задача"]
    assert not hasattr(launch, "generation")

    verify = parser.parse_args(["dsh", "verify", "--generation", "/tmp/ent.json"])
    assert verify.generation == "/tmp/ent.json"


def test_tracked_overlay_registers_exactly_two_bridge_routes() -> None:
    text = BRIDGE_OVERLAY_PATH.read_text(encoding="utf-8")
    overlay = yaml.load(text, Loader=_JavascriptLoader)
    rows = [row for entry in overlay for row in (entry.get("insert") or [])]
    clients = [row for row in rows if row.get("name") == "@deepseek-ai/dsh-mcp-client"]
    routes = {route.server_name: route for route in BRIDGE_ROUTES.values()}

    assert len(rows) == 3
    assert len(clients) == 2
    assert rows[0]["name"] == "./azurpilot-bridge-guard.mjs"
    assert BRIDGE_GUARD_PATH.exists()
    for row in clients:
        config = row["config"]
        route = routes[config["serverName"]]
        assert config["transport"] == "streamable-http"
        assert config["url"] == route.bridge_url
        assert config["failOnStartupError"] is True
        headers = config["headers"]
        assert BRIDGE_CALLER_TOKEN_ENV_VAR in headers["Authorization"]
        assert (
            IDENTITY_ENV_VARS[config["serverName"]]
            in headers["x-azurpilot-expected-source-identity"]
        )
    assert "module.dev_mcp" not in text
    assert "module.game_mcp" not in text
    assert re.search(r"\b[0-9a-f]{40,64}\b", text) is None


def test_guard_uses_the_python_owned_environment_contract() -> None:
    text = BRIDGE_GUARD_PATH.read_text(encoding="utf-8")

    for variable in (
        CHECKOUT_ROOT_ENV_VAR,
        SOURCE_REVISION_ENV_VAR,
        AZUR_EXECUTABLE_ENV_VAR,
        READINESS_FILE_ENV_VAR,
        READINESS_NONCE_ENV_VAR,
        *IDENTITY_ENV_VARS.values(),
    ):
        assert variable in text
    assert BRIDGE_CALLER_TOKEN_ENV_VAR not in text
    assert "AZURPILOT_DSH_SOURCE_DRIFT" in text
    assert "AZURPILOT_DSH_GUARD_NOT_PREPARED" in text
    assert "AZURPILOT_DSH_MCP_CLIENT_UNAVAILABLE" in text
    # Идентичность источника принадлежит владельцу проверки: страж не считает
    # хеши сам и не дублирует вычисление идентичности.
    assert "createHash" not in text
    assert "sha256" not in text


def test_guard_registers_on_the_monotonic_boundary() -> None:
    """Запрет расхождения источника не должен исполняться в waterfall.

    Точный pin DeepSeek Harness `0.1.7-rc.2` (`dsh-tools`) исполняет монотонные
    guard после расширяемого `tools/pre-execute` и не позволяет ни одному guard
    разрешить вызов, запрещённый другим guard. Поэтому страж обязан
    регистрироваться через `ctx.tools.guard()`, а не через reorderable listener
    `tools/pre-execute`, иначе порядок policy-плагинов смог бы превратить
    доказанное расхождение в разрешение.
    """

    text = BRIDGE_GUARD_PATH.read_text(encoding="utf-8")

    assert "ctx.tools.guard(" in text
    # Регистрации listener в расширяемом waterfall у стража быть не должно.
    assert "ctx.on(" not in text
    assert "ctx.waterfall(" not in text
    assert "tools/pre-execute'," not in text
    # Проверка владельца не выполняется в пути вызова инструмента: решение guard
    # синхронное, а запрос уходит только в фоновом watchdog.
    assert "await generation.confirmVerdict()" in text
    assert "await this.refresh()" in text
    assert "VERIFY_INTERVAL_MS" in text


def test_load_generation_reads_prepare_envelope(tmp_path: Path) -> None:
    generation = _generation()
    envelope = tmp_path / "generation.json"
    envelope.write_text(
        json.dumps(
            {
                "ok": True,
                "code": "OK",
                "state": "ready",
                "message": "генерация",
                "details": generation.model_dump(mode="json"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert load_generation(envelope) == generation
    assert (
        load_generation(
            _write(tmp_path, "raw.json", generation.model_dump(mode="json"))
        )
        == generation
    )

    broken = _write(tmp_path, "broken.json", {"ok": True})
    with pytest.raises(ToolingError) as failure:
        load_generation(broken)
    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED

    with pytest.raises(ToolingError):
        load_generation(tmp_path / "absent.json")


def _families(result: object) -> dict[str, DshBridgeFamilyCheck]:
    details = getattr(result, "details", None)
    assert isinstance(details, DshVerificationDetails)
    return {item.server_name: item for item in details.families}


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def test_verify_fails_closed_without_prepared_environment(tmp_path: Path) -> None:
    service = DshBridgeService(resolver=_FakeResolver(tmp_path))

    with pytest.raises(ToolingError) as failure:
        service.verify(environ={})
    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED

    incomplete = {SOURCE_REVISION_ENV_VAR: REVISION}
    with pytest.raises(ToolingError) as missing_identity:
        service.verify(environ=incomplete)
    assert missing_identity.value.code is ResultCode.TOOLING_PRECONDITION_FAILED

    broken = {
        SOURCE_REVISION_ENV_VAR: REVISION,
        IDENTITY_ENV_VARS["azurpilot-dev"]: "не идентичность",
        IDENTITY_ENV_VARS["azurpilot-game"]: "не идентичность",
    }
    with pytest.raises(ToolingError) as damaged:
        service.verify(environ=broken)
    assert damaged.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_verify_accepts_matching_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _patch_boundary(
        monkeypatch, root=tmp_path, revision=REVISION, dev_digest="a", game_digest="b"
    )

    result = service.verify_generation(_generation())

    assert result.ok is True
    assert result.code is ResultCode.OK
    assert [item.status for item in _families(result).values()] == ["ready", "ready"]


def test_verify_blocks_only_the_family_with_changed_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _patch_boundary(
        monkeypatch, root=tmp_path, revision=REVISION, dev_digest="f", game_digest="b"
    )

    result = service.verify_generation(_generation())
    families = _families(result)

    assert result.ok is False
    assert result.code is ResultCode.MCP_SOURCE_BUNDLE_DRIFT
    assert families["azurpilot-dev"].status == "drift"
    assert families["azurpilot-dev"].mismatched_fields == ("source_set_digest",)
    assert families["azurpilot-game"].status == "ready"


def test_verify_blocks_both_families_when_revision_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _patch_boundary(
        monkeypatch,
        root=tmp_path,
        revision=OTHER_REVISION,
        dev_digest="a",
        game_digest="b",
    )

    result = service.verify_generation(_generation())
    families = _families(result)

    assert result.ok is False
    assert families["azurpilot-dev"].mismatched_fields == ("source_revision",)
    assert families["azurpilot-game"].mismatched_fields == ("source_revision",)
    assert "Git HEAD изменился" in families["azurpilot-dev"].message


def test_generation_environment_drops_ambient_backend_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Внутренние учётные данные проектных сервисов не пересекают границу клиента."""

    service = _patch_boundary(
        monkeypatch, root=tmp_path, revision=REVISION, dev_digest="a", game_digest="b"
    )
    monkeypatch.setattr(
        dsh_tooling.shutil, "which", lambda command: f"/usr/bin/{command}"
    )
    generation = _generation()
    channel = dsh_tooling._ReadinessChannel(
        directory=tmp_path / "channel",
        document=tmp_path / "channel" / "ready.json",
        nonce="nonce-current",
    )
    ambient = {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        "NODE_OPTIONS": "--max-old-space-size=4096",
        "AZURPILOT_DEV_LOCAL_MCP_TOKEN": "ambient-dev-token",
        "AZURPILOT_GAME_LOCAL_MCP_TOKEN": "ambient-game-token",
        "AZURPILOT_MCP_BRIDGE_CALLER_TOKEN": "ambient-caller-token",
        "AZURPILOT_POSTGRES_PASSWORD": "ambient-postgres-secret",
        CHECKOUT_ROOT_ENV_VAR: "/tmp/чужой-checkout",
        SOURCE_REVISION_ENV_VAR: OTHER_REVISION,
        DSH_PACKAGE_ENV_VAR: "@example/stale@0.0.0",
        READINESS_FILE_ENV_VAR: "/tmp/чужой-канал.json",
        READINESS_NONCE_ENV_VAR: "чужое-значение",
        IDENTITY_ENV_VARS["azurpilot-dev"]: "чужая-идентичность",
    }

    environment = service._generation_environment(
        tmp_path,
        generation,
        "caller-token",
        ambient,
        channel,
    )

    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"] == "/home/operator"
    assert environment["NODE_OPTIONS"] == "--max-old-space-size=4096"
    for variable in (
        "AZURPILOT_DEV_LOCAL_MCP_TOKEN",
        "AZURPILOT_GAME_LOCAL_MCP_TOKEN",
        "AZURPILOT_POSTGRES_PASSWORD",
    ):
        assert variable not in environment
    assert environment[BRIDGE_CALLER_TOKEN_ENV_VAR] == "caller-token"
    assert environment[CHECKOUT_ROOT_ENV_VAR] == str(tmp_path)
    assert environment[SOURCE_REVISION_ENV_VAR] == REVISION
    assert environment[DSH_PROFILE_ENV_VAR] == DEFAULT_DSH_PROFILE
    assert environment[DSH_PACKAGE_ENV_VAR] == DEFAULT_DSH_PACKAGE
    assert environment[AZUR_EXECUTABLE_ENV_VAR] == "/usr/bin/azur"
    assert environment[READINESS_FILE_ENV_VAR] == str(channel.document)
    assert environment[READINESS_NONCE_ENV_VAR] == "nonce-current"
    assert (
        int(environment[READINESS_TIMEOUT_ENV_VAR]) < READINESS_TIMEOUT_SECONDS * 1000
    )
    for record in generation.families:
        assert environment[IDENTITY_ENV_VARS[record.server_name]] == (
            serialize_identity(record.identity)
        )
    leaked = {
        "ambient-dev-token",
        "ambient-game-token",
        "ambient-caller-token",
        "ambient-postgres-secret",
    }
    assert not leaked & set(environment.values())


def test_generation_environment_keeps_client_contract_without_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Обычная генерация клиента не зависит от канала подтверждения готовности."""

    service = _patch_boundary(
        monkeypatch, root=tmp_path, revision=REVISION, dev_digest="a", game_digest="b"
    )
    monkeypatch.setattr(
        dsh_tooling.shutil, "which", lambda command: f"/usr/bin/{command}"
    )
    generation = _generation()

    environment = service._generation_environment(
        tmp_path,
        generation,
        "caller-token",
        {"PATH": "/usr/bin"},
    )

    assert environment["PATH"] == "/usr/bin"
    assert environment[CHECKOUT_ROOT_ENV_VAR] == str(tmp_path)
    assert environment[SOURCE_REVISION_ENV_VAR] == REVISION
    assert environment[DSH_PROFILE_ENV_VAR] == DEFAULT_DSH_PROFILE
    assert environment[DSH_PACKAGE_ENV_VAR] == DEFAULT_DSH_PACKAGE
    assert environment[AZUR_EXECUTABLE_ENV_VAR] == "/usr/bin/azur"
    assert environment[BRIDGE_CALLER_TOKEN_ENV_VAR] == "caller-token"
    for record in generation.families:
        assert environment[IDENTITY_ENV_VARS[record.server_name]] == (
            serialize_identity(record.identity)
        )
    assert "AZURPILOT_DEV_LOCAL_MCP_TOKEN" not in environment
    assert "AZURPILOT_GAME_LOCAL_MCP_TOKEN" not in environment
    assert READINESS_FILE_ENV_VAR not in environment


def _readiness_document(
    channel: object, generation: DshBridgeGeneration, **overrides: object
) -> str:
    payload: dict[str, object] = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "nonce": channel.nonce,
        "guard_installed": True,
        "verification": "ready",
        "mcp_families": ["azurpilot-dev", "azurpilot-game"],
        "source_revision": generation.source_revision,
        "checkout_root": generation.checkout_root,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _channel(tmp_path: Path) -> object:
    directory = tmp_path / "channel"
    directory.mkdir(exist_ok=True)
    return dsh_tooling._ReadinessChannel(
        directory=directory,
        document=directory / "ready.json",
        nonce="nonce-current",
    )


def test_readiness_attestation_confirms_product_bound_launch(tmp_path: Path) -> None:
    service = DshBridgeService(resolver=_FakeResolver(tmp_path))
    generation = _generation()
    channel = _channel(tmp_path)

    service._require_readiness(
        channel, generation, _readiness_document(channel, generation)
    )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"nonce": "чужое-значение"}, id="чужой-nonce"),
        pytest.param({"schema_version": 99}, id="неизвестная-версия"),
        pytest.param(
            {
                "guard_installed": False,
                "reason_code": "AZURPILOT_DSH_MCP_CLIENT_UNAVAILABLE",
                "message": "клиенты не активировались",
            },
            id="страж-не-активировался",
        ),
        pytest.param({"verification": "drift"}, id="расхождение-источника"),
        pytest.param({"verification": "unavailable"}, id="проверка-недоступна"),
        pytest.param({"mcp_families": ["azurpilot-dev"]}, id="только-dev"),
        pytest.param({"source_revision": OTHER_REVISION}, id="другая-ревизия"),
        pytest.param({"mcp_families": "azurpilot-dev"}, id="повреждённые-семейства"),
    ],
)
def test_readiness_attestation_refuses_incomplete_activation(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    """Запуск продукта не подтверждается без обязательной активации клиентов."""

    service = DshBridgeService(resolver=_FakeResolver(tmp_path))
    generation = _generation()
    channel = _channel(tmp_path)

    with pytest.raises(ToolingError) as failure:
        service._require_readiness(
            channel, generation, _readiness_document(channel, generation, **overrides)
        )
    assert failure.value.code is ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE


def test_readiness_attestation_refuses_damaged_document(tmp_path: Path) -> None:
    service = DshBridgeService(resolver=_FakeResolver(tmp_path))
    generation = _generation()
    channel = _channel(tmp_path)

    for document in ("", "не json", "[]"):
        with pytest.raises(ToolingError) as failure:
            service._require_readiness(channel, generation, document)
        assert failure.value.code is ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE


class _FakeHarness:
    """Подменить обычный процесс Harness, чтобы проверить контракт запускателя."""

    def __init__(
        self,
        *,
        attestation: str | None,
        poll_code: int | None = None,
        exit_code: int | None = None,
    ) -> None:
        self.attestation = attestation
        self.poll_code = poll_code
        self.exit_code = exit_code
        self.environment: dict[str, str] = {}
        self.arguments: list[str] = []
        self.terminated = False
        self.finished = False

    def poll(self) -> int | None:
        return self.poll_code

    def wait(self, timeout: float | None = None) -> int:
        self.finished = True
        return self.exit_code or 0

    def terminate(self) -> None:
        self.terminated = True
        self.finished = True
        self.poll_code = 0

    def kill(self) -> None:
        self.terminated = True
        self.finished = True
        self.poll_code = 0


def _install_launcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    attestation_of,
    poll_code: int | None = None,
    exit_code: int | None = None,
) -> tuple[DshBridgeService, _FakeHarness, io.StringIO]:
    service = DshBridgeService(resolver=_FakeResolver(tmp_path))
    generation = _generation()
    holder: dict[str, _FakeHarness] = {}

    monkeypatch.setattr(
        service,
        "prepare",
        lambda *args, **kwargs: dsh_tooling.ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message="генерация клиента AzurPilot Harness готова.",
            details=generation,
        ),
    )
    monkeypatch.setattr(service, "_caller_token", lambda root: "caller-token")
    monkeypatch.setattr(
        dsh_tooling.shutil, "which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(service, "_azur_executable", lambda: "/usr/bin/azur")
    monkeypatch.setattr(
        dsh_tooling, "validate_composition", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        dsh_tooling,
        "overlay_composition",
        lambda path=None: (),
    )
    monkeypatch.setattr(dsh_tooling, "load_composition", lambda text: ())
    monkeypatch.setattr(
        dsh_tooling.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )

    def _popen(
        arguments: Sequence[str], executable: str, env: dict[str, str], cwd: str, **_
    ) -> _FakeHarness:
        child = _FakeHarness(attestation=None, poll_code=poll_code, exit_code=exit_code)
        child.environment = dict(env)
        child.arguments = list(arguments)
        if attestation_of is not None:
            document = attestation_of(child)
            if document is not None:
                Path(env[READINESS_FILE_ENV_VAR]).write_text(document, encoding="utf-8")
        holder["child"] = child
        return child

    monkeypatch.setattr(dsh_tooling.subprocess, "Popen", _popen)
    stream = io.StringIO()
    return service, holder, stream


def test_launch_confirms_mandatory_activation_before_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Сессия продукта открывается только с подтверждённой активацией клиентов."""

    channel_nonce: dict[str, str] = {}

    def attestation(child: _FakeHarness) -> str:
        channel_nonce["nonce"] = child.environment[READINESS_NONCE_ENV_VAR]
        return _readiness_document(
            SimpleNamespace(nonce=channel_nonce["nonce"]), _generation()
        )

    service, holder, stream = _install_launcher(
        monkeypatch, tmp_path, attestation_of=attestation, exit_code=0
    )

    with pytest.raises(SystemExit) as exit_signal:
        service.launch(tmp_path, environ={"PATH": "/usr/bin"}, progress_stream=stream)

    assert exit_signal.value.code == 0
    child = holder["child"]
    assert child.terminated is False
    assert child.arguments[:3] == ["npx", "--yes", DEFAULT_DSH_PACKAGE]
    environment = child.environment
    assert environment[BRIDGE_CALLER_TOKEN_ENV_VAR] == "caller-token"
    assert "подтвердил активацию клиентов" in stream.getvalue()


def test_launch_confirms_effective_composition_of_the_tracked_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Композиция проверяется по эффективному дереву, а не по строкам overlay.

    Запускатель сам спрашивает pin DeepSeek Harness о собранном профиле, поэтому
    тест отдаёт настоящий dump-текст: относительное имя стража из overlay-файла
    композитор печатает абсолютным `file://`-адресом.
    """

    def attestation(child: _FakeHarness) -> str:
        return _readiness_document(
            SimpleNamespace(nonce=child.environment[READINESS_NONCE_ENV_VAR]),
            _generation(),
        )

    # Композитор печатает плоское дерево: записи overlay-файла без обёртки
    # `insert`, а относительное имя плагина — абсолютным `file://`-адресом.
    overlay_text = BRIDGE_OVERLAY_PATH.read_text(encoding="utf-8")
    dumped = "\n".join(
        line[4:] if line.startswith("    ") else line
        for line in overlay_text.split("- insert:\n", 1)[1].splitlines()
    )
    dumped = dumped.replace("./azurpilot-bridge-guard.mjs", BRIDGE_GUARD_PATH.as_uri())

    def install(document: str, *, failing: bool = False):
        service, holder, stream = _install_launcher(
            monkeypatch, tmp_path, attestation_of=attestation, exit_code=0
        )
        if failing:
            dumped_document = document.replace(
                "serverName: azurpilot-game", "serverName: чужой-сервер"
            )
        else:
            dumped_document = document
        monkeypatch.setattr(
            dsh_tooling.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout=dumped_document, stderr=""
            ),
        )
        monkeypatch.setattr(
            dsh_tooling, "overlay_composition", composition.overlay_composition
        )
        monkeypatch.setattr(
            dsh_tooling, "load_composition", composition.load_composition
        )
        monkeypatch.setattr(
            dsh_tooling, "validate_composition", composition.validate_composition
        )
        return service, holder, stream

    service, holder, stream = install(dumped)

    with pytest.raises(SystemExit) as exit_signal:
        service.launch(tmp_path, environ={"PATH": "/usr/bin"}, progress_stream=stream)

    assert exit_signal.value.code == 0
    assert holder["child"].terminated is False
    assert "подтвердил активацию клиентов" in stream.getvalue()

    damaged, damaged_holder, damaged_stream = install(dumped, failing=True)

    with pytest.raises(ToolingError) as failure:
        damaged.launch(
            tmp_path, environ={"PATH": "/usr/bin"}, progress_stream=damaged_stream
        )

    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED
    assert "child" not in damaged_holder


def test_launch_stops_session_without_mandatory_activation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Отсутствие аттестации готовности останавливает запуск продукта."""

    monkeypatch.setattr(dsh_tooling, "READINESS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(dsh_tooling, "READINESS_POLL_SECONDS", 0.01)
    service, holder, stream = _install_launcher(
        monkeypatch, tmp_path, attestation_of=None
    )

    with pytest.raises(ToolingError) as failure:
        service.launch(tmp_path, environ={"PATH": "/usr/bin"}, progress_stream=stream)

    assert failure.value.code is ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE
    assert holder["child"].terminated is True
    assert "подтвердил активацию" not in stream.getvalue()


def test_launch_stops_session_when_guard_reports_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Аттестация отказа стража не превращается в успешный запуск продукта."""

    def attestation(child: _FakeHarness) -> str:
        return json.dumps(
            {
                "schema_version": READINESS_SCHEMA_VERSION,
                "nonce": child.environment[READINESS_NONCE_ENV_VAR],
                "guard_installed": False,
                "reason_code": "AZURPILOT_DSH_MCP_CLIENT_UNAVAILABLE",
                "message": "обязательные клиенты AzurPilot MCP не активировались",
            },
            ensure_ascii=False,
        )

    service, holder, stream = _install_launcher(
        monkeypatch, tmp_path, attestation_of=attestation
    )

    with pytest.raises(ToolingError) as failure:
        service.launch(tmp_path, environ={"PATH": "/usr/bin"}, progress_stream=stream)

    assert failure.value.code is ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE
    assert "AZURPILOT_DSH_MCP_CLIENT_UNAVAILABLE" in failure.value.message
    assert holder["child"].terminated is True


def test_launch_stops_session_that_exits_before_activation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, holder, stream = _install_launcher(
        monkeypatch, tmp_path, attestation_of=None, poll_code=1
    )

    with pytest.raises(ToolingError) as failure:
        service.launch(tmp_path, environ={"PATH": "/usr/bin"}, progress_stream=stream)

    assert failure.value.code is ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE
    assert "завершилась (код 1)" in failure.value.message
    assert holder["child"].terminated is False
