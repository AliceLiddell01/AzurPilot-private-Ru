"""DeepSeek Harness как обычный Linux-клиент моста Windows MCP.

Модуль владеет клиентской границей: подготовкой окружения одной генерации,
запуском Harness с отслеживаемым overlay и проверкой соответствия checkout этой
генерации. Идентичность источника здесь повторно не вычисляется — её собирает
`azurpilot.tooling.mcp_source_identity` из канонического комплекта MCP.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn, TextIO

from pydantic import BaseModel, ValidationError

from azurpilot.dsh import (
    AZUR_EXECUTABLE_ENV_VAR,
    BRIDGE_GUARD_PATH,
    BRIDGE_OVERLAY_PATH,
    CHECKOUT_ROOT_ENV_VAR,
    DSH_PACKAGE_ENV_VAR,
    DSH_PROFILE_ENV_VAR,
    IDENTITY_ENV_VARS,
    SOURCE_REVISION_ENV_VAR,
)
from module.mcp_shared.local_http_auth import (
    LocalHttpAuthError,
    LocalHttpAuthUnknownError,
    read_local_mcp_bridge_caller_token,
)
from module.mcp_shared.versioning import McpBundle
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_CALLER_TOKEN_ENV_VAR,
    BRIDGE_ROUTES,
    BridgeIdentityError,
    BridgeRoute,
    BridgeSourceIdentity,
    bridge_endpoint,
    parse_expected_identity,
    serialize_identity,
)

from .contracts import (
    DshBridgeFamilyCheck,
    DshBridgeFamilyRecord,
    DshBridgeGeneration,
    DshVerificationDetails,
    OperationState,
    ResultCode,
    ToolingResult,
)
from .errors import ToolingError
from .mcp import McpSourceReconciler
from .mcp_source_identity import (
    BRIDGE_MCP_SERVER_NAMES,
    expected_bridge_headers,
    expected_bridge_identities,
    expected_bridge_identity,
    identity_drift_fields,
    load_bridge_bundle,
    source_snapshot,
)
from .repository import RepositoryResolver

__all__ = (
    "DEFAULT_DSH_PACKAGE",
    "DEFAULT_DSH_PROFILE",
    "DshBridgeService",
    "load_generation",
)

DEFAULT_DSH_PROFILE = "azurpilot-web"
DEFAULT_DSH_PACKAGE = "@deepseek-ai/dsh@0.1.7-rc.2"
AZUR_COMMAND = "azur"
NPX_COMMAND = "npx"
BRIDGE_PROBE_TIMEOUT_SECONDS = 20.0
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def load_generation(path: str | Path) -> DshBridgeGeneration:
    """Прочитать конверт `azur dsh prepare --json` как запись генерации клиента."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as error:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Файл генерации клиента DeepSeek Harness недоступен.",
        ) from error
    except ValueError as error:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Файл генерации клиента DeepSeek Harness не является JSON.",
        ) from error
    candidate = payload.get("details", payload) if isinstance(payload, dict) else None
    try:
        return DshBridgeGeneration.model_validate(candidate)
    except ValidationError as error:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Конверт генерации клиента DeepSeek Harness повреждён.",
        ) from error


def _bridge_route(server_name: str) -> BridgeRoute:
    for route in BRIDGE_ROUTES.values():
        if route.server_name == server_name:
            return route
    raise ToolingError(  # pragma: no cover - закрытый каталог маршрутов моста
        ResultCode.MCP_SOURCE_BUNDLE_INVALID,
        "Канонический контракт моста Windows MCP не содержит требуемый сервер.",
    )


class DshBridgeService:
    """Подготовить, запустить и проверить клиент DeepSeek Harness."""

    def __init__(self, resolver: RepositoryResolver | None = None) -> None:
        self.resolver = resolver or RepositoryResolver()

    def prepare(
        self,
        repository_root: str | Path | None = None,
        *,
        profile: str = DEFAULT_DSH_PROFILE,
        dsh_package: str = DEFAULT_DSH_PACKAGE,
        environ: Mapping[str, str] | None = None,
    ) -> ToolingResult[BaseModel, BaseModel]:
        """Собрать генерацию клиента и подтвердить оба маршрута моста до запуска."""

        root = self._root(repository_root)
        revision, source_state = source_snapshot(root)
        declared = load_bridge_bundle(root)
        recorded = expected_bridge_identities(declared, revision)
        self._require_live_coherence(root, revision, declared)
        generation = self._generation(
            root, revision, source_state, profile, dsh_package, recorded
        )
        caller_token = self._caller_token(root)
        checks = self._probe_bridge(root, revision, recorded, caller_token)
        failed = next((item for item in checks if item.status != "ready"), None)
        if failed is not None:
            return ToolingResult(
                ok=False,
                code=ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                state=OperationState.FAILED,
                message=(
                    f"Мост Windows MCP не подтвердил маршрут {failed.server_name} "
                    f"({failed.reason_code}). Checkout на ревизии "
                    f"{generation.source_revision[:12]}; среду выполнения моста "
                    "нужно запустить на той же ревизии."
                )[:300],
                details=generation,
            )
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message=(
                "Мост Windows MCP подтвердил оба маршрута AzurPilot MCP новой сессией "
                "только для чтения; генерация клиента DeepSeek Harness готова."
            ),
            details=generation,
        )

    def launch(
        self,
        repository_root: str | Path | None = None,
        *,
        profile: str = DEFAULT_DSH_PROFILE,
        dsh_package: str = DEFAULT_DSH_PACKAGE,
        dsh_arguments: Sequence[str] = (),
        environ: Mapping[str, str] | None = None,
        progress_stream: TextIO | None = None,
    ) -> NoReturn:
        """Подготовить генерацию и заменить процесс запуска обычным вызовом Harness."""

        prepared = self.prepare(
            repository_root,
            profile=profile,
            dsh_package=dsh_package,
            environ=environ,
        )
        generation = prepared.details
        if not prepared.ok or generation is None:
            raise ToolingError(prepared.code, prepared.message)

        root = Path(generation.checkout_root)
        environment = self._generation_environment(
            root,
            generation,
            self._caller_token(root),
            os.environ if environ is None else environ,
        )
        executable = shutil.which(NPX_COMMAND)
        if executable is None:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Команда `npx` недоступна в PATH; запуск DeepSeek Harness невозможен.",
            )
        stream = progress_stream or sys.stderr
        stream.write(
            "[azurpilot-harness] мост Windows MCP подтверждён для "
            f"{', '.join(item.server_name for item in generation.families)} "
            f"на ревизии {generation.source_revision[:12]}\n"
        )
        stream.flush()

        arguments = [
            NPX_COMMAND,
            "--yes",
            dsh_package,
            "--profile",
            profile,
            "--patch",
            str(BRIDGE_OVERLAY_PATH),
        ]
        if not dsh_arguments:
            arguments.append("--no-open")
        arguments.extend(dsh_arguments)
        try:
            os.execvpe(executable, arguments, environment)
        except OSError as error:  # pragma: no cover - зависит от состояния процесса
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Не удалось заменить процесс запуска DeepSeek Harness.",
            ) from error

    def verify(
        self,
        repository_root: str | Path | None = None,
        *,
        revision: str | None = None,
        expected: Mapping[str, BridgeSourceIdentity] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> ToolingResult[BaseModel, BaseModel]:
        """Сверить текущий checkout с генерацией, в которой работает сессия."""

        environment = os.environ if environ is None else environ
        configured_root = repository_root
        if configured_root is None:
            configured_root = environment.get(CHECKOUT_ROOT_ENV_VAR)
        root = self._root(configured_root)
        recorded_revision = revision or environment.get(SOURCE_REVISION_ENV_VAR)
        if not isinstance(recorded_revision, str) or not _SHA_RE.fullmatch(
            recorded_revision
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Генерация клиента не подготовлена: нет подтверждённой ревизии источника.",
            )
        recorded = expected if expected is not None else self._recorded_identities(environment)

        current_revision, _ = source_snapshot(root)
        fresh = McpSourceReconciler().build(root, requested_bump=None)
        checks = tuple(
            self._family_check(
                server_name,
                recorded[server_name],
                expected_bridge_identity(fresh.bundle, server_name, current_revision),
                current_revision,
            )
            for server_name in BRIDGE_MCP_SERVER_NAMES
        )
        drifted = tuple(item for item in checks if item.status != "ready")
        details = DshVerificationDetails(
            checkout_root=str(root),
            recorded_revision=recorded_revision,
            current_revision=current_revision,
            bridge_endpoint=bridge_endpoint(),
            families=checks,
        )
        if not drifted:
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message=(
                    "Checkout соответствует генерации клиента: оба семейства AzurPilot MCP "
                    "остаются валидными."
                ),
                details=details,
            )
        return ToolingResult(
            ok=False,
            code=ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
            state=OperationState.CONFLICT,
            message=(
                "Checkout больше не соответствует генерации клиента для "
                f"{', '.join(item.server_name for item in drifted)}."
            )[:300],
            details=details,
        )

    def verify_generation(
        self,
        generation: DshBridgeGeneration,
        repository_root: str | Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> ToolingResult[BaseModel, BaseModel]:
        """Сверить checkout с явно переданной записью генерации клиента."""

        return self.verify(
            generation.checkout_root if repository_root is None else repository_root,
            revision=generation.source_revision,
            expected={item.server_name: item.identity for item in generation.families},
            environ=environ,
        )

    def _root(self, repository_root: str | Path | None) -> Path:
        return self.resolver.resolve(repository_root).path

    def _caller_token(self, root: Path) -> str:
        try:
            return read_local_mcp_bridge_caller_token(root)
        except LocalHttpAuthUnknownError as error:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние токена клиента моста Windows MCP неизвестно.",
            ) from error
        except LocalHttpAuthError as error:
            raise ToolingError(
                ResultCode.MCP_BRIDGE_AUTH_NOT_CONFIGURED,
                "Токен клиента моста Windows MCP недоступен; настройте его командой "
                "`azur mcp bridge configure --stdin-token`.",
            ) from error

    def _require_live_coherence(
        self, root: Path, revision: str, declared: McpBundle
    ) -> None:
        fresh = McpSourceReconciler().build(root, requested_bump=None)
        for server_name in BRIDGE_MCP_SERVER_NAMES:
            drift = identity_drift_fields(
                expected_bridge_identity(declared, server_name, revision),
                expected_bridge_identity(fresh.bundle, server_name, revision),
            )
            if drift:
                raise ToolingError(
                    ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
                    f"Канонический комплект MCP не соответствует исходникам для "
                    f"{server_name} ({', '.join(drift)}); выполните `azur mcp sync`.",
                )

    def _generation(
        self,
        root: Path,
        revision: str,
        source_state: str,
        profile: str,
        dsh_package: str,
        identities: Mapping[str, BridgeSourceIdentity],
    ) -> DshBridgeGeneration:
        return DshBridgeGeneration(
            checkout_root=str(root),
            source_revision=revision,
            source_state=source_state,
            profile=profile,
            dsh_package=dsh_package,
            overlay_path=str(BRIDGE_OVERLAY_PATH),
            guard_path=str(BRIDGE_GUARD_PATH),
            bridge_endpoint=bridge_endpoint(),
            families=tuple(
                DshBridgeFamilyRecord(
                    server_name=server_name,
                    endpoint=_bridge_route(server_name).bridge_url,
                    identity=identities[server_name],
                )
                for server_name in BRIDGE_MCP_SERVER_NAMES
            ),
        )

    def _generation_environment(
        self,
        root: Path,
        generation: DshBridgeGeneration,
        caller_token: str,
        environ: Mapping[str, str],
    ) -> dict[str, str]:
        child = dict(environ)
        child[CHECKOUT_ROOT_ENV_VAR] = str(root)
        child[SOURCE_REVISION_ENV_VAR] = generation.source_revision
        child[AZUR_EXECUTABLE_ENV_VAR] = self._azur_executable()
        child[DSH_PROFILE_ENV_VAR] = generation.profile
        child[DSH_PACKAGE_ENV_VAR] = generation.dsh_package
        child[BRIDGE_CALLER_TOKEN_ENV_VAR] = caller_token
        for record in generation.families:
            child[IDENTITY_ENV_VARS[record.server_name]] = serialize_identity(
                record.identity
            )
        return child

    def _azur_executable(self) -> str:
        executable = shutil.which(AZUR_COMMAND)
        if executable is None:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Команда `azur` недоступна в PATH; клиент нельзя подготовить безопасно.",
            )
        return executable

    def _recorded_identities(
        self, environ: Mapping[str, str]
    ) -> dict[str, BridgeSourceIdentity]:
        identities: dict[str, BridgeSourceIdentity] = {}
        for server_name in BRIDGE_MCP_SERVER_NAMES:
            raw = environ.get(IDENTITY_ENV_VARS[server_name])
            if not isinstance(raw, str) or not raw.strip():
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Генерация клиента не подготовлена: нет ожидаемой идентичности "
                    f"{server_name}.",
                )
            try:
                identities[server_name] = parse_expected_identity(raw)
            except BridgeIdentityError as error:
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    f"Записанная ожидаемая идентичность {server_name} повреждена.",
                ) from error
        return identities

    def _family_check(
        self,
        server_name: str,
        recorded: BridgeSourceIdentity,
        live: BridgeSourceIdentity,
        current_revision: str,
    ) -> DshBridgeFamilyCheck:
        drift = identity_drift_fields(recorded, live)
        if not drift:
            return DshBridgeFamilyCheck(
                server_name=server_name,
                endpoint=_bridge_route(server_name).bridge_url,
                status="ready",
                reason_code="AZURPILOT_DSH_SOURCE_MATCHES",
                message="Checkout соответствует генерации сессии.",
                server_version=live.server_version,
                source_revision=current_revision,
            )
        if "source_revision" in drift:
            message = (
                "Git HEAD изменился: генерация создана на "
                f"{recorded.source_revision[:12]}, текущий checkout на "
                f"{current_revision[:12]}."
            )
        else:
            message = (
                "Изменились исходники MCP семейства: " + ", ".join(drift) + "."
            )
        return DshBridgeFamilyCheck(
            server_name=server_name,
            endpoint=_bridge_route(server_name).bridge_url,
            status="drift",
            reason_code="AZURPILOT_DSH_SOURCE_DRIFT",
            message=message[:300],
            server_version=live.server_version,
            source_revision=current_revision,
            mismatched_fields=drift,
        )

    def _probe_bridge(
        self,
        root: Path,
        revision: str,
        identities: Mapping[str, BridgeSourceIdentity],
        caller_token: str,
    ) -> tuple[DshBridgeFamilyCheck, ...]:
        """Подтвердить оба маршрута тем же каноническим клиентом, что и приёмка."""

        from azurpilot.integrations.mcp_client import (
            HttpTransportPolicy,
            accept_fresh_http,
            accept_fresh_http_modern,
        )
        from dev_tools.mcp_acceptance import build_plan

        async def probe_all() -> list[object]:
            pending = []
            labels: list[tuple[str, str]] = []
            for server_name in BRIDGE_MCP_SERVER_NAMES:
                route = _bridge_route(server_name)
                headers = expected_bridge_headers(caller_token, identities[server_name])
                plan = build_plan(revision, server_name)
                for client in (accept_fresh_http, accept_fresh_http_modern):
                    labels.append((server_name, client.__name__))
                    pending.append(
                        client(
                            endpoint=route.bridge_url,
                            headers=headers,
                            plan=plan,
                            timeout_seconds=BRIDGE_PROBE_TIMEOUT_SECONDS,
                            transport_policy=HttpTransportPolicy.ISOLATED_LOOPBACK,
                        )
                    )
            results = await asyncio.gather(*pending, return_exceptions=True)
            return [*zip(labels, results, strict=True)]

        outcomes: dict[str, tuple[str, tuple[str, ...]]] = {}
        for (server_name, client_name), outcome in asyncio.run(probe_all()):
            if isinstance(outcome, BaseException):
                failure = (
                    "MCP_FRESH_CLIENT_HTTP_FAILED",
                    (type(outcome).__name__,),
                )
            elif getattr(outcome, "state", None) is not None and (
                outcome.state.value == "READY"
            ):
                failure = None
            else:
                failure = (
                    str(getattr(outcome, "reason_code", "MCP_FRESH_CLIENT_UNKNOWN")),
                    tuple(getattr(outcome, "diagnostics", ()) or ()),
                )
            if failure is None or server_name in outcomes:
                continue
            outcomes[server_name] = (
                failure[0],
                (client_name, *failure[1]),
            )
        checks = []
        for server_name in BRIDGE_MCP_SERVER_NAMES:
            identity = identities[server_name]
            failure = outcomes.get(server_name)
            checks.append(
                DshBridgeFamilyCheck(
                    server_name=server_name,
                    endpoint=_bridge_route(server_name).bridge_url,
                    status="ready" if failure is None else "unavailable",
                    reason_code=(
                        "AZURPILOT_DSH_BRIDGE_ACCEPTED"
                        if failure is None
                        else failure[0]
                    )[:128],
                    message=(
                        "Независимая сессия только для чтения подтвердила маршрут моста."
                        if failure is None
                        else "Независимая сессия только для чтения не подтвердила "
                        "маршрут моста ("
                        + ", ".join(failure[1])
                        + ")."
                    ),
                    server_version=identity.server_version,
                    source_revision=identity.source_revision,
                )
            )
        return tuple(checks)
