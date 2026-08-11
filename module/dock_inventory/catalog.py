"""Strict offline canonical ship catalog for Dock Inventory identity scans."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from module.dock_inventory.model import CanonicalShipIdentity

CATALOG_SCHEMA_VERSION = 1
CATALOG_LANGUAGE = "en"
CATALOG_IDENTITY_SCHEME = "azur_lane_ship_group"
DEFAULT_CATALOG_PATH = (
    Path(__file__).parents[2] / "assets" / "ship" / "dock_identity_catalog.json"
)
_CANONICAL_ID_RE = re.compile(r"^azur_lane_ship_group:[1-9][0-9]*$")


class DockIdentityCatalogError(RuntimeError):
    """The tracked canonical identity catalog is missing or invalid."""


def normalize_ship_name(value: str) -> str:
    """Return the conservative comparison key used by catalog and resolver.

    NFKC is applied before removing Unicode whitespace and case-folding. No
    punctuation, Roman numeral, META/II suffix, accent, or script is dropped.
    """

    if not isinstance(value, str):
        raise TypeError("ship name must be a string")
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


@dataclass(frozen=True, slots=True)
class DockCanonicalShip:
    canonical_id: str
    canonical_name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_id, str) or not _CANONICAL_ID_RE.fullmatch(
            self.canonical_id
        ):
            raise DockIdentityCatalogError(
                f"Недопустимый canonical_id: {self.canonical_id!r}."
            )
        if not isinstance(self.canonical_name, str) or not self.canonical_name.strip():
            raise DockIdentityCatalogError("Canonical name должен быть непустой строкой.")
        if not isinstance(self.aliases, tuple) or any(
            not isinstance(alias, str) or not alias.strip() for alias in self.aliases
        ):
            raise DockIdentityCatalogError("Aliases должны быть tuple непустых строк.")
        if len(self.aliases) != len(set(self.aliases)):
            raise DockIdentityCatalogError("Aliases не должны содержать дубликаты.")
        if self.canonical_name in self.aliases:
            raise DockIdentityCatalogError("Canonical name не должен дублироваться в aliases.")

    @property
    def identity(self) -> CanonicalShipIdentity:
        return CanonicalShipIdentity(self.canonical_id)

    @property
    def names(self) -> tuple[str, ...]:
        return (self.canonical_name, *self.aliases)


@dataclass(frozen=True, slots=True)
class DockCatalogProvenance:
    source_repository: str
    source_commit: str
    source_path: str
    source_blob_sha: str
    source_sha256: str
    source_generator_path: str
    source_generator_blob_sha: str
    supplemental_source_repository: str
    supplemental_source_commit: str
    supplemental_source_path: str
    supplemental_source_blob_sha: str
    selection_contract: str

    def __post_init__(self) -> None:
        values = {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise DockIdentityCatalogError(
                "Все поля provenance должны быть непустыми строками."
            )
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise DockIdentityCatalogError("source_commit должен быть полным Git SHA-1.")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_blob_sha):
            raise DockIdentityCatalogError("source_blob_sha должен быть Git blob SHA-1.")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise DockIdentityCatalogError("source_sha256 должен быть SHA-256.")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_generator_blob_sha):
            raise DockIdentityCatalogError(
                "source_generator_blob_sha должен быть Git blob SHA-1."
            )
        if not re.fullmatch(r"[0-9a-f]{40}", self.supplemental_source_commit):
            raise DockIdentityCatalogError(
                "supplemental_source_commit должен быть полным Git SHA-1."
            )
        if not re.fullmatch(r"[0-9a-f]{40}", self.supplemental_source_blob_sha):
            raise DockIdentityCatalogError(
                "supplemental_source_blob_sha должен быть Git blob SHA-1."
            )


@dataclass(frozen=True, slots=True)
class DockIdentityCatalog:
    records: tuple[DockCanonicalShip, ...]
    provenance: DockCatalogProvenance
    schema_version: int = CATALOG_SCHEMA_VERSION
    language: str = CATALOG_LANGUAGE
    identity_scheme: str = CATALOG_IDENTITY_SCHEME
    _by_normalized_name: dict[str, tuple[DockCanonicalShip, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != CATALOG_SCHEMA_VERSION:
            raise DockIdentityCatalogError(
                f"Неподдерживаемая версия catalog schema: {self.schema_version!r}."
            )
        if self.language != CATALOG_LANGUAGE:
            raise DockIdentityCatalogError(
                f"Неподдерживаемый язык catalog: {self.language!r}."
            )
        if self.identity_scheme != CATALOG_IDENTITY_SCHEME:
            raise DockIdentityCatalogError(
                f"Неподдерживаемая identity scheme: {self.identity_scheme!r}."
            )
        if not isinstance(self.records, tuple) or not self.records:
            raise DockIdentityCatalogError("Catalog records должны быть непустым tuple.")
        if not all(isinstance(record, DockCanonicalShip) for record in self.records):
            raise DockIdentityCatalogError(
                "Catalog records должны содержать DockCanonicalShip."
            )
        if not isinstance(self.provenance, DockCatalogProvenance):
            raise DockIdentityCatalogError("Catalog provenance имеет неверный тип.")
        identifiers = tuple(record.canonical_id for record in self.records)
        if len(identifiers) != len(set(identifiers)):
            raise DockIdentityCatalogError(
                "Catalog содержит повторяющиеся canonical identities."
            )
        if identifiers != tuple(sorted(identifiers, key=_canonical_id_sort_key)):
            raise DockIdentityCatalogError(
                "Catalog records должны быть детерминированно отсортированы по group id."
            )

        index: dict[str, list[DockCanonicalShip]] = defaultdict(list)
        for record in self.records:
            for name in record.names:
                normalized = normalize_ship_name(name)
                if not normalized:
                    raise DockIdentityCatalogError(
                        f"Имя {name!r} нормализовалось в пустую строку."
                    )
                if all(existing.canonical_id != record.canonical_id for existing in index[normalized]):
                    index[normalized].append(record)
        object.__setattr__(
            self,
            "_by_normalized_name",
            {
                key: tuple(sorted(values, key=lambda item: item.canonical_id))
                for key, values in sorted(index.items())
            },
        )

    @property
    def by_normalized_name(self) -> dict[str, tuple[DockCanonicalShip, ...]]:
        return dict(self._by_normalized_name)

    @property
    def normalized_collisions(self) -> dict[str, tuple[DockCanonicalShip, ...]]:
        return {
            key: records
            for key, records in self._by_normalized_name.items()
            if len(records) > 1
        }

    @property
    def alias_count(self) -> int:
        return sum(len(record.aliases) for record in self.records)

    @property
    def fingerprint(self) -> str:
        semantic = {
            "schema_version": self.schema_version,
            "language": self.language,
            "identity_scheme": self.identity_scheme,
            "records": [
                {
                    "canonical_id": record.canonical_id,
                    "canonical_name": record.canonical_name,
                    "aliases": list(record.aliases),
                }
                for record in self.records
            ],
        }
        encoded = json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def candidates_for_exact_name(self, normalized: str) -> tuple[DockCanonicalShip, ...]:
        return self._by_normalized_name.get(normalized, ())

    @classmethod
    def from_mapping(cls, payload: object) -> DockIdentityCatalog:
        if not isinstance(payload, dict):
            raise DockIdentityCatalogError("Catalog top level должен быть JSON object.")
        required = {
            "schema_version",
            "language",
            "identity_scheme",
            "provenance",
            "records",
        }
        if set(payload) != required:
            raise DockIdentityCatalogError(
                "Catalog top-level schema не совпадает с version 1 contract."
            )
        records_payload = payload["records"]
        if not isinstance(records_payload, list):
            raise DockIdentityCatalogError("Catalog records должен быть JSON array.")
        records = []
        for raw in records_payload:
            if not isinstance(raw, dict) or set(raw) != {
                "canonical_id",
                "canonical_name",
                "aliases",
            }:
                raise DockIdentityCatalogError("Record schema не совпадает с contract.")
            aliases = raw["aliases"]
            if not isinstance(aliases, list):
                raise DockIdentityCatalogError("Record aliases должен быть JSON array.")
            records.append(
                DockCanonicalShip(
                    canonical_id=raw["canonical_id"],
                    canonical_name=raw["canonical_name"],
                    aliases=tuple(aliases),
                )
            )

        provenance_payload = payload["provenance"]
        provenance_fields = set(DockCatalogProvenance.__dataclass_fields__)
        if not isinstance(provenance_payload, dict) or set(provenance_payload) != provenance_fields:
            raise DockIdentityCatalogError("Provenance schema не совпадает с contract.")
        return cls(
            schema_version=payload["schema_version"],
            language=payload["language"],
            identity_scheme=payload["identity_scheme"],
            provenance=DockCatalogProvenance(**provenance_payload),
            records=tuple(records),
        )


def _canonical_id_sort_key(value: str) -> int:
    try:
        return int(value.rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise DockIdentityCatalogError(f"Недопустимый canonical_id: {value!r}.") from exc


def load_dock_identity_catalog(
    path: Path | str = DEFAULT_CATALOG_PATH,
) -> DockIdentityCatalog:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DockIdentityCatalogError(f"Catalog не найден: {source}.") from exc
    except OSError as exc:
        raise DockIdentityCatalogError(f"Catalog не удалось прочитать: {source}.") from exc
    except json.JSONDecodeError as exc:
        raise DockIdentityCatalogError(f"Catalog содержит неверный JSON: {source}.") from exc
    return DockIdentityCatalog.from_mapping(payload)
