"""Bounded parsers legacy SQLite/CL1 без production singleton и side effects."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Iterable
from urllib.parse import quote
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import PBKDF2

from module.application.migration_models import (
    IdentityEvidence,
    LegacyIdentity,
    LegacyMigrationPlan,
    MigrationRecord,
    RecordDisposition,
    SourceManifestEntry,
    canonical_digest,
)

_IDENTITY_NAMESPACE = UUID("bc6db2da-cb91-4d6e-bc33-bb598d715c13")
_REQUIRED_CL1_COLUMNS = {"instance", "month", "data_json", "encrypted_blob"}
_RESOURCE_COLUMNS = (
    "oil",
    "coin",
    "gem",
    "pt",
    "cube",
    "core",
    "medal",
    "merit",
    "guild_coin",
    "action_point",
    "yellow_coin",
    "purple_coin",
)
_REQUIRED_RESOURCE_COLUMNS = {"id", "instance", "ts", *_RESOURCE_COLUMNS}
_REQUIRED_OPSI_COLUMNS = {
    "id",
    "imgid",
    "server",
    "zone",
    "zone_type",
    "zone_id",
    "hazard_level",
    "item",
    "amount",
    "tag",
    "device_id",
    "genre",
    "combat_count",
    "created_at",
}
_CL1_KEYS = {
    "battle_count",
    "akashi_encounters",
    "akashi_ap",
    "akashi_ap_entries",
    "ap_snapshots",
    "yellow_coin_snapshots",
    "coins_snapshots",
    "meow_battle_raw_count",
    "meow_battle_count",
    "meow_round_times",
    "meow_battle_times",
    "meow_hazard_stats",
    "siren_research_devices",
    "siren_research_device_entries",
    "commission_income_entries",
    "last_ap_notification",
}
_MONTH_RE = re.compile(r"^(?P<year>20\d{2})-(?P<month>0[1-9]|1[0-2])$")


class LegacySourceError(ValueError):
    """Источник нарушает bounded/read-only migration contract."""


class LegacySourceReader:
    MAX_SQLITE_SIZE = 64 * 1024 * 1024
    MAX_JSON_SIZE = 8 * 1024 * 1024
    MAX_CSV_SIZE = 2 * 1024 * 1024
    MAX_JSON_DEPTH = 12
    MAX_COLLECTION_ITEMS = 200_000
    MAX_TEXT = 4_096
    MAX_LEGACY_ENTRIES = 10_000
    MAX_LEGACY_SOURCES = 2_048

    def __init__(
        self,
        source_root: Path,
        *,
        legacy_timezone: str,
        profile_names: Iterable[str] = (),
        decryption_ids: Iterable[str] = (),
    ):
        try:
            self._root = source_root.resolve(strict=True)
        except OSError as exc:
            raise LegacySourceError("SOURCE_ROOT_INVALID") from exc
        if not self._root.is_dir():
            raise LegacySourceError("SOURCE_ROOT_NOT_DIRECTORY")
        try:
            self._timezone = ZoneInfo(legacy_timezone)
        except ZoneInfoNotFoundError as exc:
            raise LegacySourceError("TIMEZONE_POLICY_INVALID") from exc
        self._timezone_name = legacy_timezone
        self._profile_digests = {
            self._identity_digest(name) for name in profile_names if name
        }
        self._decryption_keys = tuple(
            self._derive_key(value) for value in decryption_ids if value
        )

    def capture(self) -> LegacyMigrationPlan:
        try:
            return self._capture()
        except LegacySourceError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise LegacySourceError("SOURCE_READ_FAILED") from exc

    def _capture(self) -> LegacyMigrationPlan:
        manifest: list[SourceManifestEntry] = []
        identities: dict[str, LegacyIdentity] = {}
        records: list[MigrationRecord] = []

        primary_cl1 = self._optional("config/cl1_data.db")
        if primary_cl1 is not None:
            item, parsed, found = self._parse_cl1_sqlite(primary_cl1, "cl1-primary")
            manifest.append(item)
            records.extend(parsed)
            identities.update(found)

        azurstats = self._optional("config/azurstats_local.db")
        if azurstats is not None:
            item, parsed, found = self._parse_azurstats(azurstats)
            manifest.append(item)
            records.extend(parsed)
            identities.update(found)

        legacy_db = self._optional("log/cl1/cl1_data.db")
        if legacy_db is not None:
            item, parsed, found = self._parse_cl1_sqlite(legacy_db, "cl1-legacy-sqlite")
            manifest.append(item)
            records.extend(parsed)
            identities.update(found)

        legacy_root = self._optional_directory("log/cl1")
        if legacy_root is not None:
            candidates: list[Path] = []
            for index, path in enumerate(legacy_root.rglob("*"), start=1):
                if index > self.MAX_LEGACY_ENTRIES:
                    raise LegacySourceError("LEGACY_SOURCE_COUNT_EXCEEDED")
                if path.is_file() and path.name in {
                    "cl1_monthly.json",
                    "cl1_monthly.json.bak",
                }:
                    candidates.append(path)
                    if len(candidates) > self.MAX_LEGACY_SOURCES:
                        raise LegacySourceError("LEGACY_SOURCE_COUNT_EXCEEDED")
            candidates.sort()
            for path in candidates:
                safe = self._bounded(path)
                logical_id = f"cl1-legacy-json-{self._digest_file(safe)[:12]}"
                item, parsed, found = self._parse_cl1_json(safe, logical_id)
                manifest.append(item)
                records.extend(parsed)
                identities.update(found)

        csv_parity: bool | None = None
        derived_csv = self._optional("log/azurstat_meowofficer_farming.csv")
        if derived_csv is not None:
            self._require_size(derived_csv, self.MAX_CSV_SIZE)
            manifest.append(self._manifest(derived_csv, "meow-derived-csv", "csv"))
            csv_parity = self._compare_meow_csv(derived_csv, records)

        ordered_manifest = tuple(sorted(manifest, key=lambda item: item.logical_id))
        manifest_digest = canonical_digest(
            [
                (item.logical_id, item.source_kind, item.size, item.sha256)
                for item in ordered_manifest
            ]
        )
        return LegacyMigrationPlan(
            manifest=ordered_manifest,
            manifest_digest=manifest_digest,
            identities=tuple(
                sorted(identities.values(), key=lambda item: item.alias_digest)
            ),
            records=tuple(
                sorted(
                    records,
                    key=lambda item: (
                        item.dataset,
                        item.identity_digest,
                        item.source_object,
                        item.source_locator,
                    ),
                )
            ),
            timezone_policy=f"explicit:{self._timezone_name}",
            derived_csv_parity=csv_parity,
        )

    def _optional(self, relative: str) -> Path | None:
        candidate = self._root / relative
        if not candidate.exists():
            return None
        bounded = self._bounded(candidate)
        if not bounded.is_file():
            raise LegacySourceError("SOURCE_NOT_REGULAR_FILE")
        return bounded

    def _optional_directory(self, relative: str) -> Path | None:
        candidate = self._root / relative
        if not candidate.exists():
            return None
        bounded = self._bounded(candidate)
        if not bounded.is_dir():
            raise LegacySourceError("SOURCE_NOT_DIRECTORY")
        return bounded

    def _bounded(self, path: Path) -> Path:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self._root):
            raise LegacySourceError("SOURCE_PATH_ESCAPE")
        current = path
        while current != self._root:
            if current.is_symlink() or current.is_junction():
                raise LegacySourceError("SOURCE_SYMLINK_FORBIDDEN")
            current = current.parent
        return resolved

    @staticmethod
    def _digest_file(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _identity_digest(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _derive_key(device_id: str) -> bytes:
        return PBKDF2(
            device_id.encode("utf-8"),
            b"AlasCl1SecureStorage",
            dkLen=32,
            count=1000,
            hmac_hash_module=SHA256,
        )

    def _identity(self, raw_value: str) -> LegacyIdentity:
        if not isinstance(raw_value, str) or not raw_value or len(raw_value) > 512:
            raise LegacySourceError("IDENTITY_INVALID")
        digest = self._identity_digest(raw_value)
        evidence = (
            IdentityEvidence.EXACT_PROFILE
            if digest in self._profile_digests
            else IdentityEvidence.UNRESOLVED
        )
        return LegacyIdentity(
            alias_kind="legacy_instance",
            alias_digest=digest,
            internal_id=uuid5(_IDENTITY_NAMESPACE, digest),
            evidence=evidence,
        )

    def _manifest(
        self,
        path: Path,
        logical_id: str,
        source_kind: str,
        *,
        schema_fingerprint: str | None = None,
        integrity: str | None = None,
    ) -> SourceManifestEntry:
        return SourceManifestEntry(
            logical_id=logical_id,
            source_kind=source_kind,
            size=path.stat().st_size,
            sha256=self._digest_file(path),
            schema_fingerprint=schema_fingerprint,
            integrity=integrity,
        )

    @staticmethod
    def _sqlite_uri(path: Path) -> str:
        normalized = path.resolve(strict=True).as_posix()
        return f"file:{quote(normalized, safe='/:')}?mode=ro&immutable=1"

    def _open_sqlite(self, path: Path) -> sqlite3.Connection:
        self._require_size(path, self.MAX_SQLITE_SIZE)
        connection = sqlite3.connect(self._sqlite_uri(path), uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
        if table not in {"cl1_data", "resource_snapshots", "opsi_items"}:
            raise LegacySourceError("SQLITE_TABLE_NOT_ALLOWED")
        return tuple(
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        )

    @staticmethod
    def _require_columns(
        actual: Iterable[str], required: set[str], reason: str
    ) -> None:
        if not required.issubset(set(actual)):
            raise LegacySourceError(reason)

    @staticmethod
    def _integrity(connection: sqlite3.Connection) -> str:
        rows = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
        if rows != ("ok",):
            raise LegacySourceError("SQLITE_INTEGRITY_FAILED")
        return "ok"

    def _parse_cl1_sqlite(
        self, path: Path, logical_id: str
    ) -> tuple[SourceManifestEntry, list[MigrationRecord], dict[str, LegacyIdentity]]:
        parsed: list[MigrationRecord] = []
        identities: dict[str, LegacyIdentity] = {}
        with closing(self._open_sqlite(path)) as connection:
            columns = self._columns(connection, "cl1_data")
            self._require_columns(
                columns, _REQUIRED_CL1_COLUMNS, "CL1_SCHEMA_UNSUPPORTED"
            )
            integrity = self._integrity(connection)
            schema_fingerprint = canonical_digest(("cl1_data", columns))
            rows = connection.execute(
                "SELECT instance, month, data_json, encrypted_blob "
                "FROM cl1_data ORDER BY instance, month"
            )
            for row in rows:
                identity = self._identity(row["instance"])
                identities[identity.alias_digest] = identity
                data = self._load_cl1_payload(row["data_json"], row["encrypted_blob"])
                base = f"month:{row['month']}:identity:{identity.alias_digest[:16]}"
                if data is None:
                    parsed.append(
                        self._quarantine(
                            "cl1_row",
                            identity.alias_digest,
                            logical_id,
                            base,
                            "CL1_PAYLOAD_UNREADABLE",
                        )
                    )
                    continue
                try:
                    parsed.extend(
                        self._parse_cl1_data(
                            data,
                            identity.alias_digest,
                            row["month"],
                            logical_id,
                            base,
                        )
                    )
                except LegacySourceError, InvalidOperation, TypeError, ValueError:
                    parsed.append(
                        self._quarantine(
                            "cl1_row",
                            identity.alias_digest,
                            logical_id,
                            base,
                            "CL1_RECORD_INVALID",
                        )
                    )
        return (
            self._manifest(
                path,
                logical_id,
                "cl1_sqlite",
                schema_fingerprint=schema_fingerprint,
                integrity=integrity,
            ),
            parsed,
            identities,
        )

    def _load_cl1_payload(self, text: str | None, blob: bytes | None) -> dict | None:
        if text:
            if len(text.encode("utf-8")) > self.MAX_JSON_SIZE:
                raise LegacySourceError("CL1_JSON_TOO_LARGE")
            try:
                data = json.loads(text)
            except json.JSONDecodeError, RecursionError:
                return None
            self._validate_json(data)
            return data if isinstance(data, dict) else None
        if not blob or len(blob) < 32:
            return None
        for key in self._decryption_keys:
            plaintext = b""
            try:
                try:
                    cipher = AES.new(key, AES.MODE_GCM, nonce=blob[:16])
                    plaintext = cipher.decrypt_and_verify(blob[32:], blob[16:32])
                except ValueError:
                    continue
                if len(plaintext) > self.MAX_JSON_SIZE:
                    raise LegacySourceError("CL1_JSON_TOO_LARGE")
                try:
                    data = json.loads(plaintext.decode("utf-8"))
                except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
                    continue
                self._validate_json(data)
                if isinstance(data, dict):
                    return data
            finally:
                plaintext = b""
        return None

    def _parse_cl1_json(
        self, path: Path, logical_id: str
    ) -> tuple[SourceManifestEntry, list[MigrationRecord], dict[str, LegacyIdentity]]:
        self._require_size(path, self.MAX_JSON_SIZE)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise LegacySourceError("CL1_LEGACY_JSON_INVALID") from exc
        self._validate_json(payload)
        if not isinstance(payload, dict):
            raise LegacySourceError("CL1_LEGACY_JSON_SHAPE")
        identity = self._identity(path.parent.name)
        records: list[MigrationRecord] = []
        suffixes = ("", "-akashi", "-akashi-ap", "-akashi-ap-entries")
        months: set[str] = set()
        for key in payload:
            if not isinstance(key, str) or len(key) < 7:
                months.clear()
                break
            month_text = key[:7]
            if not _MONTH_RE.fullmatch(month_text) or key[7:] not in suffixes:
                months.clear()
                break
            months.add(month_text)
        if not months:
            records.append(
                self._quarantine(
                    "cl1_legacy_json",
                    identity.alias_digest,
                    logical_id,
                    "legacy-json",
                    "CL1_SHAPE_UNKNOWN",
                )
            )
        else:
            metric_suffixes = {
                "battle_count": "",
                "akashi_encounters": "-akashi",
                "akashi_ap": "-akashi-ap",
            }
            for month_text in sorted(months):
                month = self._month(month_text)
                base = f"month:{month_text}:identity:{identity.alias_digest[:16]}"
                for metric, suffix in metric_suffixes.items():
                    key = month_text + suffix
                    if key not in payload:
                        continue
                    try:
                        records.append(
                            self._record(
                                "monthly_aggregate",
                                identity.alias_digest,
                                logical_id,
                                f"{base}/{metric}",
                                month=month,
                                metric=metric,
                                value=self._nonnegative_decimal(payload[key], metric),
                                source_kind="legacy_aggregate",
                            )
                        )
                    except LegacySourceError, InvalidOperation, TypeError, ValueError:
                        records.append(
                            self._quarantine(
                                "monthly_aggregate",
                                identity.alias_digest,
                                logical_id,
                                f"{base}/{metric}",
                                "CL1_RECORD_INVALID",
                            )
                        )
                entries_key = month_text + "-akashi-ap-entries"
                if entries_key in payload:
                    records.extend(
                        self._parse_list(
                            {"akashi_ap_entries": payload[entries_key]},
                            "akashi_ap_entries",
                            identity.alias_digest,
                            logical_id,
                            base,
                            self._ap_purchase,
                        )
                    )
        return (
            self._manifest(path, logical_id, "cl1_json"),
            records,
            {identity.alias_digest: identity},
        )

    def _parse_cl1_data(
        self,
        data: dict,
        identity: str,
        month_text: str,
        source: str,
        base: str,
    ) -> list[MigrationRecord]:
        month = self._month(month_text)
        unknown = sorted(set(data) - _CL1_KEYS)
        if unknown:
            return [
                self._quarantine("cl1_row", identity, source, base, "CL1_SHAPE_UNKNOWN")
            ]
        output: list[MigrationRecord] = []
        for key in (
            "battle_count",
            "akashi_encounters",
            "akashi_ap",
            "meow_battle_raw_count",
            "meow_battle_count",
        ):
            value = self._nonnegative_decimal(data.get(key, 0), key)
            output.append(
                self._record(
                    "monthly_aggregate",
                    identity,
                    source,
                    f"{base}/{key}",
                    month=month,
                    metric=key,
                    value=value,
                    source_kind="legacy_aggregate",
                )
            )

        output.extend(
            self._parse_list(
                data, "akashi_ap_entries", identity, source, base, self._ap_purchase
            )
        )
        output.extend(
            self._parse_list(
                data, "ap_snapshots", identity, source, base, self._ap_snapshot
            )
        )
        output.extend(
            self._parse_list(
                data,
                "yellow_coin_snapshots",
                identity,
                source,
                base,
                self._yellow_snapshot,
            )
        )
        output.extend(
            self._parse_list(
                data, "coins_snapshots", identity, source, base, self._coins_snapshot
            )
        )
        output.extend(
            self._parse_list(
                data,
                "commission_income_entries",
                identity,
                source,
                base,
                self._commission,
            )
        )
        output.extend(self._parse_meow(data, identity, source, base, month))
        output.extend(self._parse_siren(data, identity, source, base, month))
        if "last_ap_notification" in data:
            entry = data["last_ap_notification"]
            if not isinstance(entry, dict):
                output.append(
                    self._quarantine(
                        "ap_notification",
                        identity,
                        source,
                        f"{base}/last_ap_notification",
                        "CL1_SHAPE_UNKNOWN",
                    )
                )
            else:
                output.append(
                    self._record(
                        "ap_notification",
                        identity,
                        source,
                        f"{base}/last_ap_notification",
                        **self._timestamp_values(entry.get("ts")),
                        last_ap=self._nonnegative_int(entry.get("ap"), "ap"),
                    )
                )
        return output

    def _parse_list(self, data, key, identity, source, base, parser):
        entries = data.get(key, [])
        if not isinstance(entries, list) or len(entries) > self.MAX_COLLECTION_ITEMS:
            return [
                self._quarantine(
                    key, identity, source, f"{base}/{key}", "CL1_SHAPE_UNKNOWN"
                )
            ]
        output: list[MigrationRecord] = []
        for ordinal, entry in enumerate(entries):
            locator = f"{base}/{key}/{ordinal}"
            try:
                output.extend(parser(entry, identity, source, locator))
            except LegacySourceError, InvalidOperation, TypeError, ValueError:
                output.append(
                    self._quarantine(
                        key, identity, source, locator, "CL1_RECORD_INVALID"
                    )
                )
        return output

    def _ap_purchase(self, entry, identity, source, locator):
        self._dict_keys(entry, {"ts", "amount", "base", "count", "source"})
        return [
            self._record(
                "ap_purchase",
                identity,
                source,
                locator,
                **self._timestamp_values(entry["ts"]),
                amount=self._nonnegative_int(entry["amount"], "amount"),
                base_amount=self._nonnegative_int(entry["base"], "base"),
                purchase_count=self._nonnegative_int(entry["count"], "count"),
                source=self._text(entry["source"], "source", 64),
            )
        ]

    def _ap_snapshot(self, entry, identity, source, locator):
        self._dict_keys(
            entry,
            {"ts", "ap", "source"},
            {"ap_total", "asset", "yellow_coin", "distance"},
        )
        values = self._timestamp_values(entry["ts"])
        values.update(
            ap=self._nonnegative_int(entry["ap"], "ap"),
            source=self._text(entry["source"], "source", 64),
        )
        for key in ("ap_total", "yellow_coin", "distance"):
            values[key] = self._optional_nonnegative_int(entry.get(key), key)
        values["asset"] = self._optional_nonnegative_decimal(
            entry.get("asset"), "asset"
        )
        return [self._record("ap_snapshot", identity, source, locator, **values)]

    def _yellow_snapshot(self, entry, identity, source, locator):
        self._dict_keys(entry, {"ts", "yellow_coin", "source"})
        return [
            self._record(
                "currency_snapshot",
                identity,
                source,
                locator,
                **self._timestamp_values(entry["ts"]),
                currency_code="yellow_coin",
                amount=self._nonnegative_int(entry["yellow_coin"], "yellow_coin"),
                source=self._text(entry["source"], "source", 64),
            )
        ]

    def _coins_snapshot(self, entry, identity, source, locator):
        self._dict_keys(entry, {"ts", "yellow_coins", "source"}, {"purple_coins"})
        timestamp = self._timestamp_values(entry["ts"])
        output = [
            self._record(
                "currency_snapshot",
                identity,
                source,
                f"{locator}/yellow",
                **timestamp,
                currency_code="yellow_coin",
                amount=self._nonnegative_int(entry["yellow_coins"], "yellow_coins"),
                source=self._text(entry["source"], "source", 64),
            )
        ]
        if entry.get("purple_coins") is not None:
            output.append(
                self._record(
                    "currency_snapshot",
                    identity,
                    source,
                    f"{locator}/purple",
                    **timestamp,
                    currency_code="purple_coin",
                    amount=self._nonnegative_int(entry["purple_coins"], "purple_coins"),
                    source=self._text(entry["source"], "source", 64),
                )
            )
        return output

    def _commission(self, entry, identity, source, locator):
        self._dict_keys(entry, {"ts", "items", "commission_count"})
        items = entry["items"]
        if not isinstance(items, dict) or len(items) > 256:
            raise LegacySourceError("COMMISSION_ITEMS_INVALID")
        typed_items = tuple(
            sorted(
                (self._text(key, "item", 128), self._nonnegative_int(value, "amount"))
                for key, value in items.items()
            )
        )
        return [
            self._record(
                "commission",
                identity,
                source,
                locator,
                **self._timestamp_values(entry["ts"]),
                commission_count=self._nonnegative_int(
                    entry["commission_count"], "commission_count"
                ),
                source="cl1",
                items=typed_items,
            )
        ]

    def _parse_meow(self, data, identity, source, base, month):
        output: list[MigrationRecord] = []
        round_entries = data.get("meow_round_times", [])
        battle_entries = data.get("meow_battle_times", [])
        if not isinstance(round_entries, list) or not isinstance(battle_entries, list):
            return [
                self._quarantine(
                    "meow_timing",
                    identity,
                    source,
                    f"{base}/meow_timing",
                    "CL1_SHAPE_UNKNOWN",
                )
            ]
        for kind, entries in (("round", round_entries), ("battle", battle_entries)):
            for ordinal, entry in enumerate(entries):
                locator = f"{base}/meow_{kind}_times/{ordinal}"
                try:
                    if isinstance(entry, dict):
                        duration = self._nonnegative_decimal(
                            entry.get("duration"), "duration"
                        )
                        hazard = self._optional_hazard(entry.get("hazard_level"))
                        timestamp = (
                            self._timestamp_values(entry.get("ts"))
                            if entry.get("ts")
                            else self._empty_timestamp()
                        )
                    else:
                        duration = self._nonnegative_decimal(entry, "duration")
                        hazard = None
                        timestamp = self._empty_timestamp()
                    output.append(
                        self._record(
                            "meow_timing",
                            identity,
                            source,
                            locator,
                            month=month,
                            sample_kind=kind,
                            duration_seconds=duration,
                            hazard_level=hazard,
                            source="cl1",
                            **timestamp,
                        )
                    )
                except LegacySourceError, InvalidOperation, TypeError, ValueError:
                    output.append(
                        self._quarantine(
                            "meow_timing",
                            identity,
                            source,
                            locator,
                            "CL1_RECORD_INVALID",
                        )
                    )
        stats = data.get("meow_hazard_stats", {})
        if not isinstance(stats, dict):
            output.append(
                self._quarantine(
                    "meow_hazard",
                    identity,
                    source,
                    f"{base}/meow_hazard_stats",
                    "CL1_SHAPE_UNKNOWN",
                )
            )
        else:
            for hazard_text, entry in sorted(stats.items()):
                locator = f"{base}/meow_hazard_stats/{hazard_text}"
                try:
                    hazard = self._hazard(hazard_text)
                    if not isinstance(entry, dict):
                        raise LegacySourceError("MEOW_HAZARD_INVALID")
                    output.append(
                        self._record(
                            "meow_hazard",
                            identity,
                            source,
                            locator,
                            month=month,
                            hazard_level=hazard,
                            raw_battle_count=self._nonnegative_int(
                                entry.get("battle_raw_count", 0), "battle_raw_count"
                            ),
                            effective_rounds=self._nonnegative_decimal(
                                entry.get("effective_rounds", 0), "effective_rounds"
                            ),
                            source="legacy_aggregate",
                        )
                    )
                except LegacySourceError, InvalidOperation, TypeError, ValueError:
                    output.append(
                        self._quarantine(
                            "meow_hazard",
                            identity,
                            source,
                            locator,
                            "CL1_RECORD_INVALID",
                        )
                    )
        return output

    def _parse_siren(self, data, identity, source, base, month):
        output: list[MigrationRecord] = []
        stats = data.get("siren_research_devices", {"cl1": 0, "meow": {}})
        if not isinstance(stats, dict) or not isinstance(stats.get("meow", {}), dict):
            output.append(
                self._quarantine(
                    "siren_stat",
                    identity,
                    source,
                    f"{base}/siren_research_devices",
                    "CL1_SHAPE_UNKNOWN",
                )
            )
        else:
            output.append(
                self._record(
                    "siren_stat",
                    identity,
                    source,
                    f"{base}/siren_research_devices/cl1",
                    month=month,
                    source="cl1",
                    hazard_level=0,
                    device_count=self._nonnegative_int(stats.get("cl1", 0), "cl1"),
                )
            )
            for hazard_text, count in sorted(stats.get("meow", {}).items()):
                try:
                    output.append(
                        self._record(
                            "siren_stat",
                            identity,
                            source,
                            f"{base}/siren_research_devices/meow/{hazard_text}",
                            month=month,
                            source="meow",
                            hazard_level=self._hazard(hazard_text),
                            device_count=self._nonnegative_int(count, "device_count"),
                        )
                    )
                except LegacySourceError, TypeError, ValueError:
                    output.append(
                        self._quarantine(
                            "siren_stat",
                            identity,
                            source,
                            f"{base}/siren_research_devices/meow/{hazard_text}",
                            "CL1_RECORD_INVALID",
                        )
                    )
        entries = data.get("siren_research_device_entries", [])
        if not isinstance(entries, list):
            output.append(
                self._quarantine(
                    "siren_event",
                    identity,
                    source,
                    f"{base}/siren_research_device_entries",
                    "CL1_SHAPE_UNKNOWN",
                )
            )
        else:
            for ordinal, entry in enumerate(entries):
                locator = f"{base}/siren_research_device_entries/{ordinal}"
                try:
                    self._dict_keys(entry, {"ts", "source", "hazard_level"})
                    kind = self._text(entry["source"], "source", 16)
                    hazard = (
                        None if kind == "cl1" else self._hazard(entry["hazard_level"])
                    )
                    if kind not in {"cl1", "meow"}:
                        raise LegacySourceError("SIREN_SOURCE_INVALID")
                    output.append(
                        self._record(
                            "siren_event",
                            identity,
                            source,
                            locator,
                            **self._timestamp_values(entry["ts"]),
                            source=kind,
                            hazard_level=hazard,
                        )
                    )
                except LegacySourceError, TypeError, ValueError:
                    output.append(
                        self._quarantine(
                            "siren_event",
                            identity,
                            source,
                            locator,
                            "CL1_RECORD_INVALID",
                        )
                    )
        return output

    def _parse_azurstats(self, path: Path):
        records: list[MigrationRecord] = []
        identities: dict[str, LegacyIdentity] = {}
        with closing(self._open_sqlite(path)) as connection:
            integrity = self._integrity(connection)
            resource_columns = self._columns(connection, "resource_snapshots")
            opsi_columns = self._columns(connection, "opsi_items")
            self._require_columns(
                resource_columns,
                _REQUIRED_RESOURCE_COLUMNS,
                "RESOURCE_SCHEMA_UNSUPPORTED",
            )
            self._require_columns(
                opsi_columns, _REQUIRED_OPSI_COLUMNS, "OPSI_SCHEMA_UNSUPPORTED"
            )
            schema_fingerprint = canonical_digest((resource_columns, opsi_columns))
            for row in connection.execute(
                "SELECT * FROM resource_snapshots ORDER BY id"
            ):
                identity = self._identity(row["instance"])
                identities[identity.alias_digest] = identity
                locator = f"resource_snapshots/{row['id']}"
                try:
                    values = self._timestamp_values(row["ts"])
                    values.update(
                        source="azurstats_sqlite",
                        legacy_row_id=self._nonnegative_int(row["id"], "id"),
                    )
                    values.update(
                        {
                            name: self._optional_nonnegative_int(row[name], name)
                            for name in _RESOURCE_COLUMNS
                        }
                    )
                    records.append(
                        self._record(
                            "resource_snapshot",
                            identity.alias_digest,
                            "azurstats-primary",
                            locator,
                            **values,
                        )
                    )
                except LegacySourceError, TypeError, ValueError:
                    records.append(
                        self._quarantine(
                            "resource_snapshot",
                            identity.alias_digest,
                            "azurstats-primary",
                            locator,
                            "RESOURCE_RECORD_INVALID",
                        )
                    )
            for row in connection.execute("SELECT * FROM opsi_items ORDER BY id"):
                identity = self._identity(row["device_id"] or f"opsi-row-{row['id']}")
                identities[identity.alias_digest] = identity
                locator = f"opsi_items/{row['id']}"
                try:
                    observed_at = self._epoch_seconds(row["created_at"])
                    records.append(
                        self._record(
                            "opsi_item",
                            identity.alias_digest,
                            "azurstats-primary",
                            locator,
                            observed_at=observed_at,
                            legacy_row_id=self._nonnegative_int(row["id"], "id"),
                            imgid=self._text(row["imgid"], "imgid", 128),
                            server=self._optional_text(row["server"], "server", 32),
                            zone=self._optional_text(row["zone"], "zone", 128),
                            zone_type=self._optional_text(
                                row["zone_type"], "zone_type", 64
                            ),
                            zone_id=self._optional_nonnegative_int(
                                row["zone_id"], "zone_id"
                            ),
                            hazard_level=self._optional_hazard(row["hazard_level"]),
                            item_code=self._text(row["item"], "item", 128),
                            amount=self._nonnegative_int(row["amount"], "amount"),
                            tag=self._optional_text(row["tag"], "tag", 64),
                            genre=self._text(row["genre"], "genre", 64),
                            combat_count=self._optional_nonnegative_int(
                                row["combat_count"], "combat_count"
                            ),
                        )
                    )
                except LegacySourceError, TypeError, ValueError:
                    records.append(
                        self._quarantine(
                            "opsi_item",
                            identity.alias_digest,
                            "azurstats-primary",
                            locator,
                            "OPSI_RECORD_INVALID",
                        )
                    )
        return (
            self._manifest(
                path,
                "azurstats-primary",
                "azurstats_sqlite",
                schema_fingerprint=schema_fingerprint,
                integrity=integrity,
            ),
            records,
            identities,
        )

    def _compare_meow_csv(self, path: Path, records: list[MigrationRecord]) -> bool:
        try:
            with path.open("r", encoding="utf-8", newline="") as stream:
                rows = list(csv.reader(stream))
            if (
                len(rows) != 7
                or len(rows[0]) != 7
                or any(len(row) != 7 for row in rows[1:])
            ):
                return False
            actual = [[Decimal(value) for value in row] for row in rows[1:]]
        except UnicodeDecodeError, csv.Error, InvalidOperation:
            return False
        derived = [[Decimal(0) for _ in range(7)] for _ in range(6)]
        seen_images: set[str] = set()
        unit = (2, 2, 2, 3, 3, 3)
        prefixes = ("OperationCoin", "Plate", "CoordinateAbyssal", "CoordinateObscure")
        for record in records:
            if (
                record.dataset != "opsi_item"
                or record.disposition is not RecordDisposition.IMPORT
            ):
                continue
            values = record.as_dict()
            if values.get("genre") != "opsi_meowfficer_farming":
                continue
            hazard = values.get("hazard_level")
            if not isinstance(hazard, int) or not 1 <= hazard <= 6:
                continue
            image = str(values["imgid"])
            if image not in seen_images:
                seen_images.add(image)
                derived[hazard - 1][2] += Decimal(int(values.get("combat_count") or 0))
            code = str(values["item_code"])
            for index, prefix in enumerate(prefixes):
                if code.startswith(prefix):
                    derived[hazard - 1][3 + index] += Decimal(int(values["amount"]))
                    break
        for index, row in enumerate(derived):
            row[0] = Decimal(index + 1)
            row[2] /= unit[index]
            if row[2] > 0:
                for column in range(3, 7):
                    row[column] /= row[2]
        # Поле времени производное и намеренно не участвует в parity.
        return all(
            actual[r][c] == derived[r][c] for r in range(6) for c in (0, 2, 3, 4, 5, 6)
        )

    def _record(self, dataset, identity, source_object, locator, **values):
        ordered = tuple(sorted(values.items()))
        digest = canonical_digest((dataset, identity, ordered))
        return MigrationRecord(
            dataset, identity, source_object, locator, ordered, digest
        )

    def _quarantine(self, dataset, identity, source_object, locator, reason):
        values = (("reason_code", reason),)
        return MigrationRecord(
            dataset,
            identity,
            source_object,
            locator,
            values,
            canonical_digest((dataset, identity, values)),
            RecordDisposition.QUARANTINE,
            reason,
        )

    def _timestamp_values(self, literal):
        if not isinstance(literal, str) or not literal or len(literal) > 64:
            raise LegacySourceError("TIMESTAMP_INVALID")
        try:
            parsed = datetime.fromisoformat(literal)
        except ValueError as exc:
            raise LegacySourceError("TIMESTAMP_INVALID") from exc
        if parsed.tzinfo is None:
            first = parsed.replace(tzinfo=self._timezone, fold=0)
            second = parsed.replace(tzinfo=self._timezone, fold=1)
            if first.utcoffset() != second.utcoffset():
                raise LegacySourceError("TIMESTAMP_AMBIGUOUS_OR_NONEXISTENT")
            parsed = first
        return {
            "observed_at": parsed,
            "legacy_timestamp_text": literal,
            "legacy_timezone": self._timezone_name,
        }

    @staticmethod
    def _empty_timestamp():
        return {
            "observed_at": None,
            "legacy_timestamp_text": None,
            "legacy_timezone": None,
        }

    @staticmethod
    def _month(value):
        match = _MONTH_RE.fullmatch(value) if isinstance(value, str) else None
        if not match:
            raise LegacySourceError("MONTH_INVALID")
        return date(int(match["year"]), int(match["month"]), 1)

    @staticmethod
    def _nonnegative_int(value, field):
        if isinstance(value, bool):
            raise LegacySourceError(f"{field.upper()}_INVALID")
        parsed = int(value)
        if parsed < 0 or parsed != value:
            raise LegacySourceError(f"{field.upper()}_INVALID")
        return parsed

    def _optional_nonnegative_int(self, value, field):
        return None if value is None else self._nonnegative_int(value, field)

    @staticmethod
    def _nonnegative_decimal(value, field):
        if isinstance(value, bool):
            raise LegacySourceError(f"{field.upper()}_INVALID")
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            raise LegacySourceError(f"{field.upper()}_INVALID")
        return parsed

    def _optional_nonnegative_decimal(self, value, field):
        return None if value is None else self._nonnegative_decimal(value, field)

    @staticmethod
    def _text(value, field, limit):
        if not isinstance(value, str) or not value or len(value) > limit:
            raise LegacySourceError(f"{field.upper()}_INVALID")
        return value

    def _optional_text(self, value, field, limit):
        return None if value is None else self._text(value, field, limit)

    @staticmethod
    def _hazard(value):
        parsed = int(value)
        if parsed < 1 or parsed > 6:
            raise LegacySourceError("HAZARD_INVALID")
        return parsed

    def _optional_hazard(self, value):
        return None if value is None else self._hazard(value)

    @staticmethod
    def _dict_keys(value, required, optional=frozenset()):
        if (
            not isinstance(value, dict)
            or not required.issubset(value)
            or set(value) - required - set(optional)
        ):
            raise LegacySourceError("RECORD_SHAPE_INVALID")

    @staticmethod
    def _epoch_seconds(value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 946684800 <= value <= 4102444800
        ):
            raise LegacySourceError("EPOCH_SECONDS_INVALID")
        return datetime.fromtimestamp(value, tz=ZoneInfo("UTC"))

    def _require_size(self, path, maximum):
        if path.stat().st_size > maximum:
            raise LegacySourceError("SOURCE_TOO_LARGE")

    def _validate_json(self, root):
        count = 0
        stack = [(root, 0)]
        while stack:
            value, depth = stack.pop()
            count += 1
            if count > self.MAX_COLLECTION_ITEMS or depth > self.MAX_JSON_DEPTH:
                raise LegacySourceError("JSON_BOUNDS_EXCEEDED")
            if isinstance(value, dict):
                stack.extend((key, depth + 1) for key in value)
                stack.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
            elif isinstance(value, str) and len(value) > self.MAX_TEXT:
                raise LegacySourceError("JSON_TEXT_TOO_LARGE")
