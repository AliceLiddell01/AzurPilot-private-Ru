"""Детектор признаков предупреждения о низкой морали с безопасным отказом."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
_MOOD_RE = re.compile(r"\b(?:mood|morale|emotion|spirit)\b", re.IGNORECASE)
_LOW_MOOD_RE = re.compile(
    r"\b(?:mood|morale|emotion|spirit)\b"
    r"(?:\s+\w+){0,5}\s+"
    r"\b(?:low|lowest|red|decrease|decreased|decreasing|decline|declined|fall|fallen|falling)\b"
    r"|\b(?:low|lowest|red|decrease|decreased|decreasing|decline|declined|fall|fallen|falling)\b"
    r"(?:\s+\w+){0,5}\s+"
    r"\b(?:mood|morale|emotion|spirit)\b",
    re.IGNORECASE,
)
_AFFINITY_RE = re.compile(
    r"\b(?:affinity|relationship|friendship|bond)\b"
    r"(?:\s+\w+){0,5}\s+"
    r"\b(?:reduce|reduced|decrease|decreased|lose|lost|losing|loss|drop|drops|lower)\b"
    r"|\b(?:reduce|reduced|decrease|decreased|lose|lost|losing|loss|drop|drops|lower)\b"
    r"(?:\s+\w+){0,5}\s+"
    r"\b(?:affinity|relationship|friendship|bond)\b",
    re.IGNORECASE,
)
_FORCED_ATTACK_RE = re.compile(
    r"\b(?:force|forced|forcing|continue)\b"
    r"(?:\s+\w+){0,5}\s+"
    r"\b(?:attack|attacking|battle|fight)\b"
    r"|\b(?:attack|attacking|battle|fight)\b"
    r"(?:\s+\w+){0,5}\s+"
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
        clauses = tuple(
            normalize_warning_text(clause)
            for clause in re.split(r"[.!?]", text)
        )
        evidence = LowMoraleWarningEvidence(
            normalized_text=normalized,
            mood_term=bool(_MOOD_RE.search(normalized)),
            low_term=any(_LOW_MOOD_RE.search(clause) for clause in clauses),
            consequence_term=any(_AFFINITY_RE.search(clause) for clause in clauses),
            forced_attack_term=any(
                _FORCED_ATTACK_RE.search(clause) for clause in clauses
            ),
        )
        return evidence if evidence.proven else None

    def detect_many(self, texts: Iterable[str]) -> LowMoraleWarningEvidence | None:
        values = tuple(text for text in texts if isinstance(text, str))
        if not values:
            return None
        combined = ". ".join(values)
        return self.detect(combined)

    def detect_fragments(
        self, texts: Iterable[str]
    ) -> LowMoraleWarningEvidence | None:
        """Проверить соседние фрагменты одного popup без склейки всей страницы."""

        values = tuple(
            normalize_warning_text(text)
            for text in texts
            if isinstance(text, str) and text.strip()
        )
        if not values:
            return None

        normalized = " ".join(values)
        windows = tuple(
            " ".join(values[index : index + width])
            for width in (1, 2, 3)
            for index in range(len(values) - width + 1)
        )
        evidence = LowMoraleWarningEvidence(
            normalized_text=normalized,
            mood_term=any(_MOOD_RE.search(value) for value in values),
            low_term=any(_LOW_MOOD_RE.search(window) for window in windows),
            consequence_term=any(_AFFINITY_RE.search(window) for window in windows),
            forced_attack_term=any(
                _FORCED_ATTACK_RE.search(window) for window in windows
            ),
        )
        return evidence if evidence.proven else None


__all__ = (
    "LowMoraleWarningDetector",
    "LowMoraleWarningEvidence",
    "normalize_warning_text",
)
