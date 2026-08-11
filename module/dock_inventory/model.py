"""Pure domain model for Dock Inventory scan observations."""

from dataclasses import dataclass
from enum import Enum
from typing import Iterator


class IdentityStatus(Enum):
    """Resolution state of a ship observation's canonical identity."""

    UNRESOLVED = "unresolved"
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"


class AffinityState(Enum):
    """Observable affinity/oath classification for one dock card."""

    UNKNOWN = "unknown"
    BELOW_100 = "below_100"
    AFFINITY_100 = "affinity_100"
    OATH = "oath"


@dataclass(frozen=True, slots=True)
class CanonicalShipIdentity:
    """Opaque canonical ship key, never a concrete ship-instance identifier.

    Stage 1 intentionally does not assign this key to a ship_data_group,
    ship_data_template, retrofit, or other upstream numeric namespace. That
    mapping belongs to the later identity-matching stage.
    """

    key: str

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("canonical identity key must not be blank")


@dataclass(frozen=True, slots=True)
class StarObservation:
    """Raw observable star counts from one dock card."""

    filled: int
    empty: int
    total: int

    def __post_init__(self) -> None:
        if self.filled < 0:
            raise ValueError("filled star count must be non-negative")
        if self.empty < 0:
            raise ValueError("empty star count must be non-negative")
        if self.total < 0:
            raise ValueError("total star count must be non-negative")
        if self.filled + self.empty != self.total:
            raise ValueError("filled plus empty star counts must equal total")


@dataclass(frozen=True, slots=True)
class DockShipObservation:
    """One scan-local observation of a concrete dock card.

    ``ordinal`` identifies an observation only inside one scan result. It is
    not a persistent or server-side ship-instance identifier and must not be
    compared across scans as if it were one.
    """

    ordinal: int
    identity_status: IdentityStatus = IdentityStatus.UNRESOLVED
    raw_name_ocr: str | None = None
    displayed_name: str | None = None
    canonical_identity: CanonicalShipIdentity | None = None
    canonical_name: str | None = None
    level: int | None = None
    stars: StarObservation | None = None
    affinity: AffinityState = AffinityState.UNKNOWN

    def __post_init__(self) -> None:
        if not isinstance(self.identity_status, IdentityStatus):
            raise TypeError("identity_status must be an IdentityStatus")
        if not isinstance(self.affinity, AffinityState):
            raise TypeError("affinity must be an AffinityState")
        if self.ordinal < 0:
            raise ValueError("observation ordinal must be non-negative")

        if self.level is not None and self.level < 1:
            raise ValueError("known ship level must be at least 1")

        if self.identity_status is IdentityStatus.MATCHED:
            if self.canonical_identity is None:
                raise ValueError("matched observation requires canonical identity")
            if self.canonical_name is None or not self.canonical_name.strip():
                raise ValueError("matched observation requires canonical name")
        else:
            if self.canonical_identity is not None:
                raise ValueError("only matched observations may carry canonical identity")
            if self.canonical_name is not None:
                raise ValueError("only matched observations may carry canonical name")


@dataclass(frozen=True, slots=True)
class DockInventoryScanResult:
    """Ordered immutable aggregate of dock-card observations."""

    observations: tuple[DockShipObservation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple):
            raise TypeError("observations must be a tuple to preserve immutability")

        ordinals = [observation.ordinal for observation in self.observations]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("observation ordinals must be unique within a scan result")

    def __iter__(self) -> Iterator[DockShipObservation]:
        return iter(self.observations)

    def __len__(self) -> int:
        return len(self.observations)
