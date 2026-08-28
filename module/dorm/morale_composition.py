"""Composition adapter для campaign morale bootstrap.

Application boundary не импортирует PostgreSQL/persistence напрямую. UI/Dorm
слой разрешает production runtime context только в момент фактического bootstrap.
"""

from __future__ import annotations


def build_campaign_morale_context(*, require_ready: bool = False):
    from module.persistence.runtime import build_runtime_morale_context

    return build_runtime_morale_context(require_ready=require_ready)


__all__ = ("build_campaign_morale_context",)
