"""Каноническое SemVer-описание MCP-серверов AzurPilot.

В этой модуле нет сетевых, Git или runtime-вызовов. Версии серверов читаются
из одного repository-owned manifest, а ``source_revision`` принимается только
из явно переданного bounded окружения процесса.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path
from typing import Final

MCP_VERSION_MANIFEST = Path("config/mcp-versions.toml")
MCP_VERSION_MANIFEST_SCHEMA_VERSION: Final = 1
SOURCE_REVISION_ENV = "AZURPILOT_SOURCE_REVISION"
UNKNOWN_SOURCE_REVISION = "unknown"
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


def load_server_versions(root: Path | str | None = None) -> dict[str, str]:
    """Загрузить и проверить все canonical server versions."""

    path = _root(root) / MCP_VERSION_MANIFEST
    try:
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VersioningError("Не удалось прочитать MCP version manifest") from exc
    if payload.get("schema_version") != MCP_VERSION_MANIFEST_SCHEMA_VERSION:
        raise VersioningError("Неподдерживаемая схема MCP version manifest")
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, dict) or not raw_servers:
        raise VersioningError("MCP version manifest не содержит servers")
    versions: dict[str, str] = {}
    for name, raw in raw_servers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise VersioningError("Некорректное имя MCP server")
        if not isinstance(raw, dict) or set(raw) != {"version"}:
            raise VersioningError("Некорректная запись MCP server version")
        version = raw.get("version")
        parsed = SemVer.parse(version) if isinstance(version, str) else None
        if parsed is None:
            raise VersioningError("Некорректная версия MCP server")
        versions[name] = str(parsed)
    return versions


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
    "MCP_VERSION_MANIFEST",
    "MCP_VERSION_MANIFEST_SCHEMA_VERSION",
    "SOURCE_REVISION_ENV",
    "UNKNOWN_SOURCE_REVISION",
    "SemVer",
    "VersioningError",
    "load_server_versions",
    "parse_version",
    "parse_version_range",
    "server_version",
    "source_revision",
    "version_satisfies",
)
