"""Правила распознавания служебных Git refs без внешних зависимостей."""

from __future__ import annotations

import re

_AD_HOC_REMOTE_REF_PATTERNS = (
    re.compile(
        r"(?i)^codex/(?:base|scratch|tmp|temporary|transport|helper|aux)(?:[-/]|$)"
    ),
    re.compile(r"(?i)(?:^|/)(?:temporary|scratch|transport)(?:/|$)"),
)


def is_ad_hoc_remote_ref(value: str) -> bool:
    """Распознать reserved refs, которые не являются parent/feature topology."""

    return any(pattern.search(value) for pattern in _AD_HOC_REMOTE_REF_PATTERNS)
