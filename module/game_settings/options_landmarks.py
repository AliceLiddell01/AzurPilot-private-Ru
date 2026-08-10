"""Semantic navigation landmarks for the current EN Settings -> Options page.

Landmarks describe the page structure, not the bot's required-setting registry.
They are used only to reason about monotonic downward traversal when the UI has
no measurable scrollbar and image phase correlation becomes ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from module.game_settings.options_detector import (
    OcrTextBox,
    _FRAME_OCR_CACHE,
    _group_text,
    _label_similarity,
    _normalize,
    _same_line_groups,
)


_SEMANTIC_MATCH_THRESHOLD = 0.80
_SEMANTIC_MARQUEE_MIN_CHARS = 10
_SEMANTIC_MARQUEE_MIN_COVERAGE = 0.60
_SEMANTIC_SPAN_MAX_BOXES = 4
_EXACT_VISIBLE_ALIASES = frozenset(("compatibilitymode",))


@dataclass(frozen=True, slots=True)
class OptionsSemanticLandmark:
    key: str
    rank: int
    aliases: tuple[str, ...]
    terminal: bool = False


@dataclass(frozen=True, slots=True)
class OptionsSemanticObservation:
    landmark: OptionsSemanticLandmark
    score: float
    text: str

    @property
    def key(self) -> str:
        return self.landmark.key

    @property
    def rank(self) -> int:
        return self.landmark.rank

    @property
    def terminal(self) -> bool:
        return self.landmark.terminal


OPTIONS_SEMANTIC_LANDMARKS = (
    OptionsSemanticLandmark(
        key="frame_rate_region",
        rank=10,
        aliases=("Frame Rate Settings", "Frame Rate"),
    ),
    OptionsSemanticLandmark(
        key="story_autoplay_region",
        rank=20,
        aliases=("Story Autoplay", "Story auto-play"),
    ),
    OptionsSemanticLandmark(
        key="idle_screen_region",
        rank=30,
        aliases=("Enable Idle Screen", "Enable Idle Mode"),
    ),
    OptionsSemanticLandmark(
        key="custom_ship_names_region",
        rank=40,
        aliases=(
            "Custom Ship Names",
            "Change Oathed Ship Names",
        ),
    ),
    OptionsSemanticLandmark(
        key="fixed_l2d_region",
        rank=45,
        aliases=("Fixed L2D Settings",),
    ),
    OptionsSemanticLandmark(
        key="rendering_compatibility_terminal",
        rank=50,
        aliases=(
            "Rendering Compatibility",
            "Rendering Compatibility Mode",
            "Compatibility Mode",
        ),
        terminal=True,
    ),
)


def _semantic_similarity(text: str, alias: str) -> float:
    """Match full labels and long marquee-visible label fragments.

    Long Options labels move horizontally inside a clipped field. OCR can
    therefore observe only a prefix or suffix of the real label while the row
    itself remains perfectly identifiable. Short fragments stay rejected so
    generic words such as ``Off``, ``On`` or ``Mode`` cannot become landmarks.
    ``Compatibility Mode`` is a special live terminal suffix and must be
    visible in full; accepting ``Compatibility`` alone would be too generic.
    """

    left = _normalize(text)
    right = _normalize(alias)
    if not left or not right:
        return 0.0

    if right in _EXACT_VISIBLE_ALIASES and right not in left:
        return 0.0

    score = _label_similarity(text, alias)
    if left in right:
        coverage = len(left) / len(right)
        if (
            len(left) >= _SEMANTIC_MARQUEE_MIN_CHARS
            and coverage >= _SEMANTIC_MARQUEE_MIN_COVERAGE
        ):
            score = max(score, 0.80 + min(0.19, coverage * 0.19))
    return score


def _semantic_candidate_texts(group: tuple[OcrTextBox, ...]) -> tuple[str, ...]:
    """Return bounded contiguous OCR spans from one visual row.

    The full row remains a candidate, but individual/short contiguous spans
    let a clipped moving label match independently from its adjacent Off/On
    controls. The bound prevents combinatorial growth on text-heavy rows.
    """

    candidates: list[str] = [_group_text(group)]
    limit = min(len(group), _SEMANTIC_SPAN_MAX_BOXES)
    for start in range(len(group)):
        stop_limit = min(len(group), start + limit)
        for stop in range(start + 1, stop_limit + 1):
            candidates.append(_group_text(group[start:stop]))
    return tuple(dict.fromkeys(candidates))


def detect_options_semantic_landmark(
    image: np.ndarray,
    *,
    detections: tuple[OcrTextBox, ...] | None = None,
) -> OptionsSemanticObservation | None:
    """Return the deepest confidently visible structural landmark.

    Choosing the deepest visible landmark makes overlapping viewports useful:
    an older row may still remain on screen while a later structural marker has
    already entered the viewport. The result is intentionally independent from
    ``GAME_SETTINGS_OPTIONS_REGISTRY``.
    """

    groups = (
        _FRAME_OCR_CACHE.get_groups(image)
        if detections is None
        else _same_line_groups(detections)
    )
    candidates: list[OptionsSemanticObservation] = []

    for landmark in OPTIONS_SEMANTIC_LANDMARKS:
        best_score = 0.0
        best_text = ""
        for group in groups:
            for text in _semantic_candidate_texts(group):
                score = max(
                    _semantic_similarity(text, alias)
                    for alias in landmark.aliases
                )
                if score > best_score:
                    best_score = score
                    best_text = text
        if best_score >= _SEMANTIC_MATCH_THRESHOLD:
            candidates.append(
                OptionsSemanticObservation(
                    landmark=landmark,
                    score=best_score,
                    text=best_text,
                )
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (item.rank, item.score),
        reverse=True,
    )
    return candidates[0]
