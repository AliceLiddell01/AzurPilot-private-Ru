"""Совместимый комплект MCP, согласование исходников и локальная среда выполнения."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from module.mcp_shared.catalog import (
    canonical_json,
    capability_catalog_sha256,
    contract_revision,
    sha256_text,
    tool_catalog_sha256_from_tools,
    tool_descriptor_hashes_from_tools,
    tool_names_from_tools,
)
from module.mcp_shared.local_http_auth import (
    LocalHttpAuthError,
    LocalHttpAuthUnknownError,
    read_local_mcp_token,
)
from module.mcp_shared.versioning import (
    MCP_SOURCE_SET_NAMES,
    McpBundle,
    McpCompatibility,
    McpServerVersion,
    SemVer,
    VersioningError,
    load_mcp_bundle,
    load_mcp_bundle_bytes,
    source_revision,
)

from .config import project_python
from .contracts import (
    McpAcceptanceDetails,
    McpBridgeAcceptanceDetails,
    McpBridgeProcessStatus,
    McpBridgeStatusDetails,
    McpBridgeUpstreamStatus,
    McpDigest,
    McpImpactDetails,
    McpImpactPath,
    McpLifecycleDetails,
    McpReconcileDetails,
    McpServerStatus,
    McpStatusDetails,
    McpSyncDetails,
    McpVersionDetails,
    OperationState,
    ResultCode,
    ToolingResult,
)
from .errors import ToolingError
from .filesystem import (
    ScopedPath,
    bounded_read_bytes,
    bounded_read_text,
    is_unsafe_path,
)
from .git import GitClient
from .process import (
    MCP_LOCAL_TOKEN_ENVIRONMENT_KEYS,
    ProcessController,
    ProcessSpec,
    ProcessStartError,
    StructuredProcessRunner,
)
from .repository import RepositoryResolver

if TYPE_CHECKING:
    from module.mcp_shared.local_http_supervisor import LocalHttpSupervisorStopResult

MCP_SERVER_NAMES = ("azurpilot-dev", "azurpilot-game")
_McpAuthenticationState = Literal["configured", "unavailable", "unknown"]
_McpAuthenticationStates = dict[str, _McpAuthenticationState]
PLUGIN_MANIFEST_PATH = Path("plugins/azurpilot/.codex-plugin/plugin.json")
PLUGIN_COMPATIBILITY_PATH = Path("plugins/azurpilot/compatibility.json")
CODEX_CONFIG_PATH = Path(".codex/config.toml")
MCP_GENERATED_ARTIFACTS = (
    Path("config/mcp-versions.toml"),
    PLUGIN_MANIFEST_PATH,
    PLUGIN_COMPATIBILITY_PATH,
)


def _supports_windows_mcp_bridge() -> bool:
    return os.name == "nt"
_MCP_IMPACT_SAMPLE_LIMIT = 256

SOURCE_SET_PATHS: Mapping[str, tuple[Path, ...]] = {
    # Эти пути отражают реальные границы обычного и отложенного импорта серверов.
    # Здесь намеренно не включён весь `module/application`: это предотвращает
    # повышение версии из-за несвязанных частей продукта и сохраняет модули
    # приложения и предметной области, которые вызывают адаптеры MCP.
    "DEV_MCP_SOURCE_SET": (
        Path("module/dev_mcp"),
        Path("module/dev_runtime"),
        Path("module/application/canonical_payload.py"),
        Path("module/application/adb_target.py"),
        Path("module/application/database_diagnostics.py"),
        Path("module/application/errors.py"),
        Path("module/application/fleet_manual_scan.py"),
        Path("module/application/fleet_page.py"),
        Path("module/application/game_models.py"),
        Path("module/application/game_ports.py"),
        Path("module/application/game_read_service.py"),
        Path("module/application/game_validation.py"),
        Path("module/application/legacy_adapters.py"),
        Path("module/application/legacy_game_adapters.py"),
        Path("module/application/models.py"),
        Path("module/application/ports.py"),
        Path("module/application/fleet_state.py"),
        Path("module/application/instance_identity.py"),
        Path("module/application/morale.py"),
        Path("module/application/resource_fields.py"),
        Path("module/application/bot_runtime_client.py"),
        Path("module/application/host_lock.py"),
        Path("module/application/runtime_control.py"),
        Path("module/application/runtime_handover.py"),
        Path("module/application/runtime_log_projection.py"),
        Path("module/application/runtime_process_manager.py"),
        Path("module/application/resource_lease.py"),
        Path("module/application/runtime_state.py"),
        Path("module/application/runtime_worker_registry.py"),
        Path("module/bot_runtime.py"),
        Path("module/application/bot_runtime_owner.py"),
        Path("module/application/runtime_storage.py"),
        Path("module/application/storage_models.py"),
        Path("module/application/storage_ports.py"),
        Path("module/formation/model.py"),
        Path("module/dock_inventory/model.py"),
        Path("module/persistence"),
        Path("module/config/profile.py"),
        Path("module/config/time_sentinel.py"),
        Path("module/config/constants.py"),
        Path("deploy/atomic.py"),
        Path("module/observability/identity.py"),
    ),
    "GAME_MCP_SOURCE_SET": (
        Path("module/game_mcp"),
        Path("module/application/canonical_payload.py"),
        Path("module/application/adb_target.py"),
        Path("module/application/database_diagnostics.py"),
        Path("module/application/errors.py"),
        Path("module/application/fleet_manual_scan.py"),
        Path("module/application/fleet_page.py"),
        Path("module/application/fleet_state.py"),
        Path("module/application/game_control_lock.py"),
        Path("module/application/game_control_service.py"),
        Path("module/application/game_models.py"),
        Path("module/application/game_ports.py"),
        Path("module/application/game_read_service.py"),
        Path("module/application/game_validation.py"),
        Path("module/application/host_lock.py"),
        Path("module/application/instance_identity.py"),
        Path("module/application/legacy_adapters.py"),
        Path("module/application/legacy_game_adapters.py"),
        Path("module/application/models.py"),
        Path("module/application/morale.py"),
        Path("module/application/ports.py"),
        Path("module/application/resource_fields.py"),
        Path("module/application/bot_runtime_client.py"),
        Path("module/application/runtime_control.py"),
        Path("module/application/runtime_handover.py"),
        Path("module/application/runtime_log_projection.py"),
        Path("module/application/runtime_execution.py"),
        Path("module/application/runtime_process_manager.py"),
        Path("module/application/runtime_state.py"),
        Path("module/application/resource_lease.py"),
        Path("module/application/runtime_worker_registry.py"),
        Path("module/bot_runtime.py"),
        Path("module/application/bot_runtime_owner.py"),
        Path("module/application/runtime_storage.py"),
        Path("module/application/scheduler_runtime.py"),
        Path("module/application/services.py"),
        Path("module/application/storage_models.py"),
        Path("module/application/storage_ports.py"),
        Path("module/formation/model.py"),
        Path("module/dock_inventory/model.py"),
        Path("module/persistence"),
        Path("module/config/profile.py"),
        Path("module/config/config.py"),
        Path("module/config/config_updater.py"),
        Path("module/config/time_source.py"),
        Path("module/config/utils.py"),
        Path("module/config/task_priority.py"),
        Path("module/observability/incident.py"),
    ),
    "SHARED_MCP_SOURCE_SET": (
        Path("module/mcp_shared"),
        Path("azurpilot/tooling/mcp_coordination.py"),
        Path("azurpilot/tooling/mcp_contracts.py"),
        Path("azurpilot/tooling/mcp_errors.py"),
        Path("azurpilot/tooling/mcp_filesystem.py"),
        Path("azurpilot/tooling/process_core.py"),
        Path("azurpilot/tooling/result.py"),
    ),
    "PLUGIN_BUNDLE_SOURCE_SET": (
        Path("plugins/azurpilot/.codex-plugin"),
        Path("plugins/azurpilot/README.md"),
        Path("plugins/azurpilot/references"),
    ),
    "SKILL_BUNDLE_SOURCE_SET": (Path("plugins/azurpilot/skills"),),
}
SOURCE_SET_NAMES = MCP_SOURCE_SET_NAMES
SERVER_SOURCE_SETS: Mapping[str, tuple[str, ...]] = {
    "azurpilot-dev": ("DEV_MCP_SOURCE_SET", "SHARED_MCP_SOURCE_SET"),
    "azurpilot-game": ("GAME_MCP_SOURCE_SET", "SHARED_MCP_SOURCE_SET"),
}
TOKEN_ENVIRONMENT_KEYS = MCP_LOCAL_TOKEN_ENVIRONMENT_KEYS
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True, slots=True)
class SourceChangeClassification:
    """Результат сопоставления изменённых файлов с явно заданными наборами исходников."""

    changed_components: tuple[str, ...]
    affected_servers: tuple[str, ...]
    plugin_changed: bool
    skill_changed: bool
    unknown_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _BundleBuild:
    bundle: McpBundle
    manifest_text: str
    plugin_manifest_text: str
    compatibility_text: str
    changed_components: tuple[str, ...]
    affected_servers: tuple[str, ...]
    plugin_changed: bool
    skill_changed: bool
    required_bump_servers: tuple[str, ...]
    failure_code: ResultCode | None = None


@dataclass(frozen=True, slots=True)
class McpBaseCompatibility:
    """Результат независимой проверки изменений MCP от базы до текущей ревизии."""

    base_commit: str
    changed_components: tuple[str, ...]
    affected_servers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _McpBaseline:
    versions: Mapping[str, str]
    bundle: McpBundle | None
    plugin_version: str | None = None
    bundle_revision: str | None = None
    skill_bundle_revision: str | None = None


@dataclass(frozen=True, slots=True)
class _McpStartOutcome:
    changed: bool
    runtime: dict[str, object]
    auth_unavailable_servers: tuple[str, ...] = ()
    runtime_unavailable_servers: tuple[str, ...] = ()


class McpSourceDriftDetails(BaseModel):
    """Ограниченные сведения об одном расхождении производного файла MCP."""

    model_config = ConfigDict(extra="forbid")

    artifact: str = Field(min_length=1, max_length=256)
    changed_source_sets: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    affected_servers: tuple[str, ...] = Field(default_factory=tuple, max_length=2)
    expected_source_digests: dict[str, str] = Field(default_factory=dict, max_length=8)
    actual_source_digests: dict[str, str] = Field(default_factory=dict, max_length=8)


def _source_drift_details(
    root: Path,
    artifact: Path,
    build: _BundleBuild,
    current: McpBundle,
) -> McpSourceDriftDetails:
    """Сформировать ограниченные сведения без сравнения изменений, путей окружения и секретов."""

    changed = tuple(
        name
        for name in SOURCE_SET_NAMES
        if current.source_digests.get(name) != build.bundle.source_digests.get(name)
    )
    return McpSourceDriftDetails(
        artifact=artifact.relative_to(root).as_posix(),
        changed_source_sets=changed or build.changed_components,
        affected_servers=build.affected_servers,
        expected_source_digests={
            name: build.bundle.source_digests[name]
            for name in changed
            if name in build.bundle.source_digests
        },
        actual_source_digests={
            name: current.source_digests[name]
            for name in changed
            if name in current.source_digests
        },
    )


def _files_for_source_set(root: Path, source_paths: Iterable[Path]) -> tuple[Path, ...]:
    files: list[Path] = []
    root = root.resolve(strict=False)
    for relative in source_paths:
        candidate = root / relative
        if not candidate.exists():
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                f"Набор исходников указывает на отсутствующий путь: {relative.as_posix()}.",
            )
        if is_unsafe_path(candidate):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                f"Набор исходников содержит символическую ссылку или точку повторной обработки: {relative.as_posix()}.",
            )
        if candidate.is_file():
            files.append(candidate)
            continue
        if not candidate.is_dir():
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                f"Набор исходников содержит объект неизвестного типа: {relative.as_posix()}.",
            )
        try:
            children = sorted(candidate.rglob("*"), key=lambda item: item.as_posix())
        except OSError as exc:
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                f"Не удалось перечислить файлы набора исходников: {relative.as_posix()}.",
            ) from exc
        for child in children:
            if child.name in {"__pycache__", ".pytest_cache"}:
                continue
            if child.is_dir():
                continue
            if child.suffix in {".pyc", ".pyo"}:
                continue
            if is_unsafe_path(child):
                raise ToolingError(
                    ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                    f"Набор исходников содержит символическую ссылку или точку повторной обработки: {child.relative_to(root).as_posix()}.",
                )
            if not child.is_file():
                raise ToolingError(
                    ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                    "Набор исходников содержит объект неизвестного типа.",
                )
            files.append(child)
    unique = {item.resolve(strict=False): item for item in files}
    return tuple(sorted(unique.values(), key=lambda item: item.relative_to(root).as_posix()))


def _source_file_bytes(root: Path, path: Path) -> bytes:
    relative = path.relative_to(root).as_posix()
    raw = bounded_read_bytes(path)
    if relative == PLUGIN_MANIFEST_PATH.as_posix():
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Манифест плагина не является корректным JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Корень манифеста плагина должен быть объектом.",
            )
        payload = dict(payload)
        payload.pop("version", None)
        return canonical_json(payload).encode("utf-8")
    # Git может выдавать один и тот же blob с разными окончаниями строк из-за
    # core.autocrlf. Дайджест набора исходников должен описывать содержимое Git,
    # а не локальную нормализацию рабочей копии; бинарные файлы не преобразуем.
    if b"\x00" not in raw:
        return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return raw


def source_set_digest(root: Path | str, source_set: str) -> str:
    """Вычислить дайджест содержимого одного явно заданного набора исходников."""

    root_path = Path(root).resolve()
    try:
        paths = SOURCE_SET_PATHS[source_set]
    except KeyError as exc:
        raise ToolingError(
            ResultCode.MCP_SOURCE_BUNDLE_INVALID,
            f"Неизвестный набор исходников MCP: {source_set}.",
        ) from exc
    entries: list[str] = []
    for path in _files_for_source_set(root_path, paths):
        content = _source_file_bytes(root_path, path)
        entries.append(
            f"{path.relative_to(root_path).as_posix()}\0"
            f"{hashlib.sha256(content).hexdigest()}\0"
            f"{len(content)}\n"
        )
    return sha256_text("".join(entries))


def source_set_digests(root: Path | str) -> dict[str, str]:
    """Вычислить дайджесты всех наборов исходников в стабильном порядке."""

    root_path = Path(root).resolve()
    return {
        name: source_set_digest(root_path, name)
        for name in SOURCE_SET_NAMES
    }


def classify_source_changes(paths: Iterable[str | Path]) -> SourceChangeClassification:
    """Определить обязательные производные файлы и затронутые семейства серверов."""

    normalized = tuple(
        sorted(
            {
                str(Path(path).as_posix()).removeprefix("./")
                for path in paths
                if str(path).strip()
            }
        )
    )
    changed: set[str] = set()
    affected: set[str] = set()
    unknown: list[str] = []
    for raw_path in normalized:
        path = Path(raw_path)
        matched = False
        for source_set, roots in SOURCE_SET_PATHS.items():
            if any(path == root or root in path.parents for root in roots):
                changed.add(source_set)
                matched = True
        if not matched:
            unknown.append(raw_path)
    for server, source_sets in SERVER_SOURCE_SETS.items():
        if set(source_sets) & changed:
            affected.add(server)
    return SourceChangeClassification(
        changed_components=tuple(sorted(changed)),
        affected_servers=tuple(name for name in MCP_SERVER_NAMES if name in affected),
        plugin_changed=bool(
            {"PLUGIN_BUNDLE_SOURCE_SET", "SKILL_BUNDLE_SOURCE_SET"} & changed
        ),
        skill_changed="SKILL_BUNDLE_SOURCE_SET" in changed,
        unknown_paths=tuple(unknown),
    )


def _working_tree_paths(git: GitClient) -> tuple[str, ...]:
    """Получить пути изменений в индексе, рабочей копии и неотслеживаемых файлов, сохранив исходное имя при переименовании."""

    tokens = [token for token in git.status_z().split("\x00") if token]
    paths: set[str] = set()
    index = 0
    while index < len(tokens):
        record = tokens[index]
        if len(record) < 4 or record[2] != " ":
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Команда Git status вернула запись в неподдерживаемом формате.",
            )
        status = record[:2]
        path = record[3:]
        if path:
            paths.add(path)
        index += 1
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            if index >= len(tokens) or not tokens[index]:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Команда Git status не содержит исходный путь переименованного или скопированного файла.",
                )
            paths.add(tokens[index])
            index += 1
    return tuple(sorted(paths))


def _candidate_mcp_impact(
    root: Path, *, base_commit: str
) -> McpImpactDetails:
    """Классифицировать зафиксированные изменения и рабочую копию относительно точной базы."""

    if _REVISION_RE.fullmatch(base_commit) is None:
        raise ToolingError(
            ResultCode.TOOLING_INVALID_INVOCATION,
            "База проверки влияния MCP должна быть указана полным SHA.",
        )
    git = GitClient(root)
    head = git.head()
    if not git.is_ancestor(base_commit, head):
        raise ToolingError(
            ResultCode.MCP_SOURCE_BUNDLE_INVALID,
            "База проверки влияния MCP не является предком текущего HEAD.",
        )
    committed_paths = git.changed_paths(base_commit, head)
    working_tree_paths = _working_tree_paths(git)
    candidate_paths = tuple(sorted(set(committed_paths) | set(working_tree_paths)))
    classification = classify_source_changes(candidate_paths)
    path_impacts = tuple(
        impact
        for path in candidate_paths
        for path_classification in (classify_source_changes((path,)),)
        if path_classification.changed_components
        for impact in (
            McpImpactPath(
                path=path,
                source_sets=path_classification.changed_components,
                affected_servers=path_classification.affected_servers,
            ),
        )
    )
    required = bool(classification.changed_components)
    return McpImpactDetails(
        base_sha=base_commit,
        head_sha=head,
        status="REQUIRED" if required else "NOT_REQUIRED",
        candidate_path_count=len(candidate_paths),
        candidate_paths=candidate_paths[:_MCP_IMPACT_SAMPLE_LIMIT],
        committed_paths=committed_paths[:_MCP_IMPACT_SAMPLE_LIMIT],
        working_tree_paths=working_tree_paths[:_MCP_IMPACT_SAMPLE_LIMIT],
        path_impacts=path_impacts[:_MCP_IMPACT_SAMPLE_LIMIT],
        changed_components=classification.changed_components,
        affected_servers=classification.affected_servers,
        generated_artifacts=(
            tuple(path.as_posix() for path in MCP_GENERATED_ARTIFACTS)
            if required
            else ()
        ),
        reconciliation_required=required,
    )


def _contract_and_tools(name: str) -> tuple[dict[str, object], list[object]]:
    if name == "azurpilot-dev":
        from module.dev_mcp.contract import contract_payload
        from module.dev_mcp.server import tool_definitions
    elif name == "azurpilot-game":
        from module.game_mcp.contract import contract_payload
        from module.game_mcp.server import tool_definitions
    else:  # pragma: no cover - все вызовы используют закрытый каталог серверов.
        raise ToolingError(ResultCode.MCP_SOURCE_BUNDLE_INVALID, "Неизвестный MCP-сервер.")
    tools = tool_definitions()
    return dict(contract_payload()), tools


def _server_source_digest(digests: Mapping[str, str], name: str) -> str:
    return sha256_text(
        canonical_json({key: digests[key] for key in SERVER_SOURCE_SETS[name]})
    )


def _server_version(server: McpServerVersion, *, bump: Literal["patch", "minor", "major"] | None) -> str:
    if bump is None:
        return server.version
    parsed = SemVer.parse(server.version)
    if bump == "major":
        return str(SemVer(parsed.major + 1, 0, 0))
    if bump == "minor":
        return str(SemVer(parsed.major, parsed.minor + 1, 0))
    return str(SemVer(parsed.major, parsed.minor, parsed.patch + 1))


def _range_for(version: str) -> str:
    parsed = SemVer.parse(version)
    return f">={parsed.major}.{parsed.minor}.{parsed.patch},<{parsed.major + 1}.0.0"


def _deterministic_plugin_version(material: str) -> str:
    value = int(material[:16], 16) % 10**14
    return f"0.1.0+codex.{value:014d}"


def _server_model(
    name: str,
    payload: Mapping[str, object],
    tools: Iterable[object],
    *,
    version: str,
    source_digest: str,
) -> McpServerVersion:
    descriptor_hash = tool_catalog_sha256_from_tools(tools)
    descriptor_hashes = tool_descriptor_hashes_from_tools(tools)
    names = tool_names_from_tools(tools)
    capability = dict(payload)
    capability["tool_catalog_sha256"] = descriptor_hash
    capability["tool_count"] = len(names)
    capability_hash = capability_catalog_sha256(capability)
    capability["capability_catalog_sha256"] = capability_hash
    capability["server_version"] = version
    revision = contract_revision(capability)
    api_key = "dev_mcp_api_version" if name == "azurpilot-dev" else "game_mcp_api_version"
    vocabulary_key = "result_outcomes" if name == "azurpilot-dev" else "result_states"
    return McpServerVersion(
        name=name,
        version=version,
        api_version=_required_int(payload.get(api_key), f"{name}.api_version"),
        contract_schema_version=_required_int(
            payload.get("contract_schema_version"), f"{name}.contract_schema_version"
        ),
        tool_catalog_sha256=descriptor_hash,
        tool_names=names,
        tool_descriptor_hashes=descriptor_hashes,
        capability_catalog_sha256=capability_hash,
        contract_revision=revision,
        source_set_digest=source_digest,
        authorization_scopes=_required_strings(
            payload.get("authorization_scopes"), f"{name}.authorization_scopes"
        ),
        feature_flags=_required_flags(payload.get("feature_flags"), f"{name}.feature_flags"),
        capability_families=_required_strings(
            payload.get("capability_families"), f"{name}.capability_families"
        ),
        result_vocabulary=_required_strings(
            payload.get(vocabulary_key), f"{name}.{vocabulary_key}"
        ),
        smoke_spec_schema_version=(
            _required_int(
                payload.get("smoke_spec_schema_version"),
                f"{name}.smoke_spec_schema_version",
            )
            if name == "azurpilot-dev"
            else None
        ),
        smoke_result_schema_version=(
            _required_int(
                payload.get("smoke_result_schema_version"),
                f"{name}.smoke_result_schema_version",
            )
            if name == "azurpilot-dev"
            else None
        ),
    )


def _required_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolingError(ResultCode.MCP_SOURCE_BUNDLE_INVALID, f"{label} должно быть целым числом.")
    return value


def _required_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ToolingError(ResultCode.MCP_SOURCE_BUNDLE_INVALID, f"{label} имеет неверный список.")
    result = tuple(item.strip() for item in value)
    if len(set(result)) != len(result):
        raise ToolingError(ResultCode.MCP_SOURCE_BUNDLE_INVALID, f"{label} содержит повторения.")
    return result


def _required_flags(value: object, label: str) -> Mapping[str, bool]:
    if not isinstance(value, dict) or not value or any(
        not isinstance(key, str) or not isinstance(item, bool)
        for key, item in value.items()
    ):
        raise ToolingError(ResultCode.MCP_SOURCE_BUNDLE_INVALID, f"{label} должно быть словарём.")
    return dict(value)


def _current_bundle(root: Path) -> McpBundle | None:
    try:
        return load_mcp_bundle(root)
    except VersioningError:
        return None


def _public_change_kind(
    old: McpServerVersion | None,
    new: McpServerVersion,
) -> Literal["none", "patch", "minor", "major"]:
    if old is None:
        return "none"
    if old.version != new.version:
        # Сама версия уже выражает намерение разработчика; не вычисляем
        # дополнительное повышение версии при проверке уже согласованных исходников.
        return "none"
    if (
        old.tool_catalog_sha256 == new.tool_catalog_sha256
        and dict(old.tool_descriptor_hashes) == dict(new.tool_descriptor_hashes)
        and old.capability_catalog_sha256 == new.capability_catalog_sha256
        and old.api_version == new.api_version
        and old.contract_schema_version == new.contract_schema_version
        and old.authorization_scopes == new.authorization_scopes
        and dict(old.feature_flags) == dict(new.feature_flags)
        and old.capability_families == new.capability_families
        and old.result_vocabulary == new.result_vocabulary
        and old.smoke_spec_schema_version == new.smoke_spec_schema_version
        and old.smoke_result_schema_version == new.smoke_result_schema_version
    ):
        return "patch" if old.source_set_digest != new.source_set_digest else "none"
    old_names = set(old.tool_names)
    new_names = set(new.tool_names)
    additive_capabilities = (
        old_names <= new_names
        and set(old.capability_families) <= set(new.capability_families)
        and set(old.result_vocabulary) <= set(new.result_vocabulary)
        and all(
            name in new.feature_flags and new.feature_flags[name] == value
            for name, value in old.feature_flags.items()
        )
        and old.authorization_scopes == new.authorization_scopes
        and old.api_version == new.api_version
        and old.contract_schema_version == new.contract_schema_version
        and old.smoke_spec_schema_version == new.smoke_spec_schema_version
        and old.smoke_result_schema_version == new.smoke_result_schema_version
    )
    # Изменённый хеш описания при тех же именах может означать
    # несовместимость входной или выходной схемы. Без семантического сравнения
    # безопаснее потребовать явное повышение версии major, чем делать предположения.
    existing_descriptors_unchanged = (
        set(old.tool_descriptor_hashes) == old_names
        and set(new.tool_descriptor_hashes) == new_names
        and old_names <= new_names
        and all(
            old.tool_descriptor_hashes[name] == new.tool_descriptor_hashes[name]
            for name in old_names
        )
    )
    if additive_capabilities and old_names < new_names and existing_descriptors_unchanged:
        return "minor"
    return "major"


def _base_public_contract_equal(
    old: McpServerVersion, new: McpServerVersion
) -> bool:
    """Сравнить публичный контракт, не учитывая производную ревизию версии."""

    return all(
        left == right
        for left, right in (
            (old.tool_catalog_sha256, new.tool_catalog_sha256),
            (dict(old.tool_descriptor_hashes), dict(new.tool_descriptor_hashes)),
            (old.capability_catalog_sha256, new.capability_catalog_sha256),
            (old.api_version, new.api_version),
            (old.contract_schema_version, new.contract_schema_version),
            (old.authorization_scopes, new.authorization_scopes),
            (dict(old.feature_flags), dict(new.feature_flags)),
            (old.capability_families, new.capability_families),
            (old.result_vocabulary, new.result_vocabulary),
            (old.smoke_spec_schema_version, new.smoke_spec_schema_version),
            (old.smoke_result_schema_version, new.smoke_result_schema_version),
        )
    )


def _base_public_change_kind(
    old: McpServerVersion | None,
    new: McpServerVersion,
    *,
    source_changed: bool,
) -> Literal["none", "patch", "minor", "major"]:
    """Классифицировать изменения от базы до текущей ревизии независимо от версии, указанной вручную."""

    if old is None:
        return "major"
    if _base_public_contract_equal(old, new):
        return "patch" if source_changed else "none"

    old_names = set(old.tool_names)
    new_names = set(new.tool_names)
    additive_capabilities = (
        old_names <= new_names
        and set(old.capability_families) <= set(new.capability_families)
        and set(old.result_vocabulary) <= set(new.result_vocabulary)
        and all(
            name in new.feature_flags and new.feature_flags[name] == value
            for name, value in old.feature_flags.items()
        )
        and old.authorization_scopes == new.authorization_scopes
        and old.api_version == new.api_version
        and old.contract_schema_version == new.contract_schema_version
        and old.smoke_spec_schema_version == new.smoke_spec_schema_version
        and old.smoke_result_schema_version == new.smoke_result_schema_version
    )
    existing_descriptors_unchanged = (
        set(old.tool_descriptor_hashes) == old_names
        and set(new.tool_descriptor_hashes) == new_names
        and old_names <= new_names
        and all(
            old.tool_descriptor_hashes[name] == new.tool_descriptor_hashes[name]
            for name in old_names
        )
    )
    if additive_capabilities and old_names < new_names and existing_descriptors_unchanged:
        return "minor"
    return "major"


def _version_bump_kind(
    old: str, new: str
) -> Literal["none", "patch", "minor", "major", "invalid"]:
    """Определить фактическое повышение SemVer без учёта метаданных сборки."""

    previous = SemVer.parse(old)
    current = SemVer.parse(new)
    previous_core = (previous.major, previous.minor, previous.patch)
    current_core = (current.major, current.minor, current.patch)
    if current_core < previous_core:
        return "invalid"
    if current.major > previous.major:
        return "major"
    if current.minor > previous.minor:
        return "minor"
    if current.patch > previous.patch:
        return "patch"
    return "none"


def _legacy_baseline_versions(content: bytes) -> dict[str, str] | None:
    """Прочитать только версии старой схемы v1 для проверки совместимости с базой."""

    try:
        payload = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if payload.get("schema_version") != 1:
        return None
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, dict) or set(raw_servers) != set(MCP_SERVER_NAMES):
        return None
    versions: dict[str, str] = {}
    try:
        for name in MCP_SERVER_NAMES:
            value = raw_servers[name]
            if not isinstance(value, dict):
                return None
            versions[name] = str(SemVer.parse(value["version"]))
    except (KeyError, TypeError, ValueError):
        return None
    return versions


def _server_status_from_model(
    server: McpServerVersion,
    *,
    status: Literal[
        "ready",
        "stale",
        "stopped",
        "unavailable",
        "not_configured",
        "unknown",
        "conflict",
    ],
    observed_version: str | None = None,
    observed_source_revision: str | None = None,
    routes: tuple[Literal["stdio", "loopback_http", "public_https"], ...] = (
        "loopback_http",
    ),
    authentication: Literal["configured", "unavailable", "unknown"] = "unknown",
    ownership_confirmed: bool = False,
    reason_code: str | None = None,
) -> McpServerStatus:
    return McpServerStatus(
        server_name=server.name,
        expected_version=server.version,
        observed_version=observed_version,
        source_revision=_safe_source_revision(observed_source_revision),
        status=status,
        source_set_digest=server.source_set_digest,
        tool_catalog_sha256=server.tool_catalog_sha256,
        capability_catalog_sha256=server.capability_catalog_sha256,
        contract_revision=server.contract_revision,
        routes=routes,
        authentication=authentication,
        ownership_confirmed=ownership_confirmed,
        reason_code=reason_code,
    )


def _safe_source_revision(value: object) -> str | None:
    """Вернуть только ограниченную ревизию Git из метаданных среды выполнения."""

    if not isinstance(value, str):
        return None
    revision = value.strip().lower()
    return revision if _SHA_RE.fullmatch(revision) else None


def _runtime_service_matches(
    item: object, expected: McpServerVersion
) -> bool:
    if not isinstance(item, Mapping) or item.get("ready") is not True:
        return False
    matches = all(
        item.get(field) == value
        for field, value in (
            ("server_name", expected.name),
            ("server_version", expected.version),
            ("source_set_digest", expected.source_set_digest),
            ("tool_catalog_sha256", expected.tool_catalog_sha256),
            ("capability_catalog_sha256", expected.capability_catalog_sha256),
            ("contract_revision", expected.contract_revision),
        )
    )
    return matches


def _digest_models(digests: Mapping[str, str]) -> tuple[McpDigest, ...]:
    return tuple(McpDigest(name=name, sha256=value) for name, value in digests.items())


def _render_toml(bundle: McpBundle) -> str:
    def string(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    def array(values: Iterable[object]) -> str:
        return "[" + ", ".join(string(str(value)) for value in values) + "]"

    def flags(values: Mapping[str, bool]) -> str:
        return "{" + ", ".join(
            f"{string(name)} = {'true' if value else 'false'}"
            for name, value in values.items()
        ) + "}"

    def strings(values: Mapping[str, str]) -> str:
        return "{" + ", ".join(
            f"{string(name)} = {string(value)}" for name, value in values.items()
        ) + "}"

    lines = [
        f"schema_version = {bundle.schema_version}",
        f"bundle_revision = {string(bundle.bundle_revision)}",
        f"plugin_version = {string(bundle.plugin_version)}",
        f"skill_bundle_revision = {string(bundle.skill_bundle_revision)}",
        "",
    ]
    for name in MCP_SERVER_NAMES:
        server = bundle.servers[name]
        lines.extend(
            [
                f"[servers.{name}]",
                f"version = {string(server.version)}",
                f"api_version = {server.api_version}",
                f"contract_schema_version = {server.contract_schema_version}",
                f"tool_catalog_sha256 = {string(server.tool_catalog_sha256)}",
                f"tool_names = {array(server.tool_names)}",
                f"tool_descriptor_hashes = {strings(server.tool_descriptor_hashes)}",
                f"capability_catalog_sha256 = {string(server.capability_catalog_sha256)}",
                f"contract_revision = {string(server.contract_revision)}",
                f"source_set_digest = {string(server.source_set_digest)}",
                f"authorization_scopes = {array(server.authorization_scopes)}",
                f"feature_flags = {flags(server.feature_flags)}",
                f"capability_families = {array(server.capability_families)}",
                f"result_vocabulary = {array(server.result_vocabulary)}",
            ]
        )
        if server.name == "azurpilot-dev":
            lines.extend(
                [
                    f"smoke_spec_schema_version = {server.smoke_spec_schema_version}",
                    f"smoke_result_schema_version = {server.smoke_result_schema_version}",
                ]
            )
        lines.append("")
    lines.extend(
        [
            "[compatibility]",
            "required_mcp_servers = {"
            + ", ".join(
                f"{string(name)} = {string(_range_for(bundle.servers[name].version))}"
                for name in MCP_SERVER_NAMES
            )
            + "}",
            "",
        ]
    )
    for name in MCP_SERVER_NAMES:
        policy = bundle.compatibility[name]
        lines.extend(
            [
                f"[compatibility.{name}]",
                f"required_feature_flags = {flags(policy.required_feature_flags)}",
                f"required_capability_families = {array(policy.required_capability_families)}",
                f"result_vocabulary = {array(policy.result_vocabulary)}",
                "",
            ]
        )
    lines.append("[source_digests]")
    for name, digest in bundle.source_digests.items():
        lines.append(f"{name} = {string(digest)}")
    return "\n".join(lines).rstrip() + "\n"


def _render_plugin_compatibility(bundle: McpBundle) -> str:
    dev = bundle.servers["azurpilot-dev"]
    result: dict[str, object] = {
        "contract_schema_version": dev.contract_schema_version,
        "product_family": "AzurPilot",
        "plugin_version": bundle.plugin_version,
        "bundle_revision": bundle.bundle_revision,
        "skill_bundle_revision": bundle.skill_bundle_revision,
        "required_mcp_servers": {
            name: _range_for(bundle.servers[name].version) for name in MCP_SERVER_NAMES
        },
        "servers": {
            name: {
                "version": bundle.servers[name].version,
                "api_version": bundle.servers[name].api_version,
                "contract_schema_version": bundle.servers[name].contract_schema_version,
                "tool_count": len(bundle.servers[name].tool_names),
                "tool_catalog_sha256": bundle.servers[name].tool_catalog_sha256,
                "tool_descriptor_hashes": dict(bundle.servers[name].tool_descriptor_hashes),
                "capability_catalog_sha256": bundle.servers[name].capability_catalog_sha256,
                "contract_revision": bundle.servers[name].contract_revision,
                "source_set_digest": bundle.servers[name].source_set_digest,
            }
            for name in MCP_SERVER_NAMES
        },
        "required_feature_flags": dict(dev.feature_flags),
        "required_capability_families": list(dev.capability_families),
        "result_outcomes": list(dev.result_vocabulary),
        "smoke_spec_schema_version": dev.smoke_spec_schema_version,
        "smoke_result_schema_version": dev.smoke_result_schema_version,
        "required_feature_flags_by_server": {
            name: dict(bundle.compatibility[name].required_feature_flags)
            for name in MCP_SERVER_NAMES
        },
        "required_capability_families_by_server": {
            name: list(bundle.compatibility[name].required_capability_families)
            for name in MCP_SERVER_NAMES
        },
        "result_vocabulary_by_server": {
            name: list(bundle.compatibility[name].result_vocabulary)
            for name in MCP_SERVER_NAMES
        },
        "tool_catalog_sha256": {
            name: bundle.servers[name].tool_catalog_sha256 for name in MCP_SERVER_NAMES
        },
        "capability_catalog_sha256": {
            name: bundle.servers[name].capability_catalog_sha256 for name in MCP_SERVER_NAMES
        },
        "contract_revision": {
            name: bundle.servers[name].contract_revision for name in MCP_SERVER_NAMES
        },
        "source_digests": dict(bundle.source_digests),
    }
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def _read_plugin_manifest(root: Path) -> dict[str, object]:
    try:
        payload = json.loads(bounded_read_text(root / PLUGIN_MANIFEST_PATH))
    except (ToolingError, json.JSONDecodeError, UnicodeError) as exc:
        raise ToolingError(
            ResultCode.MCP_SOURCE_BUNDLE_INVALID,
            "Манифест плагина имеет неверный формат.",
        ) from exc
    if not isinstance(payload, dict):
        raise ToolingError(ResultCode.MCP_SOURCE_BUNDLE_INVALID, "Манифест плагина должен быть объектом.")
    return payload


def _build_bundle(
    root: Path,
    requested_bump: str | None,
    *,
    baseline: _McpBaseline | None = None,
    base_commit: str | None = None,
) -> _BundleBuild:
    digests = source_set_digests(root)
    current_bundle = _current_bundle(root)
    if current_bundle is None:
        raise ToolingError(
            ResultCode.MCP_SOURCE_BUNDLE_INVALID,
            "Канонический комплект MCP отсутствует или имеет неверную схему.",
        )
    old_bundle = baseline.bundle if baseline and baseline.bundle else current_bundle
    old_servers = old_bundle.servers
    base_versions = {
        name: baseline.versions[name] if baseline else server.version
        for name, server in old_servers.items()
    }

    legacy_changed_servers: set[str] = set()
    if baseline is not None and baseline.bundle is None and base_commit is not None:
        git = GitClient(root)
        paths = set(git.changed_paths(base_commit, git.head()))
        paths.update(_working_tree_paths(git))
        legacy_changed_servers = set(
            classify_source_changes(paths).affected_servers
        )

    raw_models: dict[str, McpServerVersion] = {}
    contracts: dict[str, dict[str, object]] = {}
    for name in MCP_SERVER_NAMES:
        payload, tools = _contract_and_tools(name)
        contracts[name] = payload
        raw_models[name] = _server_model(
            name,
            payload,
            tools,
            version=base_versions[name],
            source_digest=_server_source_digest(digests, name),
        )

    kinds: dict[str, Literal["none", "patch", "minor", "major"]] = {}
    for name in MCP_SERVER_NAMES:
        if baseline is None:
            kinds[name] = _public_change_kind(
                current_bundle.servers.get(name), raw_models[name]
            )
        elif baseline.bundle is not None:
            old_server = baseline.bundle.servers.get(name)
            source_changed = old_server is not None and (
                old_server.source_set_digest != raw_models[name].source_set_digest
            )
            kinds[name] = _base_public_change_kind(
                old_server,
                raw_models[name],
                source_changed=source_changed,
            )
        else:
            # Базовая схема v1 не содержит отпечатков публичного контракта.
            # Сохраняем консервативное правило: изменение исходников сервера
            # требует повышения patch, а повторная синхронизация снова начинается
            # с той же точной базовой ревизии.
            kinds[name] = "patch" if name in legacy_changed_servers else "none"
    required_major = tuple(name for name, kind in kinds.items() if kind == "major")
    requested = requested_bump or "auto"
    if required_major and requested != "major" and not (
        requested == "auto" and baseline is not None
    ):
        raise ToolingError(
            ResultCode.MCP_VERSION_BUMP_REQUIRED,
            "Изменение MCP-контракта требует явного --bump major.",
        )

    final_models: dict[str, McpServerVersion] = {}
    for name in MCP_SERVER_NAMES:
        kind = kinds[name]
        bump: Literal["patch", "minor", "major"] | None = None
        if kind != "none":
            bump = kind
            if requested in {"patch", "minor", "major"}:
                rank = {"patch": 1, "minor": 2, "major": 3}
                if rank[requested] < rank[kind]:
                    raise ToolingError(
                        ResultCode.MCP_VERSION_BUMP_REQUIRED,
                        f"Изменение требует как минимум --bump {kind}.",
                    )
                # Явно заданная политика может выбрать более высокое совместимое повышение.
                bump = requested
        final_version = _server_version(raw_models[name], bump=bump)
        final_models[name] = _server_model(
            name,
            contracts[name],
            _contract_and_tools(name)[1],
            version=final_version,
            source_digest=raw_models[name].source_set_digest,
        )

    plugin_material = {
        "plugin_source_digest": digests["PLUGIN_BUNDLE_SOURCE_SET"],
        "skill_bundle_revision": digests["SKILL_BUNDLE_SOURCE_SET"],
        "servers": {
            name: {
                "version": final_models[name].version,
                "contract_revision": final_models[name].contract_revision,
                "tool_catalog_sha256": final_models[name].tool_catalog_sha256,
                "capability_catalog_sha256": final_models[name].capability_catalog_sha256,
            }
            for name in MCP_SERVER_NAMES
        },
    }
    plugin_revision = sha256_text(canonical_json(plugin_material))
    plugin_version = _deterministic_plugin_version(plugin_revision)
    bundle_material = {
        "servers": {
            name: _server_bundle_payload(final_models[name]) for name in MCP_SERVER_NAMES
        },
        "plugin_revision": plugin_revision,
        "plugin_version": plugin_version,
        "skill_bundle_revision": digests["SKILL_BUNDLE_SOURCE_SET"],
        "source_digests": digests,
    }
    bundle_revision = sha256_text(canonical_json(bundle_material))
    compatibility = {
        name: McpCompatibility(
            required_feature_flags=final_models[name].feature_flags,
            required_capability_families=final_models[name].capability_families,
            result_vocabulary=final_models[name].result_vocabulary,
        )
        for name in MCP_SERVER_NAMES
    }
    bundle = McpBundle(
        schema_version=2,
        bundle_revision=bundle_revision,
        plugin_version=plugin_version,
        skill_bundle_revision=digests["SKILL_BUNDLE_SOURCE_SET"],
        servers=final_models,
        compatibility=compatibility,
        source_digests=digests,
    )
    # Набор изменений определяется сравнением дайджестов, а не предполагаемым
    # диапазоном Git. Поэтому согласование работает и во временной копии.
    changed_components = tuple(
        name
        for name in SOURCE_SET_NAMES
        if current_bundle.source_digests.get(name) != digests[name]
    )
    affected = tuple(
        name
        for name in MCP_SERVER_NAMES
        if current_bundle.servers[name].source_set_digest != final_models[name].source_set_digest
        or current_bundle.servers[name].contract_revision != final_models[name].contract_revision
    )
    plugin_changed = (
        current_bundle.source_digests.get("PLUGIN_BUNDLE_SOURCE_SET")
        != digests["PLUGIN_BUNDLE_SOURCE_SET"]
        or current_bundle.source_digests.get("SKILL_BUNDLE_SOURCE_SET")
        != digests["SKILL_BUNDLE_SOURCE_SET"]
    )
    skill_changed = current_bundle.skill_bundle_revision != bundle.skill_bundle_revision
    plugin_payload = _read_plugin_manifest(root)
    plugin_payload["version"] = bundle.plugin_version
    plugin_manifest_text = json.dumps(plugin_payload, ensure_ascii=False, indent=2) + "\n"
    return _BundleBuild(
        bundle=bundle,
        manifest_text=_render_toml(bundle),
        plugin_manifest_text=plugin_manifest_text,
        compatibility_text=_render_plugin_compatibility(bundle),
        changed_components=tuple(sorted(changed_components)),
        affected_servers=affected,
        plugin_changed=plugin_changed,
        skill_changed=skill_changed,
        required_bump_servers=required_major,
    )


def _server_bundle_payload(server: McpServerVersion) -> dict[str, object]:
    return {
        "version": server.version,
        "api_version": server.api_version,
        "contract_schema_version": server.contract_schema_version,
        "tool_catalog_sha256": server.tool_catalog_sha256,
        "capability_catalog_sha256": server.capability_catalog_sha256,
        "contract_revision": server.contract_revision,
        "source_set_digest": server.source_set_digest,
    }


class McpSourceReconciler:
    """Генератор и строгая проверка производных файлов MCP с отказом при сомнении."""

    def build(
        self,
        root: Path | str,
        *,
        requested_bump: str | None = None,
        base_commit: str | None = None,
    ) -> _BundleBuild:
        resolved = Path(root).resolve()
        if requested_bump not in {None, "auto", "patch", "minor", "major"}:
            raise ToolingError(ResultCode.TOOLING_INVALID_INVOCATION, "Неизвестная политика повышения версии MCP.")
        baseline = self._baseline(resolved, base_commit) if base_commit else None
        return _build_bundle(
            resolved,
            requested_bump,
            baseline=baseline,
            base_commit=base_commit,
        )

    def check(
        self, root: Path | str, *, build: _BundleBuild | None = None
    ) -> _BundleBuild:
        resolved = Path(root).resolve()
        try:
            current = load_mcp_bundle(resolved)
        except VersioningError as exc:
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Канонический комплект MCP имеет неверный формат.",
            ) from exc
        build = build or self.build(resolved, requested_bump="auto")
        expected_plugin = _render_plugin_compatibility(build.bundle)
        checks = (
            (resolved / "config" / "mcp-versions.toml", build.manifest_text),
            (resolved / PLUGIN_MANIFEST_PATH, build.plugin_manifest_text),
            (resolved / PLUGIN_COMPATIBILITY_PATH, expected_plugin),
        )
        for path, expected in checks:
            try:
                actual = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ToolingError(
                    ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
                    "Отсутствует производный файл MCP.",
                    details=_source_drift_details(resolved, path, build, current),
                ) from exc
            if actual != expected:
                code = (
                    ResultCode.MCP_VERSION_BUMP_REQUIRED
                    if build.required_bump_servers
                    else ResultCode.MCP_SOURCE_BUNDLE_DRIFT
                )
                raise ToolingError(
                    code,
                    "Производный файл MCP устарел.",
                    details=_source_drift_details(resolved, path, build, current),
                )
        if current.bundle_revision != build.bundle.bundle_revision:
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
                "Ревизия комплекта не совпадает с содержимым исходников.",
                details=_source_drift_details(
                    resolved,
                    PLUGIN_COMPATIBILITY_PATH,
                    build,
                    current,
                ),
            )
        return build

    def delivery_scope_dependencies(
        self,
        root: Path | str,
        *,
        selected_paths: Iterable[str | Path],
        candidate_paths: Iterable[str | Path],
    ) -> tuple[str, ...]:
        """Дополнить область изменений MCP всеми зависимыми исходниками и производными файлами.

        Производные метаданные описывают весь вариант MCP. Если явная область
        затрагивает набор исходников MCP или производный файл, сначала проверяем,
        что комплект соответствует всем изменениям рабочей копии, затем включаем
        изменённые исходники MCP и производные файлы в тот же коммит.
        Обычная область без MCP не расширяется.
        """

        def normalize(paths: Iterable[str | Path]) -> set[str]:
            return {
                Path(path).as_posix().removeprefix("./")
                for path in paths
                if str(path).strip()
            }

        selected = normalize(selected_paths)
        candidates = normalize(candidate_paths)
        generated = {path.as_posix() for path in MCP_GENERATED_ARTIFACTS}
        selected_source = classify_source_changes(selected).changed_components
        if not selected_source and not selected.intersection(generated):
            return ()

        candidate_sources = {
            path
            for path in candidates
            if classify_source_changes((path,)).changed_components
        }
        candidate_generated = candidates.intersection(generated)
        self.check(root)
        return tuple(sorted((candidate_sources | candidate_generated) - selected))

    @staticmethod
    def _baseline(root: Path, base_commit: str) -> _McpBaseline:
        if _REVISION_RE.fullmatch(base_commit) is None:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Базовый коммит для проверки MCP должен быть указан полным SHA.",
            )
        git = GitClient(root)
        head = git.head()
        if not git.is_ancestor(base_commit, head):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Базовый коммит не является предком текущего HEAD MCP.",
            )
        manifest_ref = f"{base_commit}:config/mcp-versions.toml"
        if not git.object_exists(manifest_ref):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Базовый коммит не содержит канонический манифест версий MCP.",
            )
        content = git.object_bytes(manifest_ref)
        try:
            bundle = load_mcp_bundle_bytes(content)
        except VersioningError:
            versions = _legacy_baseline_versions(content)
            if versions is None:
                raise ToolingError(
                    ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                    "Манифест версий MCP базового коммита имеет неизвестную схему.",
                ) from None
            plugin_version: str | None = None
            bundle_revision: str | None = None
            skill_bundle_revision: str | None = None
            plugin_ref = f"{base_commit}:{PLUGIN_MANIFEST_PATH.as_posix()}"
            try:
                plugin_payload = json.loads(git.object_bytes(plugin_ref).decode("utf-8"))
                if isinstance(plugin_payload, dict) and isinstance(
                    plugin_payload.get("version"), str
                ):
                    plugin_version = plugin_payload["version"]
            except (ToolingError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            compatibility_ref = (
                f"{base_commit}:{PLUGIN_COMPATIBILITY_PATH.as_posix()}"
            )
            try:
                compatibility_payload = json.loads(
                    git.object_bytes(compatibility_ref).decode("utf-8")
                )
                if isinstance(compatibility_payload, dict):
                    if isinstance(compatibility_payload.get("bundle_revision"), str):
                        bundle_revision = compatibility_payload["bundle_revision"]
                    if isinstance(
                        compatibility_payload.get("skill_bundle_revision"), str
                    ):
                        skill_bundle_revision = compatibility_payload[
                            "skill_bundle_revision"
                        ]
            except (ToolingError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            return _McpBaseline(
                versions=versions,
                bundle=None,
                plugin_version=plugin_version,
                bundle_revision=bundle_revision,
                skill_bundle_revision=skill_bundle_revision,
            )
        return _McpBaseline(
            versions={name: server.version for name, server in bundle.servers.items()},
            bundle=bundle,
            plugin_version=bundle.plugin_version,
            bundle_revision=bundle.bundle_revision,
            skill_bundle_revision=bundle.skill_bundle_revision,
        )

    def check_base_to_head(
        self,
        root: Path | str,
        *,
        base_commit: str,
        build: _BundleBuild | None = None,
    ) -> McpBaseCompatibility:
        """Проверить политику повышения версии и комплект от конкретной базовой ревизии."""

        resolved = Path(root).resolve()
        head = build or self.check(resolved)
        baseline = self._baseline(resolved, base_commit)
        git = GitClient(resolved)
        changed_paths = git.changed_paths(base_commit, git.head())
        classification = classify_source_changes(changed_paths)

        required: list[str] = []
        for name in MCP_SERVER_NAMES:
            old_version = baseline.versions.get(name)
            current_server = head.bundle.servers[name]
            if old_version is None:
                raise ToolingError(
                    ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                    f"Манифест MCP базового коммита не содержит сервер {name}.",
                )
            source_changed = name in classification.affected_servers
            if baseline.bundle is not None:
                old_server = baseline.bundle.servers[name]
                source_changed = source_changed or (
                    old_server.source_set_digest != current_server.source_set_digest
                )
                kind = _base_public_change_kind(
                    old_server,
                    current_server,
                    source_changed=source_changed,
                )
            else:
                # Схема v1 не содержит отпечатков. Для неё безопасно требовать
                # повышение patch при любом изменении исходников сервера, но не
                # предполагать несовместимость контракта без исходного отпечатка.
                kind = "patch" if source_changed else "none"
            actual_bump = _version_bump_kind(old_version, current_server.version)
            if actual_bump == "invalid":
                raise ToolingError(
                    ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
                    f"Версия сервера MCP {name} уменьшилась относительно базового коммита.",
                )
            rank = {"none": 0, "patch": 1, "minor": 2, "major": 3}
            if rank[actual_bump] < rank[kind]:
                required.append(name)
                continue
            if kind == "major" and actual_bump != "major":
                required.append(name)

        base_bundle = baseline.bundle
        plugin_changed = "PLUGIN_BUNDLE_SOURCE_SET" in classification.changed_components
        skill_changed = "SKILL_BUNDLE_SOURCE_SET" in classification.changed_components
        if base_bundle is not None:
            plugin_changed = plugin_changed or (
                base_bundle.source_digests.get("PLUGIN_BUNDLE_SOURCE_SET")
                != head.bundle.source_digests.get("PLUGIN_BUNDLE_SOURCE_SET")
            )
            skill_changed = skill_changed or (
                base_bundle.source_digests.get("SKILL_BUNDLE_SOURCE_SET")
                != head.bundle.source_digests.get("SKILL_BUNDLE_SOURCE_SET")
            )
        if plugin_changed and (
            baseline.plugin_version == head.bundle.plugin_version
            or baseline.bundle_revision == head.bundle.bundle_revision
        ):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
                "Изменение исходников плагина не обновило его версию и ревизию комплекта.",
            )
        if skill_changed and (
            baseline.skill_bundle_revision == head.bundle.skill_bundle_revision
            or baseline.plugin_version == head.bundle.plugin_version
            or baseline.bundle_revision == head.bundle.bundle_revision
        ):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
                "Изменение исходников навыка не обновило его версию и ревизию комплекта.",
            )

        if required:
            names = ", ".join(required)
            raise ToolingError(
                ResultCode.MCP_VERSION_BUMP_REQUIRED,
                f"Base-to-head MCP compatibility требует version bump: {names}.",
            )
        return McpBaseCompatibility(
            base_commit=base_commit,
            changed_components=classification.changed_components,
            affected_servers=classification.affected_servers,
        )

    def reconcile(
        self,
        root: Path | str,
        *,
        requested_bump: str | None = None,
        base_commit: str | None = None,
    ) -> _BundleBuild:
        resolved = Path(root).resolve()
        build = self.build(
            resolved,
            requested_bump=requested_bump,
            base_commit=base_commit,
        )
        if source_set_digests(resolved) != dict(build.bundle.source_digests):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
                "Набор исходников MCP изменился при финализации варианта; повторите синхронизацию.",
            )
        scoped = ScopedPath(resolved)
        scoped.atomic_write_text("config/mcp-versions.toml", build.manifest_text)
        scoped.atomic_write_text(PLUGIN_MANIFEST_PATH, build.plugin_manifest_text)
        scoped.atomic_write_text(PLUGIN_COMPATIBILITY_PATH, build.compatibility_text)
        if source_set_digests(resolved) != dict(build.bundle.source_digests):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
                "Набор исходников MCP изменился при записи производных файлов; повторите синхронизацию.",
            )
        return build


class McpService:
    """CLI-сервис для комплекта исходников и локального процесса управления MCP."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.runner = runner or StructuredProcessRunner()
        self.source = McpSourceReconciler()

    def _root(self, repository_root: str | Path | None) -> Path:
        return self.resolver.resolve(repository_root).path

    @staticmethod
    def _bundle(root: Path) -> McpBundle:
        try:
            return load_mcp_bundle(root)
        except VersioningError as exc:
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Канонический комплект MCP имеет неверный формат.",
            ) from exc

    @staticmethod
    def _supervisor(root: Path, server_name: str):
        from module.mcp_shared.local_http_supervisor import (
            LOCAL_HTTP_SERVICES,
            LocalHttpSupervisor,
        )

        service = next(
            (item for item in LOCAL_HTTP_SERVICES if item.name == server_name),
            None,
        )
        if service is None:  # pragma: no cover - закрытый каталог MCP-серверов
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Неизвестный первичный MCP-сервер.",
            )
        return LocalHttpSupervisor(
            root,
            python_executable=project_python(root),
            services=(service,),
            state_namespace=server_name,
        )

    @staticmethod
    def _registration_state(
        root: Path,
    ) -> Literal["ready", "invalid", "unknown"]:
        """Проверить исходник зарегистрированного маршрута без утверждений об активной сессии."""

        try:
            from dev_tools.mcp_status import first_party_source_registration

            registration = first_party_source_registration(root)
        except (ImportError, OSError, UnicodeError, ValueError, ToolingError):
            return "unknown"
        return "ready" if registration.get("status") == "ready" else "invalid"

    @staticmethod
    def _session_state(
        root: Path,
        runtime: Mapping[str, object],
        *,
        changed_paths: Iterable[str | Path] = (),
    ) -> Literal["not_observable", "reload_required"]:
        """Классифицировать снимок плагина без утверждений об активной сессии."""

        requested_paths = tuple(changed_paths)
        if requested_paths:
            classification = classify_source_changes(requested_paths)
            if classification.plugin_changed or classification.skill_changed:
                return "reload_required"

        revisions = {
            item.get("source_revision")
            for item in runtime.get("services", [])
            if isinstance(item, dict)
            and isinstance(item.get("source_revision"), str)
            and _SHA_RE.fullmatch(item["source_revision"])
        }
        if len(revisions) != 1:
            return "not_observable"
        runtime_revision = next(iter(revisions))
        if len(runtime_revision) < 40:
            return "not_observable"
        try:
            git = GitClient(root)
            current_revision = git.head()
            runtime_changed_paths = git.changed_paths(runtime_revision, current_revision)
        except ToolingError:
            return "not_observable"
        classification = classify_source_changes(runtime_changed_paths)
        return (
            "reload_required"
            if classification.plugin_changed or classification.skill_changed
            else "not_observable"
        )

    def _runtime_status(
        self, root: Path, bundle: McpBundle | None = None
    ) -> tuple[str, dict[str, object]]:
        services: list[dict[str, object]] = []
        supervisors: dict[str, dict[str, object]] = {}
        for name in MCP_SERVER_NAMES:
            supervisor = self._supervisor(root, name)
            status = supervisor.status()
            code = str(status.get("code", "LOCAL_MCP_SUPERVISOR_UNKNOWN"))
            if code == "LOCAL_MCP_SUPERVISOR_STOPPED":
                conflicts = supervisor.port_conflicts()
                if conflicts:
                    status = {
                        **status,
                        "code": "TOOLING_PORT_CONFLICT",
                        "conflicting_services": list(conflicts),
                    }
                    code = "TOOLING_PORT_CONFLICT"
            supervisors[name] = status
            observed = next(
                (
                    item
                    for item in status.get("services", [])
                    if isinstance(item, dict)
                    and item.get("server_name") == name
                ),
                None,
            )
            service = dict(observed) if observed is not None else {
                "server_name": name,
                "ready": False,
            }
            if (
                bundle is not None
                and code == "LOCAL_MCP_SUPERVISOR_READY"
                and not _runtime_service_matches(service, bundle.servers[name])
            ):
                service["ready"] = False
                service["reason_code"] = "MCP_RUNTIME_CONTRACT_DRIFT"
            services.append(service)

        codes = tuple(str(status.get("code")) for status in supervisors.values())
        if any(code == "TOOLING_PORT_CONFLICT" for code in codes):
            state = "conflict"
            aggregate_code = "TOOLING_PORT_CONFLICT"
        elif all(code == "LOCAL_MCP_SUPERVISOR_STOPPED" for code in codes):
            state = "stopped"
            aggregate_code = "LOCAL_MCP_SUPERVISOR_STOPPED"
        elif any(code == "LOCAL_MCP_SUPERVISOR_UNKNOWN" for code in codes):
            state = "unknown"
            aggregate_code = "LOCAL_MCP_SUPERVISOR_UNKNOWN"
        elif all(item.get("ready") is True for item in services):
            state = "ready"
            aggregate_code = "LOCAL_MCP_SUPERVISOR_READY"
        elif any(
            code
            in {
                "LOCAL_MCP_SUPERVISOR_STALE",
                "LOCAL_MCP_SUPERVISOR_DEGRADED",
                "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
                "LOCAL_MCP_SUPERVISOR_OWNERSHIP_MISMATCH",
            }
            for code in codes
        ) or any(item.get("ready") is False for item in services):
            state = "stale"
            aggregate_code = "MCP_RUNTIME_CONTRACT_DRIFT"
        else:
            state = "unknown"
            aggregate_code = "LOCAL_MCP_SUPERVISOR_UNKNOWN"
        return state, {
            "ok": state == "ready",
            "code": aggregate_code,
            "repository_root": str(root),
            "services": services,
            "supervisors": supervisors,
        }

    @staticmethod
    def _plugin_source_state(
        current: McpBundle, build: _BundleBuild
    ) -> Literal["ready", "drift", "unknown"]:
        """Проверить исходники плагина и навыка отдельно от среды серверов MCP."""

        if getattr(build, "failure_code", None) is not None:
            return "unknown"
        digests = build.bundle.source_digests
        return (
            "drift"
            if any(
                digests.get(name) != current.source_digests.get(name)
                for name in ("PLUGIN_BUNDLE_SOURCE_SET", "SKILL_BUNDLE_SOURCE_SET")
            )
            else "ready"
        )

    def _runtime_server_statuses(
        self,
        root: Path,
        bundle: McpBundle,
        runtime: Mapping[str, object],
        *,
        authentication_states: Mapping[str, _McpAuthenticationState] | None = None,
        auth_unavailable_servers: Iterable[str] = (),
        runtime_unavailable_servers: Iterable[str] = (),
        source_ready: bool = True,
    ) -> tuple[McpServerStatus, ...]:
        services = {
            item.get("server_name"): item
            for item in runtime.get("services", [])
            if isinstance(item, dict)
        }
        supervisors = runtime.get("supervisors", {})
        auth_unavailable = set(auth_unavailable_servers)
        runtime_unavailable = set(runtime_unavailable_servers)
        resolved_authentication = (
            self._authentication_states(root)
            if authentication_states is None
            else authentication_states
        )
        statuses: list[McpServerStatus] = []
        for name in MCP_SERVER_NAMES:
            server = bundle.servers[name]
            item = services.get(name, {})
            supervisor = supervisors.get(name, {})
            supervisor_code = (
                str(supervisor.get("code"))
                if isinstance(supervisor, Mapping)
                else "LOCAL_MCP_SUPERVISOR_UNKNOWN"
            )
            authentication = resolved_authentication.get(name, "unknown")
            if supervisor_code == "TOOLING_PORT_CONFLICT":
                status: Literal[
                    "ready",
                    "stale",
                    "stopped",
                    "unavailable",
                    "not_configured",
                    "unknown",
                    "conflict",
                ] = "conflict"
                reason_code = supervisor_code
            elif name in auth_unavailable:
                authentication = "unavailable"
                status = "not_configured"
                reason_code = ResultCode.MCP_AUTH_NOT_CONFIGURED.value
            elif name in runtime_unavailable:
                status = "unavailable"
                reason_code = ResultCode.MCP_RUNTIME_UNAVAILABLE.value
            elif item.get("ready") is True:
                if source_ready:
                    status = "ready"
                    reason_code = None
                else:
                    status = "unknown"
                    reason_code = "MCP_SOURCE_NOT_RECONCILED"
            elif supervisor_code == "LOCAL_MCP_SUPERVISOR_STOPPED":
                status = "stopped"
                reason_code = supervisor_code
            elif supervisor_code in {
                "LOCAL_MCP_SUPERVISOR_STALE",
                "LOCAL_MCP_SUPERVISOR_DEGRADED",
                "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
                "LOCAL_MCP_SUPERVISOR_OWNERSHIP_MISMATCH",
            } or item.get("reason_code") == "MCP_RUNTIME_CONTRACT_DRIFT":
                status = "stale"
                reason_code = (
                    item.get("reason_code")
                    if isinstance(item.get("reason_code"), str)
                    else supervisor_code
                )
            else:
                status = "unknown"
                reason_code = str(
                    item.get("reason_code", "MCP_RUNTIME_UNAVAILABLE")
                )
            observed_version = item.get("server_version")
            statuses.append(
                _server_status_from_model(
                    server,
                    status=status,
                    observed_version=(
                        observed_version if isinstance(observed_version, str) else None
                    ),
                    observed_source_revision=_safe_source_revision(
                        item.get("source_revision")
                    ),
                    authentication=authentication,
                    ownership_confirmed=supervisor_code
                    in {
                        "LOCAL_MCP_SUPERVISOR_READY",
                        "LOCAL_MCP_SUPERVISOR_DEGRADED",
                    },
                    reason_code=reason_code,
                )
            )
        return tuple(statuses)

    def _status_details(
        self,
        root: Path,
        *,
        action: Literal["status", "reconcile", "start", "stop", "restart"],
        changed_paths: Iterable[str | Path] = (),
    ):
        current = self._bundle(root)
        try:
            build = self.source.build(root, requested_bump="auto")
        except ToolingError as error:
            if error.code is not ResultCode.MCP_VERSION_BUMP_REQUIRED:
                raise
            build = _BundleBuild(
                bundle=current,
                manifest_text="",
                plugin_manifest_text="",
                compatibility_text="",
                changed_components=(),
                affected_servers=(),
                plugin_changed=False,
                skill_changed=False,
                required_bump_servers=(),
                failure_code=error.code,
            )
        bundle = build.bundle
        runtime_state, runtime = self._runtime_status(root, bundle)
        source_state = "ready"
        try:
            self.source.check(root, build=build)
        except ToolingError:
            source_state = "drift"
        registration_state = self._registration_state(root)
        if registration_state == "invalid":
            source_state = "invalid"
        elif registration_state == "unknown" and source_state == "ready":
            source_state = "unknown"
        plugin_source_state = self._plugin_source_state(current, build)
        if plugin_source_state == "drift":
            plugin_state = "drift"
        elif plugin_source_state == "unknown" or source_state == "unknown":
            plugin_state = "unknown"
        elif source_state == "ready":
            plugin_state = "ready"
        else:
            plugin_state = "drift"
        session_state: Literal["current", "reload_required", "not_observable", "unknown"] = (
            "reload_required"
            if plugin_source_state == "drift"
            else self._session_state(root, runtime, changed_paths=changed_paths)
        )
        statuses = self._runtime_server_statuses(
            root,
            bundle,
            runtime,
            source_ready=source_state == "ready",
        )
        source_reconciled = source_state == "ready" and plugin_source_state == "ready"
        runtime_ready = all(status.status == "ready" for status in statuses)
        return build, runtime_state, runtime, McpStatusDetails(
            action=action,
            source_state=source_state,
            runtime_state=runtime_state,
            source_reconciled=source_reconciled,
            runtime_ready=runtime_ready,
            plugin_state=plugin_state,
            plugin_source_state=plugin_source_state,
            session_state=session_state,
            bundle_revision=bundle.bundle_revision,
            plugin_version=bundle.plugin_version,
            skill_bundle_revision=bundle.skill_bundle_revision,
            servers=statuses,
            component_digests=_digest_models(bundle.source_digests),
            changed_components=build.changed_components,
            affected_servers=build.affected_servers,
            restarted_servers=(),
            reload_required=session_state == "reload_required",
        )

    def status(self, repository_root: str | Path | None = None) -> ToolingResult[McpStatusDetails, McpLifecycleDetails]:
        root = self._root(repository_root)
        _build, runtime_state, _runtime, details = self._status_details(
            root, action="status"
        )
        build_failure_code = getattr(_build, "failure_code", None)
        if details.source_state == "invalid":
            return ToolingResult(
                ok=False,
                code=ResultCode.MCP_PLUGIN_RUNTIME_INCOMPATIBLE,
                state=OperationState.FAILED,
                message="Регистрация источника MCP не соответствует каноническому маршруту.",
                details=details,
            )
        if details.source_state == "unknown":
            code = ResultCode.TOOLING_VERIFICATION_UNKNOWN
        elif build_failure_code is not None:
            code = build_failure_code
        elif details.session_state == "reload_required":
            code = ResultCode.MCP_RELOAD_REQUIRED
        elif details.source_state != "ready":
            code = ResultCode.MCP_SOURCE_BUNDLE_DRIFT
        elif runtime_state == "conflict":
            code = ResultCode.TOOLING_PORT_CONFLICT
        elif runtime_state == "stale":
            code = ResultCode.MCP_RUNTIME_STALE
        elif runtime_state in {"stopped", "unknown"}:
            code = ResultCode.MCP_RUNTIME_UNAVAILABLE
        else:
            code = ResultCode.OK
        if code is ResultCode.MCP_RUNTIME_UNAVAILABLE and details.source_reconciled:
            message = (
                "Исходники MCP синхронизированы, но локальная среда выполнения не готова; "
                "обязательная проверка этой среды не завершена."
            )
        else:
            message = (
                "Состояние исходников MCP, маршрутов и среды выполнения прочитано."
                if code is ResultCode.OK
                else "Исходники MCP, среда выполнения или производные метаданные требуют синхронизации."
            )
        return ToolingResult(
            ok=code is ResultCode.OK,
            code=code,
            state=(
                OperationState.READY if code is ResultCode.OK else OperationState.FAILED
            ),
            message=message,
            details=details,
        )

    def accept(
        self,
        repository_root: str | Path | None = None,
        *,
        allow_dirty: bool = False,
    ) -> ToolingResult[McpAcceptanceDetails, McpLifecycleDetails]:
        """Проверить канонические локальные HTTP-маршруты в новой сессии SDK."""

        root = self._root(repository_root)
        from dev_tools.mcp_acceptance import accept as accept_fresh_mcp_client

        result = asyncio.run(
            accept_fresh_mcp_client(root, allow_dirty=allow_dirty)
        )
        state = str(result.state.value)
        details = McpAcceptanceDetails(
            acceptance_state=state,
            reason_code=result.reason_code,
            initialized=result.initialized,
            protocol_version=result.protocol_version,
            server_name=result.server_name,
            server_version=result.server_version,
            source_revision=result.source_revision,
            tool_count=result.tool_count,
            tool_catalog_sha256=result.tool_catalog_sha256,
            capability_catalog_sha256=result.capability_catalog_sha256,
            contract_revision=result.contract_revision,
            called_tools=result.called_tools,
            diagnostics=result.diagnostics,
        )
        if state == "READY":
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Новая клиентская сессия MCP подтвердила каталог инструментов, контракт и запросы только для чтения.",
                details=details,
            )
        if state == "INCOMPATIBLE":
            code = ResultCode.MCP_PLUGIN_RUNTIME_INCOMPATIBLE
            message = "Новая клиентская сессия MCP обнаружила несовместимость контракта или каталога инструментов."
        elif state == "UNAVAILABLE":
            code = ResultCode.MCP_RUNTIME_UNAVAILABLE
            message = "Новая клиентская сессия MCP недоступна; проверка не подтверждена."
        else:
            code = ResultCode.TOOLING_VERIFICATION_UNKNOWN
            message = "Не удалось определить результат новой клиентской сессии MCP."
        return ToolingResult(
            ok=False,
            code=code,
            state=OperationState.FAILED,
            message=message,
            details=details,
        )

    def versions(self, repository_root: str | Path | None = None) -> ToolingResult[McpVersionDetails, McpLifecycleDetails]:
        root = self._root(repository_root)
        bundle = self._bundle(root)
        self.source.check(root)
        statuses = tuple(_server_status_from_model(bundle.servers[name], status="unknown", reason_code="RUNTIME_NOT_PROBED") for name in MCP_SERVER_NAMES)
        details = McpVersionDetails(
            bundle_revision=bundle.bundle_revision,
            plugin_version=bundle.plugin_version,
            skill_bundle_revision=bundle.skill_bundle_revision,
            servers=statuses,
            component_digests=_digest_models(bundle.source_digests),
        )
        return ToolingResult(ok=True, code=ResultCode.OK, state=OperationState.READY, message="Канонические версии и ревизии MCP подтверждены.", details=details)

    def impact(
        self,
        repository_root: str | Path | None = None,
        *,
        base_commit: str,
    ) -> ToolingResult[McpImpactDetails, McpLifecycleDetails]:
        """Прочитать влияние изменений MCP относительно точной базовой ревизии."""

        root = self._root(repository_root)
        details = _candidate_mcp_impact(root, base_commit=base_commit)
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message=(
                "Для текущих изменений требуется синхронизация исходников MCP."
                if details.reconciliation_required
                else "Текущие изменения не затрагивают наборы исходников MCP."
            ),
            details=details,
        )

    def sync(
        self,
        repository_root: str | Path | None = None,
        *,
        base_commit: str,
    ) -> ToolingResult[McpSyncDetails, McpLifecycleDetails]:
        """Синхронизировать исходники MCP и принадлежащую проекту среду выполнения, затем проверить их новым клиентом."""

        root = self._root(repository_root)
        impact = _candidate_mcp_impact(root, base_commit=base_commit)
        if not impact.reconciliation_required:
            # Изменения только в производных файлах нельзя считать NO_CHANGES,
            # если они нарушили согласованность канонического комплекта исходников.
            self.source.check(root)
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Изменений MCP нет; синхронизация завершена без изменений.",
                details=McpSyncDetails(
                    terminal="NO_CHANGES",
                    base_sha=base_commit,
                    impact=impact,
                ),
            )

        build = self.source.reconcile(
            root,
            requested_bump="auto",
            base_commit=base_commit,
        )
        self.source.check(root, build=build)
        self.source.check_base_to_head(root, base_commit=base_commit, build=build)

        runtime = self.reconcile(root)
        if not runtime.ok and not (
            runtime.code is ResultCode.MCP_RELOAD_REQUIRED
            and runtime.details.runtime_ready
        ):
            return ToolingResult(
                ok=False,
                code=runtime.code,
                state=runtime.state,
                message=runtime.message,
                details=McpSyncDetails(
                    terminal="FAILED",
                    base_sha=base_commit,
                    impact=impact,
                    changed_components=impact.changed_components,
                    affected_servers=impact.affected_servers,
                    generated_artifacts=tuple(
                        path.as_posix() for path in MCP_GENERATED_ARTIFACTS
                    ),
                    runtime=runtime.details,
                ),
            )

        acceptance = self.accept(root, allow_dirty=True)
        if not acceptance.ok:
            return ToolingResult(
                ok=False,
                code=acceptance.code,
                state=acceptance.state,
                message=acceptance.message,
                details=McpSyncDetails(
                    terminal="FAILED",
                    base_sha=base_commit,
                    impact=impact,
                    changed_components=impact.changed_components,
                    affected_servers=impact.affected_servers,
                    generated_artifacts=tuple(
                        path.as_posix() for path in MCP_GENERATED_ARTIFACTS
                    ),
                    runtime=runtime.details,
                    acceptance=acceptance.details,
                ),
            )

        if source_set_digests(root) != dict(build.bundle.source_digests):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
                "Набор исходников MCP изменился во время синхронизации; приёмка не подтверждает текущий вариант.",
            )
        self.source.check(root, build=build)
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message="Исходники MCP и принадлежащая проекту среда выполнения синхронизированы; новый клиент прошёл приёмку.",
            details=McpSyncDetails(
                terminal="SYNCED",
                base_sha=base_commit,
                impact=impact,
                changed_components=impact.changed_components,
                affected_servers=impact.affected_servers,
                generated_artifacts=tuple(
                    path.as_posix() for path in MCP_GENERATED_ARTIFACTS
                ),
                runtime=runtime.details,
                acceptance=acceptance.details,
            ),
        )

    @staticmethod
    def _auth_ready(
        repository_root: str | Path,
        server_names: Iterable[str] = MCP_SERVER_NAMES,
    ) -> bool:
        names = tuple(server_names)
        if not names or any(name not in MCP_SERVER_NAMES for name in names):
            return False
        try:
            for name in names:
                read_local_mcp_token(repository_root, name)
        except LocalHttpAuthUnknownError:
            raise
        except LocalHttpAuthError:
            return False
        return True

    def _authentication_states(
        self, repository_root: str | Path
    ) -> _McpAuthenticationStates:
        states: _McpAuthenticationStates = {}
        for name in MCP_SERVER_NAMES:
            try:
                auth_ready = self._auth_ready(repository_root, (name,))
            except LocalHttpAuthUnknownError:
                states[name] = "unknown"
            except (OSError, ToolingError):
                states[name] = "unknown"
            else:
                states[name] = "configured" if auth_ready else "unavailable"
        return states

    def _auth_unavailable_servers(
        self,
        repository_root: str | Path,
        server_names: Iterable[str] = MCP_SERVER_NAMES,
        *,
        authentication_states: Mapping[str, _McpAuthenticationState] | None = None,
    ) -> tuple[str, ...]:
        names = tuple(server_names)
        if not names or any(name not in MCP_SERVER_NAMES for name in names):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Список MCP-серверов для проверки учётных данных задан неверно.",
            )
        if len(set(names)) != len(names):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Список MCP-серверов для проверки учётных данных содержит повторяющиеся имена.",
            )
        states = (
            self._authentication_states(repository_root)
            if authentication_states is None
            else authentication_states
        )
        return tuple(name for name in names if states.get(name) == "unavailable")

    def _start_owned(
        self,
        root: Path,
        bundle: McpBundle,
        *,
        server_names: Iterable[str] | None = None,
        authentication_states: _McpAuthenticationStates | None = None,
    ) -> _McpStartOutcome:
        requested = tuple(server_names or MCP_SERVER_NAMES)
        if not requested or any(name not in MCP_SERVER_NAMES for name in requested):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Список первичных MCP-серверов для запуска имеет неверный формат.",
            )
        if len(set(requested)) != len(requested):
            raise ToolingError(
                ResultCode.MCP_SOURCE_BUNDLE_INVALID,
                "Список первичных MCP-серверов содержит повторения.",
            )
        resolved_authentication = (
            self._authentication_states(root)
            if authentication_states is None
            else authentication_states
        )
        before_state, before = self._runtime_status(root, bundle)
        if before_state == "conflict":
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Порт локального первичного MCP уже занят чужим процессом.",
            )
        service_items = {
            item.get("server_name"): item
            for item in before.get("services", [])
            if isinstance(item, dict)
        }
        supervisors = before.get("supervisors", {})
        start_names: list[str] = []
        for name in requested:
            item = service_items.get(name, {})
            if item.get("ready") is True:
                continue
            supervisor = supervisors.get(name, {})
            if not isinstance(supervisor, dict) or supervisor.get("code") != (
                "LOCAL_MCP_SUPERVISOR_STOPPED"
            ):
                raise ToolingError(
                    ResultCode.MCP_RUNTIME_STALE,
                    "Не удалось безопасно подтвердить состояние процесса управления локальным MCP.",
                )
            start_names.append(name)
        auth_unavailable = set(
            self._auth_unavailable_servers(
                root,
                requested,
                authentication_states=resolved_authentication,
            )
        )
        runtime_unavailable: set[str] = {
            name for name in requested if resolved_authentication.get(name) == "unknown"
        }
        if not start_names:
            return _McpStartOutcome(
                False,
                before,
                tuple(name for name in requested if name in auth_unavailable),
                tuple(name for name in requested if name in runtime_unavailable),
            )
        launch_names = tuple(
            name
            for name in start_names
            if resolved_authentication.get(name) == "configured"
        )
        if not launch_names:
            return _McpStartOutcome(
                False,
                before,
                tuple(name for name in requested if name in auth_unavailable),
                tuple(name for name in requested if name in runtime_unavailable),
            )
        python = project_python(root)
        if not python.is_file():
            raise ToolingError(
                ResultCode.MCP_ENVIRONMENT_STALE,
                "Интерпретатор Python проекта для локального MCP отсутствует.",
            )
        from module.mcp_shared.local_http_supervisor import (
            LOCAL_HTTP_SOURCE_SET_DIGEST_ENV_VARS,
        )

        try:
            revision = GitClient(root, self.runner).head()
        except ToolingError:
            revision = source_revision()
        running: dict[str, object] = {}
        try:
            for name in launch_names:
                if name in auth_unavailable:
                    continue
                # Повторить проверку непосредственно перед созданием процесса:
                # .env может измениться после первичного разделения по семействам.
                try:
                    auth_ready = self._auth_ready(root, (name,))
                except (LocalHttpAuthUnknownError, OSError, ToolingError):
                    resolved_authentication[name] = "unknown"
                    runtime_unavailable.add(name)
                    continue
                if not auth_ready:
                    resolved_authentication[name] = "unavailable"
                    auth_unavailable.add(name)
                    continue
                resolved_authentication[name] = "configured"
                explicit_env = {
                    LOCAL_HTTP_SOURCE_SET_DIGEST_ENV_VARS[name]: bundle.servers[
                        name
                    ].source_set_digest,
                }
                if _SHA_RE.fullmatch(revision):
                    explicit_env["AZURPILOT_SOURCE_REVISION"] = revision
                try:
                    running[name] = self.runner.start(
                        ProcessSpec(
                            executable=python,
                            argv=(
                                "-u",
                                "-m",
                                "module.mcp_shared.local_http_supervisor",
                                "serve",
                                "--service",
                                name,
                            ),
                            cwd=root,
                            timeout_seconds=30,
                            env=explicit_env,
                        )
                    )
                except ProcessStartError as error:
                    if (
                        error.spawn_state in {"not_spawned", "absent_after_cleanup"}
                        and error.cleanup_state == "absent"
                    ):
                        runtime_unavailable.add(name)
                        continue
                    # Без подтверждённой очистки нельзя терять сведения о возможном
                    # процессе и переходить к запуску следующей группы.
                    raise

            deadline = time.monotonic() + 30
            while running and time.monotonic() < deadline:
                _state, status = self._runtime_status(root, bundle)
                current = {
                    item.get("server_name"): item
                    for item in status.get("services", [])
                    if isinstance(item, dict)
                }
                pending = tuple(
                    name
                    for name, process in running.items()
                    if current.get(name, {}).get("ready") is not True
                    and process.poll() is None  # type: ignore[attr-defined]
                )
                if not pending:
                    break
                time.sleep(0.25)
        finally:
            _state, final_status = self._runtime_status(root, bundle)
            current = {
                item.get("server_name"): item
                for item in final_status.get("services", [])
                if isinstance(item, dict)
            }
            for name, process in running.items():
                if current.get(name, {}).get("ready") is True:
                    continue
                if process.poll() is None:  # type: ignore[attr-defined]
                    ProcessController.terminate(process.identity)  # type: ignore[attr-defined]
                try:
                    auth_ready = self._auth_ready(root, (name,))
                except (LocalHttpAuthUnknownError, OSError, ToolingError):
                    resolved_authentication[name] = "unknown"
                    runtime_unavailable.add(name)
                    continue
                if auth_ready:
                    resolved_authentication[name] = "configured"
                    runtime_unavailable.add(name)
                else:
                    resolved_authentication[name] = "unavailable"
                    auth_unavailable.add(name)
        changed = any(
            current.get(name, {}).get("ready") is True for name in running
        )
        return _McpStartOutcome(
            changed=changed,
            runtime=final_status,
            auth_unavailable_servers=tuple(
                name for name in requested if name in auth_unavailable
            ),
            runtime_unavailable_servers=tuple(
                name for name in requested if name in runtime_unavailable
            ),
        )

    def _stop_owned_supervisor(
        self, root: Path, server_name: str
    ) -> LocalHttpSupervisorStopResult:
        """Остановить процесс управления по типизированному результату проверки состояния и владения."""

        from module.mcp_shared.local_http_supervisor import (
            LocalHttpSupervisorStopOutcome,
        )

        result = self._supervisor(root, server_name).stop_result()
        if result.outcome is LocalHttpSupervisorStopOutcome.PORT_CONFLICT:
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Порт локального первичного MCP уже занят чужим процессом.",
            )
        if not result.ok:
            raise ToolingError(
                ResultCode.MCP_RUNTIME_STALE,
                "Владение локальным MCP не подтверждено: "
                f"{result.outcome.value}; {result.detail}",
            )
        return result

    def start(self, repository_root: str | Path | None = None) -> ToolingResult[McpLifecycleDetails, McpLifecycleDetails]:
        root = self._root(repository_root)
        self.source.check(root)
        bundle = self._bundle(root)
        authentication_states = self._authentication_states(root)
        outcome = self._start_owned(
            root, bundle, authentication_states=authentication_states
        )
        details = self._lifecycle_details(
            root,
            bundle,
            "start",
            outcome.runtime,
            ownership=True,
            authentication_states=authentication_states,
            auth_unavailable_servers=outcome.auth_unavailable_servers,
            runtime_unavailable_servers=outcome.runtime_unavailable_servers,
        )
        if any(service.status == "conflict" for service in details.services):
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Порт локального MCP занят процессом без подтверждённого владельца.",
                details=details,
            )
        if outcome.runtime_unavailable_servers:
            raise ToolingError(
                ResultCode.MCP_RUNTIME_UNAVAILABLE,
                "Одна или несколько локальных служб MCP не перешли в состояние готовности.",
                details=details,
            )
        if outcome.auth_unavailable_servers:
            raise ToolingError(
                ResultCode.MCP_AUTH_NOT_CONFIGURED,
                "Для части локальных MCP отсутствуют учётные данные проекта.",
                details=details,
            )
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message=(
                "Принадлежащая проекту служба управления процессами MCP готова."
                if outcome.changed
                else "Принадлежащая проекту служба управления процессами MCP уже готова; запуск не требовался."
            ),
            details=details,
        )

    def stop(self, repository_root: str | Path | None = None) -> ToolingResult[McpLifecycleDetails, McpLifecycleDetails]:
        root = self._root(repository_root)
        bundle = self._bundle(root)
        authentication_states = self._authentication_states(root)
        for name in MCP_SERVER_NAMES:
            supervisor = self._supervisor(root, name)
            before = supervisor.status()
            if before.get("code") == "LOCAL_MCP_SUPERVISOR_STOPPED":
                if supervisor.port_conflicts():
                    raise ToolingError(
                        ResultCode.TOOLING_PORT_CONFLICT,
                        "Порт локального первичного MCP уже занят чужим процессом.",
                    )
                continue
            self._stop_owned_supervisor(root, name)
        _runtime_state, runtime = self._runtime_status(root, bundle)
        details = self._lifecycle_details(
            root,
            bundle,
            "stop",
            runtime,
            ownership=True,
            authentication_states=authentication_states,
        )
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.STOPPED,
            message="Локальный процесс управления MCP, принадлежащий проекту, остановлен.",
            details=details,
        )

    def restart(self, repository_root: str | Path | None = None) -> ToolingResult[McpLifecycleDetails, McpLifecycleDetails]:
        root = self._root(repository_root)
        self.source.check(root)
        bundle = self._bundle(root)
        authentication_states = self._authentication_states(root)
        auth_unavailable = set(
            self._auth_unavailable_servers(
                root, authentication_states=authentication_states
            )
        )
        runtime_unavailable = {
            name
            for name, state in authentication_states.items()
            if state == "unknown"
        }
        supervisors = {}
        for name in MCP_SERVER_NAMES:
            supervisor = self._supervisor(root, name)
            before = supervisor.status()
            code = before.get("code")
            if code == "LOCAL_MCP_SUPERVISOR_STOPPED":
                if supervisor.port_conflicts():
                    raise ToolingError(
                        ResultCode.TOOLING_PORT_CONFLICT,
                        "Порт локального первичного MCP уже занят чужим процессом.",
                    )
            elif code not in {
                "LOCAL_MCP_SUPERVISOR_READY",
                "LOCAL_MCP_SUPERVISOR_DEGRADED",
                "LOCAL_MCP_SUPERVISOR_STALE",
            }:
                raise ToolingError(
                    ResultCode.MCP_RUNTIME_STALE,
                    "Не удалось безопасно подтвердить состояние процесса управления локальным MCP.",
                )
            supervisors[name] = before

        for name in MCP_SERVER_NAMES:
            if authentication_states.get(name) != "configured":
                continue
            if supervisors[name].get("code") != "LOCAL_MCP_SUPERVISOR_STOPPED":
                self._stop_owned_supervisor(root, name)

        start_names = tuple(
            name
            for name in MCP_SERVER_NAMES
            if authentication_states.get(name) == "configured"
        )
        if start_names:
            outcome = self._start_owned(
                root,
                bundle,
                server_names=start_names,
                authentication_states=authentication_states,
            )
        else:
            _runtime_state, runtime = self._runtime_status(root, bundle)
            outcome = _McpStartOutcome(
                False,
                runtime,
                tuple(name for name in MCP_SERVER_NAMES if name in auth_unavailable),
                tuple(name for name in MCP_SERVER_NAMES if name in runtime_unavailable),
            )
        auth_unavailable.update(outcome.auth_unavailable_servers)
        runtime_unavailable.update(outcome.runtime_unavailable_servers)
        details = self._lifecycle_details(
            root,
            bundle,
            "restart",
            outcome.runtime,
            ownership=True,
            authentication_states=authentication_states,
            auth_unavailable_servers=auth_unavailable,
            runtime_unavailable_servers=runtime_unavailable,
        )
        if any(service.status == "conflict" for service in details.services):
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Порт локального MCP занят процессом без подтверждённого владельца.",
                details=details,
            )
        if runtime_unavailable:
            raise ToolingError(
                ResultCode.MCP_RUNTIME_UNAVAILABLE,
                "Одна или несколько локальных служб MCP не перешли в состояние готовности.",
                details=details,
            )
        if auth_unavailable:
            raise ToolingError(
                ResultCode.MCP_AUTH_NOT_CONFIGURED,
                "Для части локальных MCP отсутствуют учётные данные проекта.",
                details=details,
            )
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message="Локальный процесс управления MCP, принадлежащий проекту, перезапущен и готов.",
            details=details,
        )

    def reconcile(
        self,
        repository_root: str | Path | None = None,
        *,
        source: bool = False,
        bump: str | None = None,
        changed_paths: Iterable[str | Path] = (),
    ) -> ToolingResult[McpReconcileDetails, McpLifecycleDetails]:
        root = self._root(repository_root)
        if source:
            build = self.source.reconcile(root, requested_bump=bump)
            details = McpReconcileDetails(
                mode="source",
                source_state="ready",
                runtime_state="unknown",
                source_reconciled=True,
                runtime_ready=False,
                mutation_performed=True,
                changed_components=build.changed_components,
                affected_servers=build.affected_servers,
                restarted_servers=(),
                session_state="reload_required" if build.plugin_changed else "not_observable",
                reload_required=build.plugin_changed,
            )
            if build.plugin_changed:
                return ToolingResult(
                    ok=False,
                    code=ResultCode.MCP_RELOAD_REQUIRED,
                    state=OperationState.FAILED,
                    message=(
                        "Канонический комплект MCP согласован, но активная сессия плагина или навыка требует перезагрузки; автоматическая перезагрузка не выполнялась."
                    ),
                    details=details,
                )
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Канонический комплект MCP и производные метаданные плагина согласованы.",
                details=details,
            )
        self.source.check(root)
        if not project_python(root).is_file():
            raise ToolingError(
                ResultCode.MCP_ENVIRONMENT_STALE,
                "Интерпретатор Python проекта отсутствует; синхронизация среды MCP остановлена.",
            )
        bundle = self._bundle(root)
        runtime_state, runtime = self._runtime_status(root, bundle)
        if runtime_state == "conflict":
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Порт первичного локального MCP уже занят чужим процессом.",
            )
        session_state: Literal["current", "reload_required", "not_observable", "unknown"] = self._session_state(
            root,
            runtime,
            changed_paths=changed_paths,
        )
        restarted: tuple[str, ...] = ()
        if runtime_state == "unknown":
            raise ToolingError(
                ResultCode.MCP_RUNTIME_STALE,
                "Состояние локальной среды выполнения MCP нельзя безопасно определить.",
            )
        authentication_states = self._authentication_states(root)
        auth_unavailable = set(
            self._auth_unavailable_servers(
                root, authentication_states=authentication_states
            )
        )
        runtime_unavailable: set[str] = {
            name
            for name, state in authentication_states.items()
            if state == "unknown"
        }
        mutated_servers: set[str] = set()
        if runtime_state in {"stale", "stopped"}:
            service_items = {
                item.get("server_name"): item
                for item in runtime.get("services", [])
                if isinstance(item, dict)
            }
            supervisors = runtime.get("supervisors", {})
            repair_names = tuple(
                name
                for name in MCP_SERVER_NAMES
                if service_items.get(name, {}).get("ready") is not True
                and isinstance(supervisors.get(name), dict)
                and supervisors[name].get("code")
                in {
                    "LOCAL_MCP_SUPERVISOR_READY",
                    "LOCAL_MCP_SUPERVISOR_STOPPED",
                    "LOCAL_MCP_SUPERVISOR_STALE",
                }
            )
            if not repair_names:
                raise ToolingError(
                    ResultCode.MCP_RUNTIME_STALE,
                    "Для остановленной или устаревшей локальной среды MCP не подтверждён точный владелец.",
                )
            repairable: list[str] = []
            for name in repair_names:
                if authentication_states.get(name) != "configured":
                    continue
                repairable.append(name)
            repairable_names = tuple(repairable)
            for name in repairable_names:
                if supervisors[name].get("code") == "LOCAL_MCP_SUPERVISOR_STOPPED":
                    continue
                self._stop_owned_supervisor(root, name)
                mutated_servers.add(name)
            if repairable_names:
                start_outcome = self._start_owned(
                    root,
                    bundle,
                    server_names=repairable_names,
                    authentication_states=authentication_states,
                )
                auth_unavailable.update(start_outcome.auth_unavailable_servers)
                runtime_unavailable.update(start_outcome.runtime_unavailable_servers)
            runtime_state, runtime = self._runtime_status(root, bundle)
            service_items = {
                item.get("server_name"): item
                for item in runtime.get("services", [])
                if isinstance(item, dict)
            }
            restarted = tuple(
                name
                for name in repairable_names
                if service_items.get(name, {}).get("ready") is True
            )
            mutated_servers.update(restarted)
            session_state = self._session_state(
                root, runtime, changed_paths=changed_paths
            )
        services = self._runtime_server_statuses(
            root,
            bundle,
            runtime,
            authentication_states=authentication_states,
            auth_unavailable_servers=auth_unavailable,
            runtime_unavailable_servers=runtime_unavailable,
        )
        runtime_ready = runtime_state == "ready" and all(
            service.status == "ready" for service in services
        )
        details = McpReconcileDetails(
            mode="runtime",
            source_state="ready",
            runtime_state=runtime_state,
            source_reconciled=True,
            runtime_ready=runtime_ready,
            mutation_performed=bool(mutated_servers),
            changed_components=(),
            affected_servers=tuple(
                name for name in MCP_SERVER_NAMES if name in mutated_servers
            ),
            restarted_servers=restarted,
            services=services,
            session_state=session_state,
            reload_required=session_state == "reload_required",
        )
        if any(service.status == "conflict" for service in services):
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Порт локального MCP занят процессом без подтверждённого владельца.",
                details=details,
            )
        if auth_unavailable:
            raise ToolingError(
                ResultCode.MCP_AUTH_NOT_CONFIGURED,
                "Для части локальных MCP отсутствуют учётные данные проекта.",
                details=details,
            )
        if runtime_unavailable or not runtime_ready:
            raise ToolingError(
                ResultCode.MCP_RUNTIME_UNAVAILABLE,
                "После запуска или перезапуска не подтверждены готовность и ожидаемое состояние среды выполнения MCP.",
                details=details,
            )
        if session_state == "reload_required":
            return ToolingResult(
                ok=False,
                code=ResultCode.MCP_RELOAD_REQUIRED,
                state=OperationState.FAILED,
                message="Среда выполнения MCP согласована, но активная сессия плагина требует перезагрузки.",
                details=details,
            )
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message=(
                "Среда выполнения MCP уже согласована; изменений не потребовалось."
                if not restarted
                else "Остановленная или устаревшая принадлежащая проекту среда выполнения MCP запущена или перезапущена; готовность подтверждена."
            ),
            details=details,
        )

    @staticmethod
    def _bridge_supervisor(root: Path, *, create_state_directory: bool = True):
        from module.mcp_shared.local_http_supervisor import (
            WINDOWS_MCP_BRIDGE_SERVICE,
            LocalHttpSupervisor,
        )
        from module.mcp_shared.windows_mcp_bridge_contract import BRIDGE_NAME

        return LocalHttpSupervisor(
            root,
            python_executable=project_python(root),
            services=(WINDOWS_MCP_BRIDGE_SERVICE,),
            state_namespace=BRIDGE_NAME,
            create_state_directory=create_state_directory,
        )

    def _bridge_status_result(
        self,
        root: Path,
        *,
        action: Literal["status", "start", "stop", "restart"] = "status",
    ) -> ToolingResult[McpBridgeStatusDetails, McpLifecycleDetails]:
        from module.mcp_shared.local_http_auth import (
            read_local_mcp_bridge_caller_token,
        )
        from module.mcp_shared.local_http_supervisor import LocalHttpSupervisorError
        from module.mcp_shared.windows_mcp_bridge import _bridge_route_status
        from module.mcp_shared.windows_mcp_bridge_contract import (
            BRIDGE_ROUTES,
            bridge_endpoint,
        )

        service: dict[str, object] | None = None
        observed: dict[str, object] = {}
        try:
            supervisor = self._bridge_supervisor(root, create_state_directory=False)
            observed = supervisor.status()
            code = observed.get("code")
            if code == "LOCAL_MCP_SUPERVISOR_STOPPED":
                state = "conflict" if supervisor.port_conflicts() else "stopped"
                reason_code = (
                    "BRIDGE_PORT_CONFLICT" if state == "conflict" else "BRIDGE_STOPPED"
                )
            elif code == "LOCAL_MCP_SUPERVISOR_READY":
                services = observed.get("services")
                service = (
                    next(
                        (
                            item
                            for item in services
                            if isinstance(item, dict)
                            and item.get("server_name") == "windows-mcp-bridge"
                        ),
                        None,
                    )
                    if isinstance(services, list)
                    else None
                )
                if (
                    service is not None
                    and service.get("alive") is True
                    and service.get("ready") is True
                    and service.get("transport") == "windows_mcp_bridge"
                ):
                    state = "ready"
                    reason_code = "BRIDGE_READY"
                else:
                    state = "stale"
                    reason_code = "BRIDGE_READINESS_MISMATCH"
            elif code in {
                "LOCAL_MCP_SUPERVISOR_OWNERSHIP_MISMATCH",
                "TOOLING_PORT_CONFLICT",
            }:
                state = "conflict"
                reason_code = "BRIDGE_OWNERSHIP_CONFLICT"
            elif code == "LOCAL_MCP_SUPERVISOR_UNKNOWN":
                state = "unknown"
                reason_code = "BRIDGE_OWNERSHIP_UNKNOWN"
            elif code in {
                "LOCAL_MCP_SUPERVISOR_STALE",
                "LOCAL_MCP_SUPERVISOR_DEGRADED",
                "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
            }:
                state = "stale"
                reason_code = "BRIDGE_RUNTIME_STALE"
            else:
                state = "unknown"
                reason_code = "BRIDGE_OWNERSHIP_UNKNOWN"
        except LocalHttpSupervisorError, OSError, ValueError, TypeError:
            state = "unknown"
            reason_code = "BRIDGE_OWNERSHIP_UNKNOWN"

        try:
            read_local_mcp_bridge_caller_token(root)
        except LocalHttpAuthUnknownError:
            caller_authentication = "unknown"
        except LocalHttpAuthError, OSError:
            caller_authentication = "unavailable"
        else:
            caller_authentication = "configured"

        upstreams: list[McpBridgeUpstreamStatus] = []
        for family, route in BRIDGE_ROUTES.items():
            try:
                upstream = _bridge_route_status(route, root)
                upstreams.append(
                    McpBridgeUpstreamStatus(
                        route=family,
                        server_name=route.server_name,
                        status=upstream.status,
                        identity=upstream.identity,
                        reason_code=upstream.reason_code,
                    )
                )
            except OSError, TypeError, ValueError:
                upstreams.append(
                    McpBridgeUpstreamStatus(
                        route=family,
                        server_name=route.server_name,
                        status="unknown",
                        reason_code="BRIDGE_UPSTREAM_UNKNOWN",
                    )
                )

        process = McpBridgeProcessStatus(
            supervisor_pid=(
                observed.get("supervisor_pid")
                if isinstance(observed.get("supervisor_pid"), int)
                else None
            ),
            process_pid=(
                service.get("process_pid")
                if isinstance(service, dict)
                and isinstance(service.get("process_pid"), int)
                else None
            ),
            ownership_confirmed=(
                isinstance(service, dict)
                and service.get("alive") is True
                and service.get("ownership_confirmed") is True
            ),
        )
        details = McpBridgeStatusDetails(
            action=action,
            state=state,
            endpoint=bridge_endpoint(),
            routes=tuple(route.path for route in BRIDGE_ROUTES.values()),
            caller_authentication=caller_authentication,
            process=process,
            upstreams=tuple(upstreams),
            reason_code=reason_code,
        )
        if state == "ready" and caller_authentication == "configured":
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Мост Windows MCP принадлежит проекту и готов принимать проверенные запросы.",
                details=details,
            )
        if state == "conflict":
            code = ResultCode.TOOLING_PORT_CONFLICT
            operation_state = OperationState.CONFLICT
            message = "Владение мостом Windows MCP или его локальный порт конфликтует с другим процессом."
        elif state == "stale":
            code = ResultCode.MCP_BRIDGE_RUNTIME_STALE
            operation_state = OperationState.FAILED
            message = (
                "Состояние моста Windows MCP устарело или его готовность не подтверждена."
            )
        elif caller_authentication == "unavailable":
            code = ResultCode.MCP_BRIDGE_AUTH_NOT_CONFIGURED
            operation_state = OperationState.NOT_CONFIGURED
            message = "Учётные данные вызывающего клиента для локального моста Windows MCP не настроены."
        elif caller_authentication == "unknown" or state == "unknown":
            code = ResultCode.TOOLING_VERIFICATION_UNKNOWN
            operation_state = OperationState.UNKNOWN
            message = "Не удалось безопасно подтвердить мост Windows MCP или учётные данные его вызывающего клиента."
        else:
            code = ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE
            operation_state = OperationState.STOPPED
            message = "Мост Windows MCP остановлен."
        return ToolingResult(
            ok=False,
            code=code,
            state=operation_state,
            message=message,
            details=details,
        )

    def bridge(
        self,
        action: str,
        repository_root: str | Path | None = None,
        *,
        caller_token: str | None = None,
    ) -> ToolingResult[BaseModel, McpLifecycleDetails]:
        """Управлять отдельным мостом под контролем проекта, не перезапуская Dev/Game."""

        if not _supports_windows_mcp_bridge():
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNSUPPORTED,
                "Мост Windows MCP доступен только в Windows.",
            )
        root = self._root(repository_root)
        if action == "configure":
            if caller_token is None:
                raise ToolingError(
                    ResultCode.TOOLING_INVALID_INVOCATION,
                    "Для настройки учётных данных вызывающего клиента передайте одно значение через stdin.",
                )
            from module.application.errors import (
                StorageConfigurationError,
                StorageConfigurationUnknownError,
            )
            from module.persistence.local_environment import (
                write_local_mcp_bridge_caller_token,
            )

            try:
                write_local_mcp_bridge_caller_token(root, caller_token)
                details = self._bridge_status_result(root).details
            except StorageConfigurationUnknownError as error:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Состояние защищённого локального окружения проекта неизвестно.",
                ) from error
            except StorageConfigurationError as error:
                raise ToolingError(
                    ResultCode.MCP_BRIDGE_AUTH_NOT_CONFIGURED,
                    "Учётные данные вызывающего клиента не записаны: локальное окружение проекта или формат значения не прошли проверку.",
                ) from error
            return ToolingResult(
                ok=details.caller_authentication == "configured",
                code=(
                    ResultCode.OK
                    if details.caller_authentication == "configured"
                    else ResultCode.TOOLING_VERIFICATION_UNKNOWN
                ),
                state=(
                    OperationState.READY
                    if details.caller_authentication == "configured"
                    else OperationState.UNKNOWN
                ),
                message=(
                    "Отдельные учётные данные вызывающего клиента моста сохранены; значение не выводилось."
                    if details.caller_authentication == "configured"
                    else "Учётные данные записаны, но безопасно прочитать их не удалось."
                ),
                details=details,
            )
        if action == "status":
            return self._bridge_status_result(root)
        if action == "accept":
            return self._bridge_accept(root)
        if action not in {"start", "stop", "restart"}:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Это действие для моста Windows MCP не поддерживается.",
            )

        supervisor = self._bridge_supervisor(root)
        if action == "stop":
            stopped = supervisor.stop_result()
            if not stopped.ok:
                current = self._bridge_status_result(root, action="stop")
                if stopped.outcome.value == "port_conflict":
                    raise ToolingError(
                        ResultCode.TOOLING_PORT_CONFLICT,
                        "Локальный порт моста Windows MCP занят процессом без подтверждённого владельца.",
                        details=current.details,
                    )
                raise ToolingError(
                    ResultCode.MCP_BRIDGE_RUNTIME_STALE,
                    "Владение мостом Windows MCP не подтверждено; процесс не остановлен.",
                    details=current.details,
                )
            current = self._bridge_status_result(root, action="stop")
            details = current.details.model_copy(update={"action": "stop"})
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.STOPPED,
                message="Принадлежащий проекту мост Windows MCP остановлен.",
                details=details,
            )

        if action == "restart":
            stopped = supervisor.stop_result()
            if not stopped.ok:
                current = self._bridge_status_result(root, action="restart")
                if stopped.outcome.value == "port_conflict":
                    raise ToolingError(
                        ResultCode.TOOLING_PORT_CONFLICT,
                        "Локальный порт моста Windows MCP занят процессом без подтверждённого владельца.",
                        details=current.details,
                    )
                raise ToolingError(
                    ResultCode.MCP_BRIDGE_RUNTIME_STALE,
                    "Владение мостом Windows MCP не подтверждено; перезапуск остановлен.",
                    details=current.details,
                )

        before = self._bridge_status_result(root, action=action)
        if before.details.state == "ready" and action == "start":
            return before
        if before.details.state != "stopped":
            return before
        if before.details.caller_authentication != "configured":
            return before
        python = project_python(root)
        if not python.is_file():
            raise ToolingError(
                ResultCode.MCP_ENVIRONMENT_STALE,
                "Интерпретатор Python проекта для моста Windows MCP отсутствует.",
                details=before.details,
            )
        from module.mcp_shared.windows_mcp_bridge_contract import BRIDGE_NAME

        try:
            process = self.runner.start(
                ProcessSpec(
                    executable=python,
                    argv=(
                        "-u",
                        "-m",
                        "module.mcp_shared.local_http_supervisor",
                        "serve",
                        "--service",
                        BRIDGE_NAME,
                        "--root",
                        str(root),
                    ),
                    cwd=root,
                    timeout_seconds=30,
                )
            )
        except ProcessStartError as error:
            if (
                error.spawn_state in {"not_spawned", "absent_after_cleanup"}
                and error.cleanup_state == "absent"
            ):
                return self._bridge_status_result(root, action=action)
            raise ToolingError(
                ResultCode.TOOLING_CLEANUP_UNKNOWN,
                "Не удалось подтвердить результат запуска моста Windows MCP.",
                details=self._bridge_status_result(root, action=action).details,
            ) from error

        deadline = time.monotonic() + 30.0
        last = self._bridge_status_result(root, action=action)
        while time.monotonic() < deadline:
            last = self._bridge_status_result(root, action=action)
            if last.details.state == "ready":
                return last
            if process.poll() is not None:
                break
            if last.details.state in {"conflict", "unknown"}:
                # Владелец может открыть порт раньше, чем запишет маркер готовности.
                # Пока запущенный нами процесс управления работает, ограниченно ждём маркер.
                time.sleep(0.25)
                continue
            time.sleep(0.25)

        # Сначала используем восстановление по маркеру точного владельца. Если
        # проверка готовности не создала маркер, останавливаем только процесс,
        # точную идентичность которого вернул исполнитель.
        try:
            cleanup = supervisor.stop_result()
        except OSError, ValueError, TypeError:
            cleanup = None
        if (cleanup is None or not cleanup.ok) and process.poll() is None:
            ProcessController.terminate(process.identity, timeout_seconds=8.0)
        final = self._bridge_status_result(root, action=action)
        if final.details.state == "conflict":
            return final
        if action == "restart":
            final = final.model_copy(
                update={
                    "ok": False,
                    "code": ResultCode.MCP_BRIDGE_RUNTIME_UNAVAILABLE,
                    "state": OperationState.FAILED,
                    "message": "Мост Windows MCP не подтвердил готовность после перезапуска.",
                }
            )
        return final

    def _bridge_accept(
        self, root: Path
    ) -> ToolingResult[McpBridgeAcceptanceDetails, McpLifecycleDetails]:
        from azurpilot.integrations.mcp_client import (
            HttpTransportPolicy,
            accept_fresh_http,
            accept_fresh_http_modern,
        )
        from dev_tools.mcp_acceptance import build_plan
        from dev_tools.mcp_status import git_source_snapshot
        from module.mcp_shared.local_http_auth import (
            read_local_mcp_bridge_caller_token,
        )
        from module.mcp_shared.windows_mcp_bridge_contract import (
            BRIDGE_EXPECTED_IDENTITY_HEADER,
            BRIDGE_IDENTITY_PROTOCOL,
            BRIDGE_ROUTES,
            BridgeSourceIdentity,
            serialize_identity,
        )

        revision, working_tree = git_source_snapshot(root)
        if working_tree != "clean":
            raise ToolingError(
                ResultCode.MCP_PLUGIN_RUNTIME_INCOMPATIBLE
                if working_tree == "modified"
                else ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Приёмка моста Windows MCP требует чистой рабочей копии с зафиксированным вариантом.",
            )

        status = self._bridge_status_result(root)
        if not status.ok:
            raise ToolingError(status.code, status.message, details=status.details)
        try:
            caller_token = read_local_mcp_bridge_caller_token(root)
        except LocalHttpAuthUnknownError as error:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Состояние учётных данных клиента локального моста Windows MCP неизвестно.",
                details=status.details,
            ) from error
        except (LocalHttpAuthError, OSError) as error:
            raise ToolingError(
                ResultCode.MCP_BRIDGE_AUTH_NOT_CONFIGURED,
                "Учётные данные клиента локального моста Windows MCP не настроены.",
                details=status.details,
            ) from error

        bundle = self._bundle(root)
        legacy_results: list[McpAcceptanceDetails] = []
        modern_results: list[McpAcceptanceDetails] = []
        for family, route in BRIDGE_ROUTES.items():
            server_name = route.server_name
            server = bundle.servers[server_name]
            expected = BridgeSourceIdentity(
                identity_protocol=BRIDGE_IDENTITY_PROTOCOL,
                server_name=server_name,
                server_version=str(server.version),
                source_revision=revision,
                source_set_digest=server.source_set_digest,
                contract_revision=server.contract_revision,
                tool_catalog_sha256=server.tool_catalog_sha256,
                capability_catalog_sha256=server.capability_catalog_sha256,
            )
            for accept_client, target_results in (
                (accept_fresh_http, legacy_results),
                (accept_fresh_http_modern, modern_results),
            ):
                try:
                    result = asyncio.run(
                        accept_client(
                            endpoint=route.bridge_url,
                            headers={
                                "Authorization": f"Bearer {caller_token}",
                                BRIDGE_EXPECTED_IDENTITY_HEADER: serialize_identity(
                                    expected
                                ),
                            },
                            plan=build_plan(revision, server_name),
                            timeout_seconds=20.0,
                            transport_policy=HttpTransportPolicy.ISOLATED_LOOPBACK,
                        )
                    )
                except RuntimeError as error:
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "Не удалось создать независимую сессию приёмки моста.",
                        details=status.details,
                    ) from error
                target_results.append(
                    McpAcceptanceDetails(
                        acceptance_state=result.state.value,
                        reason_code=result.reason_code,
                        initialized=result.initialized,
                        protocol_version=result.protocol_version,
                        server_name=result.server_name,
                        server_version=result.server_version,
                        source_revision=result.source_revision,
                        tool_count=result.tool_count,
                        tool_catalog_sha256=result.tool_catalog_sha256,
                        capability_catalog_sha256=result.capability_catalog_sha256,
                        contract_revision=result.contract_revision,
                        called_tools=result.called_tools,
                        diagnostics=result.diagnostics,
                    )
                )

        all_results = (*legacy_results, *modern_results)
        all_ready = all(item.acceptance_state == "READY" for item in all_results)
        first_failure = next(
            (item for item in all_results if item.acceptance_state != "READY"), None
        )
        reason_code = (
            "WINDOWS_MCP_BRIDGE_ACCEPTANCE_READY"
            if all_ready
            else first_failure.reason_code
            if first_failure is not None
            else "WINDOWS_MCP_BRIDGE_ACCEPTANCE_UNKNOWN"
        )
        details = McpBridgeAcceptanceDetails(
            acceptance_state=(
                "READY"
                if all_ready
                else "INCOMPATIBLE"
                if any(item.acceptance_state == "INCOMPATIBLE" for item in all_results)
                else "UNKNOWN"
                if any(item.acceptance_state == "UNKNOWN" for item in all_results)
                else "UNAVAILABLE"
            ),
            reason_code=reason_code,
            routes=tuple(legacy_results),
            modern_routes=tuple(modern_results),
        )
        if all_ready:
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Сеансы в режиме совместимости и современном режиме MCP 2026-07-28 подтвердили оба маршрута моста Windows MCP вызовами только для чтения.",
                details=details,
            )
        code = (
            ResultCode.MCP_PLUGIN_RUNTIME_INCOMPATIBLE
            if details.acceptance_state == "INCOMPATIBLE"
            else ResultCode.MCP_RUNTIME_UNAVAILABLE
            if details.acceptance_state == "UNAVAILABLE"
            else ResultCode.TOOLING_VERIFICATION_UNKNOWN
        )
        return ToolingResult(
            ok=False,
            code=code,
            state=OperationState.FAILED,
            message="Одна или несколько новых клиентских сессий для приёмки моста не прошли проверку.",
            details=details,
        )

    def _lifecycle_details(
        self,
        root: Path,
        bundle: McpBundle,
        action: Literal["start", "stop", "restart"],
        runtime: Mapping[str, object],
        *,
        ownership: bool,
        authentication_states: Mapping[str, _McpAuthenticationState] | None = None,
        auth_unavailable_servers: Iterable[str] = (),
        runtime_unavailable_servers: Iterable[str] = (),
    ) -> McpLifecycleDetails:
        statuses = self._runtime_server_statuses(
            root,
            bundle,
            runtime,
            authentication_states=authentication_states,
            auth_unavailable_servers=auth_unavailable_servers,
            runtime_unavailable_servers=runtime_unavailable_servers,
        )
        return McpLifecycleDetails(
            action=action,
            supervisor_code=str(runtime.get("code", "LOCAL_MCP_SUPERVISOR_UNKNOWN")),
            services=statuses,
            ownership_confirmed=ownership,
            readiness_confirmed=all(status.status == "ready" for status in statuses),
        )


__all__ = [
    "CODEX_CONFIG_PATH",
    "MCP_SERVER_NAMES",
    "PLUGIN_COMPATIBILITY_PATH",
    "PLUGIN_MANIFEST_PATH",
    "SERVER_SOURCE_SETS",
    "SOURCE_SET_NAMES",
    "SOURCE_SET_PATHS",
    "McpBaseCompatibility",
    "McpService",
    "McpSourceDriftDetails",
    "McpSourceReconciler",
    "SourceChangeClassification",
    "classify_source_changes",
    "source_set_digest",
    "source_set_digests",
]
