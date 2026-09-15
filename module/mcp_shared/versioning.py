"""Каноническое SemVer-описание MCP-серверов AzurPilot.

В этой модуле нет сетевых, Git или runtime-вызовов. Версии серверов читаются
из одного repository-owned manifest, а ``source_revision`` принимается только
из явно переданного bounded окружения процесса.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path
from types import MappingProxyType
from typing import Final

MCP_VERSION_MANIFEST = Path("config/mcp-versions.toml")
MCP_VERSION_MANIFEST_SCHEMA_VERSION: Final = 2
SOURCE_REVISION_ENV = "AZURPILOT_SOURCE_REVISION"
UNKNOWN_SOURCE_REVISION = "unknown"
MCP_SOURCE_SET_NAMES: Final = (
    "DEV_MCP_SOURCE_SET",
    "GAME_MCP_SOURCE_SET",
    "SHARED_MCP_SOURCE_SET",
    "PLUGIN_BUNDLE_SOURCE_SET",
    "SKILL_BUNDLE_SOURCE_SET",
)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SEMVER_IDENTIFIER_RE = re.compile(r"^[0-9A-Za-z-]+$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_RANGE_PART_RE = re.compile(r"^(<=|>=|<|>|=)?\s*(.+)$")


class VersioningError(ValueError):
    """Ошибка формата или структуры canonical version manifest."""


@dataclass(frozen=True, slots=True)
class McpCompatibility:
    """Политика совместимости одного first-party MCP backend."""

    required_feature_flags: Mapping[str, bool]
    required_capability_families: tuple[str, ...]
    result_vocabulary: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class McpServerVersion:
    """Полная transport-neutral identity одного MCP backend."""

    name: str
    version: str
    api_version: int
    contract_schema_version: int
    tool_catalog_sha256: str
    tool_names: tuple[str, ...]
    tool_descriptor_hashes: Mapping[str, str]
    capability_catalog_sha256: str
    contract_revision: str
    source_set_digest: str
    authorization_scopes: tuple[str, ...]
    feature_flags: Mapping[str, bool]
    capability_families: tuple[str, ...]
    result_vocabulary: tuple[str, ...]
    smoke_spec_schema_version: int | None = None
    smoke_result_schema_version: int | None = None


@dataclass(frozen=True, slots=True)
class McpBundle:
    """Строгая модель единственного canonical MCP bundle manifest."""

    schema_version: int
    bundle_revision: str
    plugin_version: str
    skill_bundle_revision: str
    servers: Mapping[str, McpServerVersion]
    compatibility: Mapping[str, McpCompatibility]
    source_digests: Mapping[str, str]

    @property
    def required_mcp_servers(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                name: f">={server.version.split('+', 1)[0]},"
                f"<{_next_major(server.version)}.0.0"
                for name, server in self.servers.items()
            }
        )


@total_ordering
@dataclass(frozen=True, slots=True, eq=False)
class SemVer:
    """Строгое представление SemVer 2.0.0 без неявных coercions."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for component_name in ("major", "minor", "patch"):
            component = getattr(self, component_name)
            if type(component) is not int or component < 0:
                raise VersioningError(
                    f"Компонент SemVer {component_name} должен быть неотрицательным int"
                )
        for section_name in ("prerelease", "build"):
            identifiers = getattr(self, section_name)
            if type(identifiers) is not tuple:
                raise VersioningError(
                    f"Секция SemVer {section_name} должна быть tuple"
                )
            for identifier in identifiers:
                if (
                    type(identifier) is not str
                    or _SEMVER_IDENTIFIER_RE.fullmatch(identifier) is None
                ):
                    raise VersioningError("Некорректный SemVer identifier")
                if (
                    section_name == "prerelease"
                    and identifier.isdigit()
                    and len(identifier) > 1
                    and identifier.startswith("0")
                ):
                    raise VersioningError(
                        "Числовой prerelease identifier содержит ведущий ноль"
                    )

    @classmethod
    def parse(cls, value: str) -> SemVer:
        if not isinstance(value, str):
            raise VersioningError("SemVer должен быть строкой")
        match = _SEMVER_RE.fullmatch(value)
        if match is None:
            raise VersioningError("Некорректный SemVer")
        prerelease = _identifiers(match.group(4))
        build = _identifiers(match.group(5))
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
            prerelease=prerelease,
            build=build,
        )

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        prerelease = f"-{'.'.join(self.prerelease)}" if self.prerelease else ""
        build = f"+{'.'.join(self.build)}" if self.build else ""
        return f"{core}{prerelease}{build}"

    def _precedence_key(self) -> tuple[object, ...]:
        if not self.prerelease:
            prerelease_key: tuple[object, ...] = (1,)
        else:
            identifiers: list[tuple[int, object]] = []
            for identifier in self.prerelease:
                if identifier.isdigit():
                    identifiers.append((0, int(identifier)))
                else:
                    identifiers.append((1, identifier))
            prerelease_key = (0, tuple(identifiers))
        return (self.major, self.minor, self.patch, prerelease_key)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._precedence_key() == other._precedence_key()

    def __hash__(self) -> int:
        return hash(self._precedence_key())

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        left = self._precedence_key()
        right = other._precedence_key()
        if left[:3] != right[:3]:
            return left[:3] < right[:3]
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not bool(other.prerelease)
        for left_identifier, right_identifier in zip(
            self.prerelease, other.prerelease, strict=False
        ):
            if left_identifier == right_identifier:
                continue
            left_numeric = left_identifier.isdigit()
            right_numeric = right_identifier.isdigit()
            if left_numeric and right_numeric:
                return int(left_identifier) < int(right_identifier)
            if left_numeric != right_numeric:
                return left_numeric
            return left_identifier < right_identifier
        return len(self.prerelease) < len(other.prerelease)


def _identifiers(value: str | None) -> tuple[str, ...]:
    return tuple(value.split(".")) if value else ()


def parse_version(value: str) -> SemVer:
    """Разобрать SemVer и вернуть типизированное значение."""

    return SemVer.parse(value)


def parse_version_range(value: str) -> tuple[tuple[str, SemVer], ...]:
    """Разобрать bounded conjunction диапазона вида ``>=3.0.0,<4.0.0``."""

    if not isinstance(value, str) or not value.strip():
        raise VersioningError("Диапазон версий пуст")
    constraints: list[tuple[str, SemVer]] = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        match = _RANGE_PART_RE.fullmatch(part)
        if match is None:
            raise VersioningError("Некорректная часть диапазона версий")
        operator = match.group(1) or "="
        constraints.append((operator, SemVer.parse(match.group(2).strip())))
    if len(constraints) == 1 and constraints[0][0] == "=":
        return tuple(constraints)
    if any(operator == "=" for operator, _version in constraints):
        raise VersioningError("Exact constraint нельзя смешивать с диапазоном")
    if not any(operator in {">", ">="} for operator, _version in constraints):
        raise VersioningError("Диапазон версий не содержит lower bound")
    if not any(operator in {"<", "<="} for operator, _version in constraints):
        raise VersioningError("Диапазон версий не содержит upper bound")
    return tuple(constraints)


def version_satisfies(version: str | SemVer, version_range: str) -> bool:
    """Проверить версию против bounded SemVer range."""

    actual = version if isinstance(version, SemVer) else SemVer.parse(version)
    constraints = parse_version_range(version_range)
    if actual.prerelease and not any(
        expected.prerelease
        and (expected.major, expected.minor, expected.patch)
        == (actual.major, actual.minor, actual.patch)
        for _operator, expected in constraints
    ):
        return False
    for operator, expected in constraints:
        if operator == "=" and actual != expected:
            return False
        if operator == ">" and not actual > expected:
            return False
        if operator == ">=" and not actual >= expected:
            return False
        if operator == "<" and not actual < expected:
            return False
        if operator == "<=" and not actual <= expected:
            return False
    return True


def _root(root: Path | str | None) -> Path:
    return (Path(root) if root is not None else _PROJECT_ROOT).resolve()


_SERVER_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_SERVERS = ("azurpilot-dev", "azurpilot-game")


def _next_major(version: str) -> int:
    return SemVer.parse(version).major + 1


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise VersioningError(f"{label} должен быть mapping")
    return value


def _string(value: object, label: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VersioningError(f"{label} должен быть непустой строкой")
    result = value.strip()
    if pattern is not None and pattern.fullmatch(result) is None:
        raise VersioningError(f"{label} имеет неверный формат")
    return result


def _bounded_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**31:
        raise VersioningError(f"{label} должен быть ограниченным int")
    return value


def _string_tuple(value: object, label: str, *, max_items: int = 512) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= max_items:
        raise VersioningError(f"{label} должен быть непустым ограниченным списком")
    result = tuple(_string(item, f"{label}[]") for item in value)
    if len(set(result)) != len(result):
        raise VersioningError(f"{label} содержит повторения")
    return result


def _flag_mapping(value: object, label: str) -> Mapping[str, bool]:
    raw = _mapping(value, label)
    if not 1 <= len(raw) <= 128:
        raise VersioningError(f"{label} имеет неверный размер")
    flags: dict[str, bool] = {}
    for key, item in raw.items():
        name = _string(key, f"{label} key", pattern=re.compile(r"[a-z][a-z0-9_.-]{0,127}"))
        if not isinstance(item, bool):
            raise VersioningError(f"{label}.{name} должен быть bool")
        flags[name] = item
    return MappingProxyType(flags)


def _hash_mapping(value: object, label: str) -> Mapping[str, str]:
    raw = _mapping(value, label)
    if not 1 <= len(raw) <= 512:
        raise VersioningError(f"{label} имеет неверный размер")
    hashes: dict[str, str] = {}
    for key, item in raw.items():
        name = _string(key, f"{label} key", pattern=re.compile(r"[a-z][a-z0-9_.-]{0,127}"))
        hashes[name] = _string(item, f"{label}.{name}", pattern=_HASH_RE)
    return MappingProxyType(hashes)


def _parse_server(name: str, raw: object) -> McpServerVersion:
    values = _mapping(raw, f"servers.{name}")
    common = {
        "version",
        "api_version",
        "contract_schema_version",
        "tool_catalog_sha256",
        "tool_names",
        "tool_descriptor_hashes",
        "capability_catalog_sha256",
        "contract_revision",
        "source_set_digest",
        "authorization_scopes",
        "feature_flags",
        "capability_families",
        "result_vocabulary",
    }
    smoke = {"smoke_spec_schema_version", "smoke_result_schema_version"}
    expected = common | (smoke if name == "azurpilot-dev" else set())
    if set(values) != expected:
        raise VersioningError(f"Некорректная запись servers.{name}")
    version = _string(values.get("version"), f"servers.{name}.version")
    parsed = SemVer.parse(version)
    tool_names = _string_tuple(values.get("tool_names"), f"servers.{name}.tool_names")
    tool_descriptor_hashes = _hash_mapping(
        values.get("tool_descriptor_hashes"),
        f"servers.{name}.tool_descriptor_hashes",
    )
    if set(tool_descriptor_hashes) != set(tool_names):
        raise VersioningError(
            f"servers.{name}.tool_descriptor_hashes не совпадает с tool_names"
        )
    scopes = _string_tuple(
        values.get("authorization_scopes"),
        f"servers.{name}.authorization_scopes",
        max_items=16,
    )
    families = _string_tuple(
        values.get("capability_families"),
        f"servers.{name}.capability_families",
        max_items=128,
    )
    vocabulary = _string_tuple(
        values.get("result_vocabulary"),
        f"servers.{name}.result_vocabulary",
        max_items=128,
    )
    return McpServerVersion(
        name=name,
        version=str(parsed),
        api_version=_bounded_int(values.get("api_version"), f"servers.{name}.api_version"),
        contract_schema_version=_bounded_int(
            values.get("contract_schema_version"),
            f"servers.{name}.contract_schema_version",
        ),
        tool_catalog_sha256=_string(
            values.get("tool_catalog_sha256"),
            f"servers.{name}.tool_catalog_sha256",
            pattern=_HASH_RE,
        ),
        tool_names=tool_names,
        tool_descriptor_hashes=tool_descriptor_hashes,
        capability_catalog_sha256=_string(
            values.get("capability_catalog_sha256"),
            f"servers.{name}.capability_catalog_sha256",
            pattern=_HASH_RE,
        ),
        contract_revision=_string(
            values.get("contract_revision"),
            f"servers.{name}.contract_revision",
            pattern=_HASH_RE,
        ),
        source_set_digest=_string(
            values.get("source_set_digest"),
            f"servers.{name}.source_set_digest",
            pattern=_HASH_RE,
        ),
        authorization_scopes=scopes,
        feature_flags=_flag_mapping(values.get("feature_flags"), f"servers.{name}.feature_flags"),
        capability_families=families,
        result_vocabulary=vocabulary,
        smoke_spec_schema_version=(
            _bounded_int(
                values.get("smoke_spec_schema_version"),
                f"servers.{name}.smoke_spec_schema_version",
            )
            if name == "azurpilot-dev"
            else None
        ),
        smoke_result_schema_version=(
            _bounded_int(
                values.get("smoke_result_schema_version"),
                f"servers.{name}.smoke_result_schema_version",
            )
            if name == "azurpilot-dev"
            else None
        ),
    )


def _parse_mcp_bundle_payload(payload: object) -> McpBundle:
    """Преобразовать уже разобранный TOML в строгую модель bundle."""

    if not isinstance(payload, dict):
        raise VersioningError("MCP bundle manifest должен иметь TOML table в корне")
    if set(payload) != {
        "schema_version",
        "bundle_revision",
        "plugin_version",
        "skill_bundle_revision",
        "servers",
        "compatibility",
        "source_digests",
    }:
        raise VersioningError("MCP bundle manifest содержит неизвестные top-level поля")
    if payload.get("schema_version") != MCP_VERSION_MANIFEST_SCHEMA_VERSION:
        raise VersioningError("Неподдерживаемая схема MCP bundle manifest")
    bundle_revision = _string(payload.get("bundle_revision"), "bundle_revision", pattern=_HASH_RE)
    skill_bundle_revision = _string(
        payload.get("skill_bundle_revision"),
        "skill_bundle_revision",
        pattern=_HASH_RE,
    )
    plugin_version = _string(payload.get("plugin_version"), "plugin_version")
    SemVer.parse(plugin_version)

    raw_servers = _mapping(payload.get("servers"), "servers")
    if set(raw_servers) != set(_EXPECTED_SERVERS) or len(raw_servers) != len(_EXPECTED_SERVERS):
        raise VersioningError("MCP bundle должен содержать только Dev и Game servers")
    servers = {
        name: _parse_server(name, raw_servers[name]) for name in _EXPECTED_SERVERS
    }

    raw_compatibility = _mapping(payload.get("compatibility"), "compatibility")
    if set(raw_compatibility) != {"required_mcp_servers", *_EXPECTED_SERVERS}:
        raise VersioningError("Некорректная compatibility policy MCP bundle")
    raw_ranges = _mapping(
        raw_compatibility.get("required_mcp_servers"),
        "compatibility.required_mcp_servers",
    )
    if set(raw_ranges) != set(_EXPECTED_SERVERS) or len(raw_ranges) != len(_EXPECTED_SERVERS):
        raise VersioningError("compatibility ranges не совпадают с server catalog")
    required_ranges: dict[str, str] = {}
    for name in _EXPECTED_SERVERS:
        value = _string(raw_ranges[name], f"required_mcp_servers.{name}")
        parse_version_range(value)
        required_ranges[name] = value
    compatibility: dict[str, McpCompatibility] = {}
    for name in _EXPECTED_SERVERS:
        values = _mapping(raw_compatibility.get(name), f"compatibility.{name}")
        if set(values) != {
            "required_feature_flags",
            "required_capability_families",
            "result_vocabulary",
        }:
            raise VersioningError(f"Некорректная compatibility.{name}")
        compatibility[name] = McpCompatibility(
            required_feature_flags=_flag_mapping(
                values.get("required_feature_flags"),
                f"compatibility.{name}.required_feature_flags",
            ),
            required_capability_families=_string_tuple(
                values.get("required_capability_families"),
                f"compatibility.{name}.required_capability_families",
                max_items=128,
            ),
            result_vocabulary=_string_tuple(
                values.get("result_vocabulary"),
                f"compatibility.{name}.result_vocabulary",
                max_items=128,
            ),
        )

    raw_sources = _mapping(payload.get("source_digests"), "source_digests")
    if set(raw_sources) != set(MCP_SOURCE_SET_NAMES):
        raise VersioningError("source_digests не совпадает с canonical source sets")
    source_digests: dict[str, str] = {}
    for key, value in raw_sources.items():
        name = _string(key, "source_digests key", pattern=re.compile(r"[A-Z][A-Z0-9_]{0,63}"))
        source_digests[name] = _string(value, f"source_digests.{name}", pattern=_HASH_RE)
    bundle = McpBundle(
        schema_version=MCP_VERSION_MANIFEST_SCHEMA_VERSION,
        bundle_revision=bundle_revision,
        plugin_version=plugin_version,
        skill_bundle_revision=skill_bundle_revision,
        servers=MappingProxyType(servers),
        compatibility=MappingProxyType(compatibility),
        source_digests=MappingProxyType(source_digests),
    )
    if required_ranges != bundle.required_mcp_servers:
        raise VersioningError(
            "compatibility.required_mcp_servers не совпадает с версиями servers"
        )
    return bundle


def load_mcp_bundle_text(content: str) -> McpBundle:
    """Загрузить строгий MCP bundle из UTF-8 TOML-текста."""

    if not isinstance(content, str):
        raise TypeError("content должен быть строкой")
    try:
        payload = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise VersioningError("Не удалось разобрать MCP version manifest") from exc
    return _parse_mcp_bundle_payload(payload)


def load_mcp_bundle_bytes(content: bytes) -> McpBundle:
    """Загрузить строгий MCP bundle из bounded Git blob."""

    if not isinstance(content, bytes):
        raise TypeError("content должен быть bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VersioningError("MCP version manifest не является UTF-8") from exc
    return load_mcp_bundle_text(text)


def load_mcp_bundle(root: Path | str | None = None) -> McpBundle:
    """Загрузить единственный строгий canonical MCP bundle."""

    path = _root(root) / MCP_VERSION_MANIFEST
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise VersioningError("Не удалось прочитать MCP version manifest") from exc
    return load_mcp_bundle_bytes(content)


def load_server_versions(root: Path | str | None = None) -> dict[str, str]:
    """Загрузить canonical server versions через строгую bundle-модель."""

    bundle = load_mcp_bundle(root)
    return {name: server.version for name, server in bundle.servers.items()}


def server_version(server_name: str, root: Path | str | None = None) -> str:
    """Вернуть canonical SemVer конкретного MCP server."""

    try:
        return load_server_versions(root)[server_name]
    except KeyError as exc:
        raise VersioningError("MCP server отсутствует в canonical manifest") from exc


def source_revision(environ: dict[str, str] | None = None) -> str:
    """Вернуть bounded source revision без чтения Git и раскрытия окружения."""

    values = os.environ if environ is None else environ
    value = str(values.get(SOURCE_REVISION_ENV, "")).strip()
    return value.lower() if _SHA_RE.fullmatch(value) else UNKNOWN_SOURCE_REVISION


__all__ = (
    "MCP_SOURCE_SET_NAMES",
    "MCP_VERSION_MANIFEST",
    "MCP_VERSION_MANIFEST_SCHEMA_VERSION",
    "SOURCE_REVISION_ENV",
    "UNKNOWN_SOURCE_REVISION",
    "McpBundle",
    "McpCompatibility",
    "McpServerVersion",
    "SemVer",
    "VersioningError",
    "load_mcp_bundle",
    "load_mcp_bundle_bytes",
    "load_mcp_bundle_text",
    "load_server_versions",
    "parse_version",
    "parse_version_range",
    "server_version",
    "source_revision",
    "version_satisfies",
)
