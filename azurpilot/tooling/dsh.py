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
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TextIO

from pydantic import BaseModel, ValidationError

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
from module.persistence.local_environment_schema import SECRET_ENVIRONMENT_KEYS

from .contracts import (
    DshBridgeFamilyCheck,
    DshBridgeFamilyRecord,
    DshBridgeGeneration,
    DshVerificationDetails,
    OperationState,
    ResultCode,
    ToolingResult,
)
from .dsh_composition import (
    load_composition,
    overlay_composition,
    validate_composition,
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
from .process_core import MCP_LOCAL_TOKEN_ENVIRONMENT_KEYS
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
#: Сколько ждать мягкого завершения сессии, прежде чем снимать группу жёстко.
TERMINATION_TIMEOUT_SECONDS = 5.0
#: Шаг ожидания группы процессов при остановке сессии.
_TERMINATION_POLL_SECONDS = 0.2
COMPOSITION_PREFLIGHT_TIMEOUT_SECONDS = 120.0
#: Окно подтверждения активации клиентов, которое запускатель даёт стражу.
GUARD_ACTIVATION_TIMEOUT_SECONDS = 50.0
#: Верхняя граница одной проверки владельца у стража и число его попыток.
#: Значения принадлежат стражу `azurpilot-bridge-guard.mjs` (`VERIFY_TIMEOUT_MS`
#: и `ATTEST_ATTEMPTS`); здесь они зеркалятся, потому что срок запускателя
#: обязан покрывать весь путь стража до аттестации готовности.
GUARD_VERIFY_TIMEOUT_SECONDS = 20.0
GUARD_VERIFY_ATTEMPTS = 3
#: Запас, с которым отказ стража обязан прийти раньше срока запускателя.
READINESS_GUARD_LEAD_SECONDS = 10.0
#: Сколько запускатель ждёт аттестацию стража целиком: окно активации, все
#: попытки проверки владельца и запас на сообщение об отказе.
READINESS_TIMEOUT_SECONDS = (
    GUARD_ACTIVATION_TIMEOUT_SECONDS
    + GUARD_VERIFY_TIMEOUT_SECONDS * GUARD_VERIFY_ATTEMPTS
    + READINESS_GUARD_LEAD_SECONDS
)
READINESS_POLL_SECONDS = 0.1
#: Аргументы обычного запуска, которые задают композицию сессии. Их принимает
#: только сам запускатель: проверка эффективной композиции относится к его
#: профилю и overlay-файлу, поэтому такой аргумент из `--dsh-arg` запустил бы
#: сессию с другой композицией, чем подтверждённая.
COMPOSITION_ARGUMENTS: frozenset[str] = frozenset(
    {"--profile", "--from-default-profile", "--patch"}
)
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")

# Префикс переменных, которыми запускатель владеет сам: унаследованные значения
# из ambient environment не должны выдавать себя за подготовленную генерацию.
GENERATION_ENVIRONMENT_PREFIX = "AZURPILOT_DSH_"

# Внутренние учётные данные проектных сервисов не пересекают границу процесса
# Harness даже если они есть в ambient environment. Список берётся у владельцев
# локального окружения и токенов MCP, а не дублируется здесь.
INTERNAL_CREDENTIAL_ENVIRONMENT_KEYS: frozenset[str] = frozenset(
    set(SECRET_ENVIRONMENT_KEYS) | set(MCP_LOCAL_TOKEN_ENVIRONMENT_KEYS.values())
)


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


@dataclass(frozen=True)
class _ReadinessChannel:
    """Канал подтверждения готовности, которым владеет один запуск Harness.

    Каталог создаётся закрытым, документ пишет страж клиента, а запускатель
    принимает только аттестацию с одноразовым значением этого запуска.
    """

    directory: Path
    document: Path
    nonce: str


def _bounded_detail(payload: Mapping[str, object], limit: int = 240) -> str:
    """Собрать короткий диагностический текст из полей аттестации или вывода команды."""

    code = payload.get("reason_code")
    message = payload.get("message")
    parts = [
        " ".join(value.split())
        for value in (code, message)
        if isinstance(value, str) and value.strip()
    ]
    return (": ".join(parts)[:limit]) if parts else "причина не указана."


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
        """Запустить обычный Harness и подтвердить обязательную активацию клиента.

        Запуск считается успешным только после аттестации стража клиента: она
        подтверждает, что страж установлен, оба клиента AzurPilot MCP
        зарегистрировали свои инструменты и владелец проверки считает checkout
        соответствующим генерации. Без аттестации сессия останавливается.
        """

        self._require_launch_arguments(dsh_arguments)
        prepared = self.prepare(
            repository_root,
            profile=profile,
            dsh_package=dsh_package,
        )
        generation = prepared.details
        if not prepared.ok or generation is None:
            raise ToolingError(prepared.code, prepared.message)

        root = Path(generation.checkout_root)
        stream = progress_stream or sys.stderr
        channel = self._readiness_channel()
        child: subprocess.Popen[bytes] | None = None
        try:
            environment = self._generation_environment(
                root,
                generation,
                self._caller_token(root),
                os.environ if environ is None else environ,
                channel,
            )
            executable = shutil.which(NPX_COMMAND)
            if executable is None:
                raise ToolingError(
                    ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                    "Команда `npx` недоступна в PATH; запуск DeepSeek Harness невозможен.",
                )
            self._require_effective_composition(
                executable, root, generation, environment
            )
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
            child = self._start_harness(arguments, executable, environment, root)
            self._await_readiness(channel, generation, child)
            stream.write(
                "[azurpilot-harness] страж AzurPilot подтвердил активацию клиентов "
                f"{', '.join(BRIDGE_MCP_SERVER_NAMES)} и соответствие checkout "
                "генерации\n"
            )
            stream.flush()
            returncode = self._supervise(child)
        except BaseException:
            if child is not None:
                self._terminate(child)
            raise
        finally:
            shutil.rmtree(channel.directory, ignore_errors=True)
        raise SystemExit(returncode)

    def _require_launch_arguments(self, arguments: Sequence[str]) -> None:
        """Запретить дополнительным аргументам задавать композицию сессии.

        Запускатель подтверждает эффективную композицию своего профиля и
        overlay-файла, поэтому аргумент, меняющий эту композицию, обязан быть
        отклонён до старта сессии, а не после проверки.
        """

        for argument in arguments:
            name = argument.split("=", 1)[0]
            if name in COMPOSITION_ARGUMENTS:
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    f"Аргумент `{name}` принадлежит запускателю: он задаёт "
                    "проверяемую композицию сессии.",
                )

    def _start_harness(
        self,
        arguments: Sequence[str],
        executable: str,
        environ: Mapping[str, str],
        root: Path,
    ) -> subprocess.Popen[bytes]:
        """Запустить обычный Harness дочерним процессом под контролем запускателя.

        Сессия запускается отдельной группой процессов POSIX: между запускателем
        и сессией стоит обёртка `npx`, поэтому остановка обязана снимать группу
        целиком, а сигналы терминала группа больше не получает сама.
        """

        try:
            return subprocess.Popen(
                list(arguments),
                executable=executable,
                env=dict(environ),
                cwd=str(root),
                close_fds=True,
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Не удалось запустить DeepSeek Harness обычным клиентом.",
            ) from error

    def _supervise(self, child: subprocess.Popen[bytes]) -> int:
        """Дождаться завершения сессии Harness и вернуть её код возврата.

        Сессия живёт в отдельной группе процессов, поэтому сигнал терминала
        запускатель пересылает группе сам. Обёртка `npx` завершается раньше самой
        сессии, поэтому после её выхода группа проверяется ещё раз: участник,
        оставшийся в живых, удержал бы соединение моста и токен в окружении.
        """

        previous = self._forward_signals(child)
        try:
            returncode = child.wait()
            if self._group_alive(child):
                self._terminate(child)
            return returncode
        except KeyboardInterrupt:  # pragma: no cover - зависит от сигнала терминала
            self._signal_group(child, signal.SIGINT)
            return child.wait()
        finally:
            self._restore_signals(previous)

    def _forward_signals(self, child: subprocess.Popen[bytes]) -> dict[int, Any]:
        """Пересылать группе сессии сигналы, которые получил запускатель."""

        if os.name != "posix":
            return {}
        forwarded: dict[int, Any] = {}
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                forwarded[signum] = signal.signal(
                    signum,
                    lambda received, _frame, session=child: self._signal_group(
                        session, received
                    ),
                )
            except AttributeError, OSError, ValueError:  # pragma: no cover
                continue
        return forwarded

    def _restore_signals(self, previous: Mapping[int, Any]) -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except OSError, ValueError:  # pragma: no cover - не главный поток
                continue

    def _terminate(self, child: subprocess.Popen[bytes]) -> None:
        """Остановить сессию вместе с её группой процессов.

        Обёртка `npx` завершается раньше самой сессии, поэтому ожидание
        ориентируется на группу процессов: участник, который не завершился по
        мягкому сигналу, получает жёсткий — иначе сессия осталась бы сиротой с
        соединением моста и токеном в окружении.
        """

        if not self._group_alive(child):
            child.wait()
            return
        # Обёртка могла быть уже снята, но группа сессии — нет: мягкий сигнал
        # адресуется группе независимо от состояния её лидера.
        self._signal_group(child, signal.SIGTERM, require_running=False)
        deadline = time.monotonic() + TERMINATION_TIMEOUT_SECONDS
        while self._group_alive(child) and time.monotonic() < deadline:
            if child.poll() is None:
                try:
                    child.wait(timeout=_TERMINATION_POLL_SECONDS)
                except subprocess.TimeoutExpired:  # pragma: no cover - зависит от сессии
                    continue
            else:
                # Лидер уже снят: ожидание группы не может опираться на его код
                # возврата, поэтому пауза выдерживается явно.
                time.sleep(_TERMINATION_POLL_SECONDS)
        if self._group_alive(child):
            kill = getattr(signal, "SIGKILL", None)
            if kill is None:  # pragma: no cover - платформа без SIGKILL
                child.kill()
            else:
                # Обёртка могла быть уже снята, но группа сессии — нет:
                # жёсткий сигнал адресуется группе независимо от её лидера.
                self._signal_group(child, kill, require_running=False)
        child.wait()

    def _signal_group(
        self,
        child: subprocess.Popen[bytes],
        signum: int,
        *,
        require_running: bool = True,
    ) -> None:
        """Послать сигнал всей группе сессии, а не одной обёртке `npx`."""

        if require_running and child.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(child.pid, signum)
            except OSError:  # pragma: no cover - группа или её лидер исчезли
                pass
            return
        try:
            child.send_signal(signum)
        except OSError:  # pragma: no cover - процесс уже исчез
            pass

    def _group_alive(self, child: subprocess.Popen[bytes]) -> bool:
        """Остался ли в группе сессии хотя бы один участник."""

        if os.name != "posix":
            return child.poll() is None
        try:
            os.killpg(child.pid, 0)
        except OSError:
            return False
        return True

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
        recorded = (
            expected if expected is not None else self._recorded_identities(environment)
        )

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
        readiness: _ReadinessChannel | None = None,
    ) -> dict[str, str]:
        """Собрать ограниченное окружение процесса Harness.

        Из унаследованного окружения убираются внутренние учётные данные
        проектных сервисов и значения генерации: ambient environment не является
        доверенным источником для этой границы. Значения, которые определяют
        клиента AzurPilot, задаются здесь явно, а обычные значения времени выполнения
        пользовательской сессии (`PATH`, `HOME`, локаль, прокси, настройки Node)
        сохраняются.
        """

        child = {
            key: value
            for key, value in environ.items()
            if key not in INTERNAL_CREDENTIAL_ENVIRONMENT_KEYS
            and not key.startswith(GENERATION_ENVIRONMENT_PREFIX)
        }
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
        if readiness is not None:
            child[READINESS_FILE_ENV_VAR] = str(readiness.document)
            child[READINESS_NONCE_ENV_VAR] = readiness.nonce
            # Страж обязан сообщить об отказе активации раньше, чем запускатель
            # остановит сессию по собственному сроку ожидания. Окно активации —
            # отдельная граница стража, а не остаток срока запускателя: после
            # него страж ещё выполняет проверку владельца.
            child[READINESS_TIMEOUT_ENV_VAR] = str(
                int(GUARD_ACTIVATION_TIMEOUT_SECONDS * 1000)
            )
        return child

    def _readiness_channel(self) -> _ReadinessChannel:
        """Создать закрытый канал подтверждения готовности для одного запуска."""

        directory = Path(tempfile.mkdtemp(prefix="azurpilot-harness-"))
        try:
            directory.chmod(0o700)
        except OSError:  # pragma: no cover - зависит от файловой системы
            pass
        return _ReadinessChannel(
            directory=directory,
            document=directory / "ready.json",
            nonce=secrets.token_hex(16),
        )

    def _await_readiness(
        self,
        channel: _ReadinessChannel,
        generation: DshBridgeGeneration,
        child: subprocess.Popen[bytes],
    ) -> None:
        """Дождаться аттестации готовности стража или остановить запуск."""

        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            returncode = child.poll()
            if returncode is not None:
                raise ToolingError(
                    ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                    "Сессия DeepSeek Harness завершилась (код "
                    f"{returncode}) до подтверждения активации обязательных "
                    "клиентов AzurPilot.",
                )
            try:
                document = channel.document.read_text(encoding="utf-8")
            except OSError:
                time.sleep(READINESS_POLL_SECONDS)
                continue
            self._require_readiness(channel, generation, document)
            return
        raise ToolingError(
            ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
            "Страж AzurPilot не подтвердил активацию клиентов "
            f"{', '.join(BRIDGE_MCP_SERVER_NAMES)} за "
            f"{READINESS_TIMEOUT_SECONDS:.0f} с; запуск Harness остановлен.",
        )

    def _require_readiness(
        self,
        channel: _ReadinessChannel,
        generation: DshBridgeGeneration,
        document: str,
    ) -> None:
        """Проверить аттестацию готовности стража клиента.

        Подтверждением считается только документ текущего запуска, в котором
        страж установлен, оба семейства AzurPilot MCP зарегистрировали
        инструменты, владелец проверки подтвердил соответствие checkout, а
        ревизия совпадает с генерацией.
        """

        try:
            payload = json.loads(document)
        except ValueError as error:
            raise ToolingError(
                ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                "Аттестация готовности стража AzurPilot повреждена.",
            ) from error
        if not isinstance(payload, dict):
            raise ToolingError(
                ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                "Аттестация готовности стража AzurPilot повреждена.",
            )
        if payload.get("schema_version") != READINESS_SCHEMA_VERSION:
            raise ToolingError(
                ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                "Аттестация готовности стража AzurPilot имеет неизвестную версию "
                "документа.",
            )
        if payload.get("nonce") != channel.nonce:
            raise ToolingError(
                ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                "Аттестация готовности не принадлежит текущему запуску Harness.",
            )
        if payload.get("guard_installed") is not True:
            raise ToolingError(
                ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                "Страж AzurPilot не активировался: " + _bounded_detail(payload),
            )
        if payload.get("verification") != "ready":
            raise ToolingError(
                ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                "Владелец проверки не подтвердил соответствие checkout генерации "
                f"клиента AzurPilot (состояние {payload.get('verification')!r}).",
            )
        families = payload.get("mcp_families")
        if not isinstance(families, list) or set(families) != set(
            BRIDGE_MCP_SERVER_NAMES
        ):
            raise ToolingError(
                ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                "Не подтверждена активация обоих клиентов AzurPilot MCP: "
                f"{families!r}.",
            )
        if payload.get("source_revision") != generation.source_revision:
            raise ToolingError(
                ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                "Аттестация готовности относится к другой ревизии источника, чем "
                "подготовленная генерация клиента AzurPilot.",
            )

    def _require_effective_composition(
        self,
        executable: str,
        root: Path,
        generation: DshBridgeGeneration,
        environ: Mapping[str, str],
    ) -> None:
        """Подтвердить эффективную композицию продуктового профиля перед запуском.

        Точный pin DeepSeek Harness `0.1.7-rc.2` не позволяет объявить entry
        обязательным из профиля, поэтому запускатель сам проверяет, что
        собранный профиль содержит страж и оба клиента AzurPilot MCP ровно по
        одному разу и без изменений относительно overlay-файла.

        `--dump-config` — верхняя аппроксимация: он перечисляет записи, которые
        загрузчик вправе не активировать. Поэтому проверка только запрещает:
        лишняя запись клиента или стража закрывает запуск, а отсутствие запрета
        здесь не заменяет подтверждение активации стражем.
        """

        overlay = Path(generation.overlay_path)
        arguments = [
            NPX_COMMAND,
            "--yes",
            generation.dsh_package,
            "--profile",
            generation.profile,
            "--patch",
            str(overlay),
            "--dump-config",
        ]
        try:
            completed = subprocess.run(
                arguments,
                executable=executable,
                env=dict(environ),
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=COMPOSITION_PREFLIGHT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Не удалось получить эффективную композицию профиля "
                f"DeepSeek Harness {generation.profile!r}.",
            ) from error
        if completed.returncode != 0:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "DeepSeek Harness не собрал эффективную композицию профиля "
                f"{generation.profile!r} (код {completed.returncode}): "
                + _bounded_detail({"message": completed.stderr or completed.stdout}),
            )
        validate_composition(
            overlay_composition(overlay),
            load_composition(completed.stdout),
            overlay_dir=overlay.parent,
        )

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
            message = "Изменились исходники MCP семейства: " + ", ".join(drift) + "."
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
                        else (
                            "Независимая сессия только для чтения не подтвердила "
                            "маршрут моста (" + ", ".join(failure[1]) + ")."
                        )[:300]
                    ),
                    server_version=identity.server_version,
                    source_revision=identity.source_revision,
                )
            )
        return tuple(checks)
