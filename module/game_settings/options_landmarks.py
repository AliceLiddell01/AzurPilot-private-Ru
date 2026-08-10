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
    _same_line_groups,
)


_SEMANTIC_MATCH_THRESHOLD = 0.80


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
        aliases=("Custom Ship Names",),
    ),
    OptionsSemanticLandmark(
        key="rendering_compatibility_terminal",
        rank=50,
        aliases=(
            "Rendering Compatibility",
            "Rendering Compatibility Mode",
        ),
        terminal=True,
    ),
)


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
            text = _group_text(group)
            score = max(
                _label_similarity(text, alias)
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
