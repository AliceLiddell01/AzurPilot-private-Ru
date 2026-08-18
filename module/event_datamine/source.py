"""Безопасное чтение расшифрованных ShareCfg без исполнения Lua."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar

from dev_tools.slpp import slpp


class ShareCfgError(ValueError):
    """Структурированная ошибка доверительной границы источника."""

    def __init__(self, code: str, message: str, *, table: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.table = table


@dataclass(frozen=True)
class SourceSnapshot:
    root: Path
    server: str
    repository: str
    revision: str
    provider: str = "AzurLaneLuaScripts"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        object.__setattr__(self, "server", self.server.upper())
        if not str(self.repository).strip():
            raise ShareCfgError("repository_missing", "Требуется identity source repository")
        if self.server.upper() not in {"CN", "EN", "JP", "TW", "KR"}:
            raise ShareCfgError("invalid_server", f"Неподдерживаемый server {self.server}")
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ShareCfgError(
                "invalid_revision", "Требуется полный закреплённый Git SHA"
            )


class ShareCfgLoader:
    """Экземплярный loader; корень и server не протекают через global state."""

    SERVER_FOLDERS: ClassVar[dict[str, tuple[str, ...]]] = {
        "CN": ("CN", "zh-CN", "zh-cn"),
        "EN": ("EN", "en-US", "en-us"),
        "JP": ("JP", "ja-JP", "ja-jp"),
        "TW": ("TW", "zh-TW", "zh-tw"),
        "KR": ("KR", "ko-KR", "ko-kr"),
    }

    def __init__(self, snapshot: SourceSnapshot) -> None:
        self.snapshot = snapshot
        self.server_root = self._server_root()
        self._fixture_manifest = self._load_fixture_manifest()
        self._cache: dict[str, dict[int, Any]] = {}

    def _server_root(self) -> Path:
        for name in self.SERVER_FOLDERS.get(
            self.snapshot.server, (self.snapshot.server,)
        ):
            candidate = (self.snapshot.root / name).resolve()
            if candidate.is_dir():
                return candidate
        raise ShareCfgError(
            "server_missing",
            f"Каталог сервера {self.snapshot.server} отсутствует в snapshot",
        )

    def _load_fixture_manifest(self) -> dict[str, Any] | None:
        """Разрешить JSON-fixture только по явному manifest с source evidence."""

        path = (self.snapshot.root / "manifest.json").resolve()
        if path.parent != self.snapshot.root or not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShareCfgError(
                "fixture_manifest_invalid",
                f"Не удалось прочитать manifest производного ShareCfg fixture: {exc}",
            ) from exc
        if not isinstance(raw, dict) or raw.get("kind") != "derived_sharecfg_subset":
            return None
        if raw.get("fixture_schema_version") != 1:
            raise ShareCfgError(
                "fixture_manifest_invalid",
                "Неподдерживаемая версия manifest производного ShareCfg fixture",
            )
        source = raw.get("source")
        records = raw.get("records")
        hashes = raw.get("sha256")
        if not isinstance(source, dict) or not isinstance(records, dict) or not isinstance(hashes, dict):
            raise ShareCfgError(
                "fixture_manifest_invalid",
                "Manifest производного ShareCfg fixture не содержит source, records или sha256",
            )
        expected_source = {
            "provider": self.snapshot.provider,
            "repository": self.snapshot.repository,
            "revision": self.snapshot.revision,
            "server": self.snapshot.server,
        }
        actual_source = {
            "provider": str(source.get("provider") or ""),
            "repository": str(source.get("repository") or ""),
            "revision": str(source.get("revision") or ""),
            "server": str(source.get("server") or "").upper(),
        }
        if actual_source != expected_source:
            raise ShareCfgError(
                "fixture_source_mismatch",
                "Manifest производного ShareCfg fixture не соответствует pinned source identity",
            )
        return raw

    @staticmethod
    def _validate_table_name(table: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", table):
            raise ShareCfgError(
                "invalid_table", "Недопустимое имя ShareCfg", table=table
            )
        return table

    @staticmethod
    def _matching_brace(text: str, start: int) -> int:
        depth = 0
        quote = ""
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in {'"', "'"}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index
        return -1

    @classmethod
    def _assignments(cls, text: str, table: str) -> dict[int, Any]:
        pattern = re.compile(
            rf"(?:_G\.)?pg(?:\.base)?\.{re.escape(table)}\[(\d+)\]\s*=\s*\{{"
        )
        result: dict[int, Any] = {}
        for match in pattern.finditer(text):
            start = match.end() - 1
            end = cls._matching_brace(text, start)
            if end < 0:
                # Некоторые generated ShareCfg завершаются ленивым служебным
                # присваиванием. Оно не является строкой данных; уже полностью
                # декодированные записи остаются пригодными.
                continue
            result[int(match.group(1))] = slpp.decode(text[start : end + 1])
        return result

    @staticmethod
    def _legacy_table(text: str, table: str) -> dict[int, Any]:
        patterns = (
            rf"pg\.{re.escape(table)}\s*=\s*\{{",
            rf'rawset\(pg,\s*"{re.escape(table)}".*?\{{',
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                start = match.end() - 1
                end = ShareCfgLoader._matching_brace(text, start)
                if end >= 0:
                    decoded = slpp.decode(text[start : end + 1])
                    return decoded if isinstance(decoded, dict) else {}
        return {}

    def _read(self, path: Path, table: str) -> str:
        resolved = path.resolve()
        if self.server_root not in resolved.parents:
            raise ShareCfgError(
                "source_escape", "Путь вышел за пределы server snapshot", table=table
            )
        try:
            return resolved.read_text(encoding="utf-8")
        except OSError as exc:
            raise ShareCfgError("source_read_failed", str(exc), table=table) from exc

    def _load_fixture_table(self, table: str) -> dict[int, Any] | None:
        manifest = self._fixture_manifest
        if manifest is None:
            return None
        records = manifest["records"]
        hashes = manifest["sha256"]
        expected_count = records.get(table)
        if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
            raise ShareCfgError(
                "fixture_manifest_invalid",
                f"Manifest fixture не содержит корректный records.{table}",
                table=table,
            )

        fixture = (self.server_root / "sharecfgjson" / f"{table}.json").resolve()
        if not fixture.is_file():
            raise ShareCfgError(
                "fixture_table_missing",
                f"Manifest fixture объявляет ShareCfg {table}, но JSON-файл отсутствует",
                table=table,
            )
        relative = fixture.relative_to(self.snapshot.root).as_posix()
        expected_hash = hashes.get(relative)
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ShareCfgError(
                "fixture_manifest_invalid",
                f"Manifest fixture не содержит корректный SHA-256 для {relative}",
                table=table,
            )
        try:
            payload = fixture.read_bytes()
        except OSError as exc:
            raise ShareCfgError("source_read_failed", str(exc), table=table) from exc
        if sha256(payload).hexdigest() != expected_hash:
            raise ShareCfgError(
                "fixture_hash_mismatch",
                f"SHA-256 производного ShareCfg fixture {table} не совпадает с manifest",
                table=table,
            )
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ShareCfgError(
                "fixture_json_invalid", str(exc), table=table
            ) from exc
        if not isinstance(raw, dict) or len(raw) != expected_count:
            raise ShareCfgError(
                "unsupported_table_shape",
                f"Fixture ShareCfg {table} не соответствует records={expected_count}",
                table=table,
            )

        def restore_keys(value: Any) -> Any:
            if isinstance(value, dict):
                restored = {}
                for key, item in value.items():
                    normalized = key
                    if isinstance(key, str) and re.fullmatch(r"-?\d+", key):
                        normalized = int(key)
                    elif isinstance(key, str) and re.fullmatch(r"-?\d+\.\d+", key):
                        normalized = float(key)
                    if normalized in restored:
                        raise ShareCfgError(
                            "fixture_key_collision",
                            f"Fixture ShareCfg {table} содержит конфликт числовых ключей",
                            table=table,
                        )
                    restored[normalized] = restore_keys(item)
                return restored
            if isinstance(value, list):
                return [restore_keys(item) for item in value]
            return value

        return restore_keys(raw)

    def load_table(self, table: str) -> dict[int, Any]:
        table = self._validate_table_name(table)
        if table in self._cache:
            return self._cache[table]

        fixture = self._load_fixture_table(table)
        if fixture is not None:
            self._cache[table] = fixture
            return fixture

        wrapper = self.server_root / "sharecfg" / f"{table}.lua"
        full = self.server_root / "sharecfgdata" / f"{table}.lua"
        selected: Path | None = None
        if wrapper.is_file():
            wrapper_text = self._read(wrapper, table)
            if re.search(
                rf"pg\.{re.escape(table)}\.__stream__\s*=\s*true", wrapper_text
            ):
                if not full.is_file():
                    raise ShareCfgError(
                        "stream_companion_missing",
                        f"Для streamed ShareCfg {table} нет sharecfgdata companion",
                        table=table,
                    )
                selected = full
            else:
                parsed = self._assignments(wrapper_text, table) or self._legacy_table(
                    wrapper_text, table
                )
                if parsed:
                    self._cache[table] = parsed
                    return parsed
        if selected is None and full.is_file():
            selected = full
        if selected is None:
            raise ShareCfgError(
                "table_missing", f"ShareCfg {table} отсутствует", table=table
            )

        text = self._read(selected, table)
        parsed = self._assignments(text, table) or self._legacy_table(text, table)
        if not parsed:
            raise ShareCfgError(
                "unsupported_table_shape",
                f"ShareCfg {table} не соответствует поддерживаемой безопасной форме",
                table=table,
            )
        self._cache[table] = parsed
        return parsed
