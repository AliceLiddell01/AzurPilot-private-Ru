"""Правила распознавания служебных Git refs без внешних зависимостей."""

from __future__ import annotations

import re

_AD_HOC_REMOTE_REF_PATTERNS = (
    re.compile(
        r"(?i)^codex/(?:base|scratch|tmp|temporary|transport|helper|aux)(?:[-/]|$)"
    ),
    re.compile(
        r"(?i)^(?:temporary|scratch|tmp|transport|helper|aux)(?:/|$)"
    ),
)
_WORKFLOW_GUARD_REF_PATTERN = re.compile(
    r"(?i)(?:^|/)(?:temporary|scratch|tmp|transport|helper|aux)(?:/|$)"
)


def is_ad_hoc_remote_ref(value: str) -> bool:
    """Распознать reserved refs, которые не являются parent/feature topology."""

    return any(pattern.search(value) for pattern in _AD_HOC_REMOTE_REF_PATTERNS)


def is_workflow_guard_ref(value: str) -> bool:
    """Распознать refs, запрещённые только для прямых workflow-команд."""

    return is_ad_hoc_remote_ref(value) or bool(_WORKFLOW_GUARD_REF_PATTERN.search(value))
