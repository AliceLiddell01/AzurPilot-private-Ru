"""Dynamic slot-relative Dock ship-name OCR and strict identity resolution."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Protocol

import cv2
import numpy as np

from module.base.utils import extract_letters
from module.dock_inventory.card_grid import (
    DockCardGridScanner,
    DockCardPresence,
    DockViewportCardScan,
)
from module.dock_inventory.catalog import (
    DockCanonicalShip,
    DockIdentityCatalog,
    load_dock_identity_catalog,
    normalize_ship_name,
)
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.dock_inventory.navigation import (
    DockInventoryNavigator,
    DockPrerequisiteEvidence,
)
from module.dock_inventory.traversal import DockTraversalResult, DockTraversalViewport
from module.logger import logger
from module.ocr.ocr import Ocr


class DockIdentityResolutionMethod(Enum):
    NONE = "none"
    EXACT = "exact"
    TRUNCATED_PREFIX = "truncated_prefix"
    FUZZY = "fuzzy"


@dataclass(frozen=True, slots=True)
class DockShipIdentityResolution:
    status: IdentityStatus
    method: DockIdentityResolutionMethod
    raw_name_ocr: str
    displayed_name: str
    canonical_identity: CanonicalShipIdentity | None = None
    canonical_name: str | None = None
    ship_form: ShipForm | None = None
    best_score: float | None = None
    runner_up_score: float | None = None
    candidate_count: int = 0
    reason: str | None = None
    candidates: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, IdentityStatus):
            raise TypeError("status должен быть IdentityStatus")
        if not isinstance(self.method, DockIdentityResolutionMethod):
            raise TypeError("method должен быть DockIdentityResolutionMethod")
        if not isinstance(self.raw_name_ocr, str) or not isinstance(self.displayed_name, str):
            raise TypeError("OCR name fields должны быть string")
        if type(self.candidate_count) is not int or self.candidate_count < 0:
            raise ValueError("candidate_count должен быть неотрицательным int")
        for name, value in (
            ("best_score", self.best_score),
            ("runner_up_score", self.runner_up_score),
        ):
            if value is not None and (
                not isinstance(value, float) or not math.isfinite(value) or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} должен быть float в [0, 1] или None")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason должен быть string или None")
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(value, str) for value in self.candidates
        ):
            raise TypeError("candidates должен быть tuple строк")
        if self.status is IdentityStatus.MATCHED:
            if self.canonical_identity is None:
                raise ValueError("MATCHED требует canonical identity")
            if self.canonical_name is None or not self.canonical_name.strip():
                raise ValueError("MATCHED требует canonical name")
            if not isinstance(self.ship_form, ShipForm):
                raise ValueError("MATCHED требует ship form")
        elif any(
            value is not None
            for value in (self.canonical_identity, self.canonical_name, self.ship_form)
        ):
            raise ValueError("Только MATCHED может содержать canonical identity/name/form")


@dataclass(frozen=True, slots=True)
class DockCardIdentityObservation:
    viewport_index: int
    slot_index: int
    row: int
    column: int
    area: tuple[int, int, int, int]
    name_area: tuple[int, int, int, int]
    resolution: DockShipIdentityResolution

    def __post_init__(self) -> None:
        for name, value in (
            ("viewport_index", self.viewport_index),
            ("slot_index", self.slot_index),
            ("row", self.row),
            ("column", self.column),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} должен быть неотрицательным int")
        for name, value in (("area", self.area), ("name_area", self.name_area)):
            if (
                not isinstance(value, tuple)
                or len(value) != 4
                or any(type(item) is not int for item in value)
            ):
                raise TypeError(f"{name} должен быть tuple из четырёх int")
        if not isinstance(self.resolution, DockShipIdentityResolution):
            raise TypeError("resolution имеет неверный тип")


@dataclass(frozen=True, slots=True)
class DockViewportIdentityScan:
    viewport_index: int
    scroll_position: float
    card_scan: DockViewportCardScan
    observations: tuple[DockCardIdentityObservation, ...]

    def __post_init__(self) -> None:
        if type(self.viewport_index) is not int or self.viewport_index < 0:
            raise ValueError("viewport_index должен быть неотрицательным int")
        if not isinstance(self.scroll_position, float) or not math.isfinite(self.scroll_position):
            raise ValueError("scroll_position должен быть конечным float")
        if not isinstance(self.card_scan, DockViewportCardScan):
            raise TypeError("card_scan имеет неверный тип")
        if self.card_scan.viewport_index != self.viewport_index:
            raise ValueError("card_scan относится к другому viewport")
        if not isinstance(self.observations, tuple) or not all(
            isinstance(value, DockCardIdentityObservation) for value in self.observations
        ):
            raise TypeError("observations имеет неверный тип")

    @property
    def matched_count(self) -> int:
        return sum(
            item.resolution.status is IdentityStatus.MATCHED for item in self.observations
        )

    @property
    def ambiguous_count(self) -> int:
        return sum(
            item.resolution.status is IdentityStatus.AMBIGUOUS for item in self.observations
        )

    @property
    def unresolved_count(self) -> int:
        return sum(
            item.resolution.status is IdentityStatus.UNRESOLVED for item in self.observations
        )


@dataclass(frozen=True, slots=True)
class DockIdentityScanResult:
    prerequisite: DockPrerequisiteEvidence
    traversal: DockTraversalResult
    viewports: tuple[DockViewportIdentityScan, ...]
    catalog_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.prerequisite, DockPrerequisiteEvidence):
            raise TypeError("prerequisite должен быть DockPrerequisiteEvidence")
        if not isinstance(self.traversal, DockTraversalResult):
            raise TypeError("traversal должен быть DockTraversalResult")
        if not isinstance(self.viewports, tuple) or not all(
            isinstance(value, DockViewportIdentityScan) for value in self.viewports
        ):
            raise TypeError("viewports имеет неверный тип")
        if len(self.viewports) != self.traversal.visited_viewports:
            raise ValueError("Число identity viewports не совпадает с traversal")
        if not re.fullmatch(r"[0-9a-f]{64}", self.catalog_fingerprint):
            raise ValueError("catalog_fingerprint должен быть SHA-256")


class DockIdentityError(RuntimeError):
    """Base fail-closed error for Stage 4 identity scanning."""


class DockIdentityInputError(DockIdentityError):
    """Viewport and Stage 3 card evidence do not describe one stable frame."""


class DockIdentityOcrError(DockIdentityError):
    """The OCR pipeline failed operationally, rather than recognizing blank text."""


class DockIdentityIncompleteError(DockIdentityError):
    """Stage 3 UNKNOWN presence prevents a complete identity pass."""


def clean_displayed_ship_name(raw: str) -> str:
    if not isinstance(raw, str):
        raise TypeError("raw OCR value должен быть string")
    return " ".join(unicodedata.normalize("NFKC", raw).strip().split())


class DockShipIdentityResolver:
    """Conservative exact, explicit-prefix, then threshold+margin resolver."""

    MIN_TRUNCATED_PREFIX_LENGTH = 6
    MIN_UNMARKED_RETROFIT_PREFIX_LENGTH = 5
    FUZZY_MIN_SCORE = 0.86
    FUZZY_MIN_MARGIN = 0.08
    _RETROFIT_SUFFIX = "retrofit"
    _TRUNCATION_RE = re.compile(r"(?:\.{2,3}|…)+$")
    _FULL_RETROFIT_RE = re.compile(r"^(?P<base>.+?)\s+\(retrofit\)$", re.IGNORECASE)
    _PARTIAL_RETROFIT_RE = re.compile(r"^(?P<base>.+?)\s+\((?P<suffix>[^)]*)$")

    def __init__(self, catalog: DockIdentityCatalog) -> None:
        if not isinstance(catalog, DockIdentityCatalog):
            raise TypeError("catalog должен быть DockIdentityCatalog")
        self.catalog = catalog

    @staticmethod
    def _candidate_ids(candidates: Sequence[DockCanonicalShip]) -> tuple[str, ...]:
        return tuple(sorted({candidate.canonical_id for candidate in candidates}))

    def _decision(
        self,
        *,
        raw: str,
        displayed: str,
        status: IdentityStatus,
        method: DockIdentityResolutionMethod,
        candidates: Sequence[DockCanonicalShip] = (),
        match: DockCanonicalShip | None = None,
        ship_form: ShipForm = ShipForm.BASE,
        best_score: float | None = None,
        runner_up_score: float | None = None,
        reason: str | None = None,
    ) -> DockShipIdentityResolution:
        candidate_ids = self._candidate_ids(candidates)
        return DockShipIdentityResolution(
            status=status,
            method=method,
            raw_name_ocr=raw,
            displayed_name=displayed,
            canonical_identity=match.identity if match is not None else None,
            canonical_name=match.canonical_name if match is not None else None,
            ship_form=ship_form if match is not None else None,
            best_score=best_score,
            runner_up_score=runner_up_score,
            candidate_count=len(candidate_ids),
            reason=reason,
            candidates=candidate_ids,
        )

    def resolve(self, raw_name_ocr: str) -> DockShipIdentityResolution:
        if not isinstance(raw_name_ocr, str):
            raise TypeError("raw_name_ocr должен быть string")
        displayed = clean_displayed_ship_name(raw_name_ocr)
        normalized = normalize_ship_name(displayed)
        if not normalized:
            return self._decision(
                raw=raw_name_ocr,
                displayed=displayed,
                status=IdentityStatus.UNRESOLVED,
                method=DockIdentityResolutionMethod.NONE,
                reason="blank_ocr",
            )

        retrofit = self._resolve_retrofit_display_suffix(
            raw=raw_name_ocr,
            displayed=displayed,
        )
        if retrofit is not None:
            return retrofit

        exact = self.catalog.candidates_for_exact_name(normalized)
        if len(exact) == 1:
            return self._decision(
                raw=raw_name_ocr,
                displayed=displayed,
                status=IdentityStatus.MATCHED,
                method=DockIdentityResolutionMethod.EXACT,
                candidates=exact,
                match=exact[0],
                best_score=1.0,
                runner_up_score=self._runner_up_score(normalized, exact[0].canonical_id),
            )
        if len(exact) > 1:
            return self._decision(
                raw=raw_name_ocr,
                displayed=displayed,
                status=IdentityStatus.AMBIGUOUS,
                method=DockIdentityResolutionMethod.EXACT,
                candidates=exact,
                best_score=1.0,
                runner_up_score=1.0,
                reason="normalized_exact_collision",
            )

        # Незакрытая parenthetical-строка не должна уходить в общий fuzzy path:
        # без доверенного Retrofit evidence это недостаточно данных для identity match.
        if self._PARTIAL_RETROFIT_RE.fullmatch(displayed) is not None:
            return self._decision(
                raw=raw_name_ocr,
                displayed=displayed,
                status=IdentityStatus.UNRESOLVED,
                method=DockIdentityResolutionMethod.TRUNCATED_PREFIX,
                reason="untrusted_parenthetical_suffix",
            )

        truncation = self._TRUNCATION_RE.search(displayed)
        if truncation is not None:
            prefix = normalize_ship_name(displayed[: truncation.start()])
            if len(prefix) < self.MIN_TRUNCATED_PREFIX_LENGTH:
                return self._decision(
                    raw=raw_name_ocr,
                    displayed=displayed,
                    status=IdentityStatus.UNRESOLVED,
                    method=DockIdentityResolutionMethod.TRUNCATED_PREFIX,
                    reason="truncated_prefix_too_short",
                )
            prefix_candidates = self._prefix_candidates(prefix)
            if len(prefix_candidates) == 1:
                return self._decision(
                    raw=raw_name_ocr,
                    displayed=displayed,
                    status=IdentityStatus.MATCHED,
                    method=DockIdentityResolutionMethod.TRUNCATED_PREFIX,
                    candidates=prefix_candidates,
                    match=prefix_candidates[0],
                    best_score=1.0,
                    runner_up_score=0.0,
                )
            if len(prefix_candidates) > 1:
                return self._decision(
                    raw=raw_name_ocr,
                    displayed=displayed,
                    status=IdentityStatus.AMBIGUOUS,
                    method=DockIdentityResolutionMethod.TRUNCATED_PREFIX,
                    candidates=prefix_candidates,
                    best_score=1.0,
                    runner_up_score=1.0,
                    reason="ambiguous_truncated_prefix",
                )
            return self._decision(
                raw=raw_name_ocr,
                displayed=displayed,
                status=IdentityStatus.UNRESOLVED,
                method=DockIdentityResolutionMethod.TRUNCATED_PREFIX,
                reason="unknown_truncated_prefix",
            )

        ranked = self._rank_fuzzy(normalized)
        if not ranked:
            return self._decision(
                raw=raw_name_ocr,
                displayed=displayed,
                status=IdentityStatus.UNRESOLVED,
                method=DockIdentityResolutionMethod.NONE,
                reason="empty_catalog",
            )
        best_score, best = ranked[0]
        runner_up_score = ranked[1][0] if len(ranked) > 1 else 0.0
        ranked_candidates = tuple(candidate for _score, candidate in ranked)
        if best_score < self.FUZZY_MIN_SCORE:
            return self._decision(
                raw=raw_name_ocr,
                displayed=displayed,
                status=IdentityStatus.UNRESOLVED,
                method=DockIdentityResolutionMethod.FUZZY,
                candidates=ranked_candidates,
                best_score=best_score,
                runner_up_score=runner_up_score,
                reason="fuzzy_below_threshold",
            )
        if best_score - runner_up_score < self.FUZZY_MIN_MARGIN:
            return self._decision(
                raw=raw_name_ocr,
                displayed=displayed,
                status=IdentityStatus.AMBIGUOUS,
                method=DockIdentityResolutionMethod.FUZZY,
                candidates=ranked_candidates,
                best_score=best_score,
                runner_up_score=runner_up_score,
                reason="fuzzy_margin_too_small",
            )
        return self._decision(
            raw=raw_name_ocr,
            displayed=displayed,
            status=IdentityStatus.MATCHED,
            method=DockIdentityResolutionMethod.FUZZY,
            candidates=ranked_candidates,
            match=best,
            best_score=best_score,
            runner_up_score=runner_up_score,
        )

    @classmethod
    def _is_unmarked_partial_retrofit_suffix(cls, suffix: str) -> bool:
        normalized = normalize_ship_name(suffix)
        if len(normalized) < cls.MIN_UNMARKED_RETROFIT_PREFIX_LENGTH:
            return False
        if cls._RETROFIT_SUFFIX.startswith(normalized):
            return True

        prefix_length = 0
        for observed, expected in zip(normalized, cls._RETROFIT_SUFFIX):
            if observed != expected:
                break
            prefix_length += 1

        # Реальный Formation OCR может заменить первый обрезанный символ после
        # уже надёжного `retro` на одну цифру; шире этот noise contract не открываем.
        return (
            prefix_length >= cls.MIN_UNMARKED_RETROFIT_PREFIX_LENGTH
            and len(normalized) == prefix_length + 1
            and normalized[-1].isdigit()
        )

    def _resolve_retrofit_display_suffix(
        self,
        *,
        raw: str,
        displayed: str,
    ) -> DockShipIdentityResolution | None:
        truncation = self._TRUNCATION_RE.search(displayed)
        method = DockIdentityResolutionMethod.EXACT
        match = self._FULL_RETROFIT_RE.fullmatch(displayed)
        if truncation is not None:
            method = DockIdentityResolutionMethod.TRUNCATED_PREFIX
            match = self._PARTIAL_RETROFIT_RE.fullmatch(displayed[: truncation.start()])
            if match is None:
                return None
            suffix = normalize_ship_name(match.group("suffix"))
            if not suffix or not self._RETROFIT_SUFFIX.startswith(suffix):
                return None
        elif match is None:
            match = self._PARTIAL_RETROFIT_RE.fullmatch(displayed)
            if match is None or not self._is_unmarked_partial_retrofit_suffix(
                match.group("suffix")
            ):
                return None
            method = DockIdentityResolutionMethod.TRUNCATED_PREFIX
        if match is None:
            return None

        base = normalize_ship_name(match.group("base"))
        candidates = self.catalog.candidates_for_exact_name(base)
        if len(candidates) == 1:
            return self._decision(
                raw=raw,
                displayed=displayed,
                status=IdentityStatus.MATCHED,
                method=method,
                candidates=candidates,
                match=candidates[0],
                ship_form=ShipForm.RETROFIT,
                best_score=1.0,
                runner_up_score=self._runner_up_score(
                    base,
                    candidates[0].canonical_id,
                ),
                reason="retrofit_display_suffix",
            )
        if len(candidates) > 1:
            return self._decision(
                raw=raw,
                displayed=displayed,
                status=IdentityStatus.AMBIGUOUS,
                method=method,
                candidates=candidates,
                best_score=1.0,
                runner_up_score=1.0,
                reason="ambiguous_retrofit_base",
            )
        return None

    def _prefix_candidates(self, prefix: str) -> tuple[DockCanonicalShip, ...]:
        candidates: dict[str, DockCanonicalShip] = {}
        for name, records in self.catalog.by_normalized_name.items():
            if name.startswith(prefix):
                for record in records:
                    candidates[record.canonical_id] = record
        return tuple(candidates[key] for key in sorted(candidates))

    def _rank_fuzzy(self, normalized: str) -> tuple[tuple[float, DockCanonicalShip], ...]:
        scores: dict[str, tuple[float, DockCanonicalShip]] = {}
        for candidate_name, records in self.catalog.by_normalized_name.items():
            score = SequenceMatcher(
                None,
                normalized,
                candidate_name,
                autojunk=False,
            ).ratio()
            for record in records:
                previous = scores.get(record.canonical_id)
                if previous is None or score > previous[0]:
                    scores[record.canonical_id] = (score, record)
        return tuple(
            sorted(
                scores.values(),
                key=lambda item: (-item[0], item[1].canonical_id),
            )
        )

    def _runner_up_score(self, normalized: str, matched_id: str) -> float:
        return next(
            (
                score
                for score, candidate in self._rank_fuzzy(normalized)
                if candidate.canonical_id != matched_id
            ),
            0.0,
        )


class _DockNameOcr(Protocol):
    def read_names(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[str, ...]: ...


class _DockNameOcrModel(Ocr):
    PINK_LETTER = (255, 170, 206)
    PINK_THRESHOLD = 108
    TEXT_ROWS = (4, 23)
    TEXT_LEFT = 4

    @staticmethod
    def _remove_edge_noise(image: np.ndarray) -> np.ndarray:
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            (image < 120).astype(np.uint8),
            connectivity=8,
        )
        for label in range(1, count):
            x, _, width, _, _ = stats[label]
            if x == 0 or x + width == image.shape[1]:
                image[labels == label] = 255
        return image

    def pre_process(self, image: np.ndarray) -> np.ndarray:
        white = extract_letters(image, letter=self.letter, threshold=self.threshold)
        pink = extract_letters(
            image,
            letter=self.PINK_LETTER,
            threshold=self.PINK_THRESHOLD,
        )
        merged = cv2.min(white, pink)
        merged = merged[self.TEXT_ROWS[0] : self.TEXT_ROWS[1], self.TEXT_LEFT :]
        return self._remove_edge_noise(merged)


class DockShipNameOcr:
    """PP-OCRv6 adapter for white and oath-pink text on supplied RGB frames."""

    def read_names(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[str, ...]:
        areas = tuple(areas)
        if not areas:
            return ()
        model = _DockNameOcrModel(
            list(areas),
            lang="azur_lane",
            threshold=128,
            name="DOCK_SHIP_NAME",
        )
        result = model.ocr(frame)
        values = result if isinstance(result, list) else [result]
        if len(values) != len(areas) or any(not isinstance(value, str) for value in values):
            raise DockIdentityOcrError(
                "OCR вернул результат, не соответствующий числу PRESENT slots."
            )
        return tuple(values)


class DockIdentityScanner:
    """Resolve identities for PRESENT Stage 3 slots on one immutable frame."""

    NAME_LEFT = -10
    NAME_TOP = 160
    NAME_RIGHT = 142
    NAME_BOTTOM = 190

    def __init__(
        self,
        catalog: DockIdentityCatalog,
        *,
        name_ocr: _DockNameOcr | None = None,
        resolver: DockShipIdentityResolver | None = None,
    ) -> None:
        self.catalog = catalog
        self.name_ocr = DockShipNameOcr() if name_ocr is None else name_ocr
        self.resolver = DockShipIdentityResolver(catalog) if resolver is None else resolver

    def name_area(
        self,
        slot_area: tuple[int, int, int, int],
        frame_shape: tuple[int, ...],
    ) -> tuple[int, int, int, int]:
        if len(frame_shape) < 2:
            raise DockIdentityInputError("Frame shape не содержит height/width.")
        height, width = frame_shape[:2]
        x1, y1, _x2, _y2 = slot_area
        area = (
            x1 + self.NAME_LEFT,
            y1 + self.NAME_TOP,
            x1 + self.NAME_RIGHT,
            y1 + self.NAME_BOTTOM,
        )
        left, top, right, bottom = area
        if left < 0 or top < 0 or right > width or bottom > height or right <= left or bottom <= top:
            raise DockIdentityInputError(
                f"Slot-relative name ROI выходит за frame: slot={slot_area}, roi={area}."
            )
        return area

    def _validate_inputs(
        self,
        viewport: DockTraversalViewport,
        card_scan: DockViewportCardScan,
    ) -> None:
        if not isinstance(viewport, DockTraversalViewport):
            raise TypeError("viewport должен быть DockTraversalViewport")
        if not isinstance(card_scan, DockViewportCardScan):
            raise TypeError("card_scan должен быть DockViewportCardScan")
        if viewport.index != card_scan.viewport_index:
            raise DockIdentityInputError("Viewport/card scan index mismatch.")
        if not math.isclose(
            viewport.scroll_position,
            card_scan.scroll_position,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise DockIdentityInputError("Viewport/card scan scroll-position mismatch.")
        height, width = viewport.frame.shape[:2]
        for slot in card_scan.slots:
            x1, y1, x2, y2 = slot.area
            if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
                raise DockIdentityInputError(
                    f"Stage 3 slot geometry выходит за текущий frame: {slot.area!r}."
                )

    def scan_viewport(
        self,
        viewport: DockTraversalViewport,
        card_scan: DockViewportCardScan,
    ) -> DockViewportIdentityScan:
        self._validate_inputs(viewport, card_scan)
        if card_scan.unknown_count:
            raise DockIdentityIncompleteError(
                "Stage 3 UNKNOWN не позволяет объявить identity pass полным: "
                f"viewport={viewport.index}, unknown={card_scan.unknown_count}."
            )
        present = tuple(
            slot for slot in card_scan.slots if slot.presence is DockCardPresence.PRESENT
        )
        areas = tuple(self.name_area(slot.area, viewport.frame.shape) for slot in present)
        ocr_frame = viewport.frame.copy()
        try:
            raw_names = self.name_ocr.read_names(ocr_frame, areas)
        except DockIdentityOcrError:
            raise
        except Exception as exc:
            raise DockIdentityOcrError(
                f"Операционный сбой Dock name OCR: {type(exc).__name__}: {exc}"
            ) from exc
        if len(raw_names) != len(present):
            raise DockIdentityOcrError(
                "Число OCR results не совпало с числом PRESENT slots."
            )
        observations = tuple(
            DockCardIdentityObservation(
                viewport_index=viewport.index,
                slot_index=slot.slot_index,
                row=slot.row,
                column=slot.column,
                area=slot.area,
                name_area=name_area,
                resolution=self.resolver.resolve(raw),
            )
            for slot, name_area, raw in zip(present, areas, raw_names)
        )
        result = DockViewportIdentityScan(
            viewport_index=viewport.index,
            scroll_position=float(viewport.scroll_position),
            card_scan=card_scan,
            observations=observations,
        )
        logger.info(
            "[Инвентарь дока] identity окно=%s present=%s matched=%s "
            "ambiguous=%s unresolved=%s",
            result.viewport_index,
            len(result.observations),
            result.matched_count,
            result.ambiguous_count,
            result.unresolved_count,
        )
        return result


class DockIdentityCollector:
    def __init__(
        self,
        identity_scanner: DockIdentityScanner,
        *,
        card_scanner: DockCardGridScanner | None = None,
    ) -> None:
        self.identity_scanner = identity_scanner
        self.card_scanner = DockCardGridScanner() if card_scanner is None else card_scanner
        self._viewports: list[DockViewportIdentityScan] = []

    def __call__(self, viewport: DockTraversalViewport) -> None:
        card_scan = self.card_scanner.scan_viewport(viewport)
        self._viewports.append(self.identity_scanner.scan_viewport(viewport, card_scan))

    @property
    def viewports(self) -> tuple[DockViewportIdentityScan, ...]:
        return tuple(self._viewports)


def scan_dock_identities(
    navigator: DockInventoryNavigator,
    *,
    catalog: DockIdentityCatalog | None = None,
    identity_scanner: DockIdentityScanner | None = None,
    card_scanner: DockCardGridScanner | None = None,
    run_stage2_kwargs: dict[str, object] | None = None,
) -> DockIdentityScanResult:
    """Reuse Stage 2 navigation/traversal and Stage 3 card geometry unchanged."""

    if not isinstance(navigator, DockInventoryNavigator):
        raise TypeError("navigator должен быть DockInventoryNavigator")
    if catalog is None:
        catalog = load_dock_identity_catalog()
    if identity_scanner is None:
        identity_scanner = DockIdentityScanner(catalog)
    elif identity_scanner.catalog.fingerprint != catalog.fingerprint:
        raise ValueError("identity_scanner и catalog имеют разные fingerprints")
    collector = DockIdentityCollector(identity_scanner, card_scanner=card_scanner)
    stage2 = navigator.run_stage2(
        collector,
        **({} if run_stage2_kwargs is None else run_stage2_kwargs),
    )
    return DockIdentityScanResult(
        prerequisite=stage2.prerequisite,
        traversal=stage2.traversal,
        viewports=collector.viewports,
        catalog_fingerprint=catalog.fingerprint,
    )
