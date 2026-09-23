"""Канонический invocation contract для project-owned operator actions."""

from __future__ import annotations

from collections.abc import Sequence

from .result import ResultCode


def validate_direct_azur_invocation(
    argv: Sequence[str], *, azur_available: bool
) -> ResultCode:
    """Проверить literal operator boundary без fallback на другой launcher.

    ``uv`` остаётся допустимым runner для dependency/bootstrap/test/build
    операций. Эта функция применяется только к capabilities, для которых
    проект уже предоставляет operator command ``azur``.
    """

    if not azur_available:
        return ResultCode.TOOLING_CAPABILITY_UNAVAILABLE
    return (
        ResultCode.OK
        if argv and argv[0] == "azur"
        else ResultCode.TOOLING_INVALID_INVOCATION
    )


__all__ = ["validate_direct_azur_invocation"]
