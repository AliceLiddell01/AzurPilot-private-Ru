"""Fail-open граница telemetry для notification use cases."""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager


@contextmanager
def safe_telemetry_span(
    telemetry: object | None,
    name: str,
    *,
    attributes: Mapping[str, object] | None = None,
) -> Iterator[None]:
    """Не позволяет telemetry нарушить durable notification operation."""
    try:
        method = getattr(telemetry, "span", None)
    except Exception:  # noqa: BLE001 - telemetry остаётся fail-open.
        method = None
    if not callable(method):
        yield
        return
    try:
        context = method(name, attributes=dict(attributes or {}))
        enter = getattr(context, "__enter__", None)
        exit = getattr(context, "__exit__", None)
    except Exception:  # noqa: BLE001 - telemetry остаётся fail-open.
        yield
        return
    if not callable(enter) or not callable(exit):
        yield
        return
    try:
        enter()
    except Exception:  # noqa: BLE001 - telemetry остаётся fail-open.
        yield
        return
    try:
        yield
    except BaseException:
        try:
            exit(*sys.exc_info())
        except Exception:  # noqa: BLE001 - telemetry остаётся fail-open.
            pass
        raise
    else:
        try:
            exit(None, None, None)
        except Exception:  # noqa: BLE001 - telemetry остаётся fail-open.
            pass


__all__ = ["safe_telemetry_span"]
