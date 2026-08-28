"""Fail-closed semantic evidence detector для low-morale warning popup."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
_MOOD_RE = re.compile(r"\b(?:mood|morale|emotion|spirit)\b", re.IGNORECASE)
_LOW_RE = re.compile(
    r"\b(?:low|lowest|red|reduced|decrease|decreased|decreasing|decline|declined|loss|lost|fall|fallen)\b",
    re.IGNORECASE,
)
_AFFINITY_RE = re.compile(
    r"\b(?:affinity|relationship|friendship|bond)\b.*"
    r"\b(?:reduce|reduced|decrease|decreased|lose|lost|losing|loss|drop|drops|lower)\b"
    r"|\b(?:reduce|reduced|decrease|decreased|lose|lost|losing|loss|drop|drops|lower)\b.*"
    r"\b(?:affinity|relationship|friendship|bond)\b",
    re.IGNORECASE,
)
_FORCED_ATTACK_RE = re.compile(
    r"\b(?:force|forced|forcing|continue)\b.*"
    r"\b(?:attack|attacking|battle|fight)\b"
    r"|\b(?:attack|attacking|battle|fight)\b.*"
    r"\b(?:force|forced|forcing|continue)\b",
    re.IGNORECASE,
)


def normalize_warning_text(text: str) -> str:
    """Нормализовать OCR/hierarchy text без привязки к имени корабля или флота."""

    if not isinstance(text, str):
        raise TypeError("warning text должен быть строкой")
    return _WORD_RE.sub(" ", text.casefold()).strip()


@dataclass(frozen=True, slots=True)
class LowMoraleWarningEvidence:
    """Семантические признаки, достаточные для распознавания warning."""

    normalized_text: str
    mood_term: bool
    low_term: bool
    consequence_term: bool
    forced_attack_term: bool

    @property
    def proven(self) -> bool:
        return (
            self.mood_term
            and self.low_term
            and (self.consequence_term or self.forced_attack_term)
        )


class LowMoraleWarningDetector:
    """Распознаёт warning только при наличии нескольких независимых признаков."""

    def detect(self, text: str) -> LowMoraleWarningEvidence | None:
        normalized = normalize_warning_text(text)
        if not normalized:
            return None
        evidence = LowMoraleWarningEvidence(
            normalized_text=normalized,
            mood_term=bool(_MOOD_RE.search(normalized)),
            low_term=bool(_LOW_RE.search(normalized)),
            consequence_term=bool(_AFFINITY_RE.search(normalized)),
            forced_attack_term=bool(_FORCED_ATTACK_RE.search(normalized)),
        )
        return evidence if evidence.proven else None

    def detect_many(self, texts: Iterable[str]) -> LowMoraleWarningEvidence | None:
        values = tuple(text for text in texts if isinstance(text, str))
        if not values:
            return None
        combined = " ".join(values)
        return self.detect(combined)


__all__ = (
    "LowMoraleWarningDetector",
    "LowMoraleWarningEvidence",
    "normalize_warning_text",
)
