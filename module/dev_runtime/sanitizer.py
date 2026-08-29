"""Общий очиститель диагностических текстов только для dev-контура."""

from __future__ import annotations

import re

MAX_SANITIZED_TEXT = 4096

_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_:/])(?:[A-Za-z]:[\\/]|\\\\|/(?!/))[^\s,;)\]}]+"
)
_FILE_URI_PATH = re.compile(r"(?i)\bfile:///[^\s,;)\]}]+")
_URL_USERINFO = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE
)
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:authorization|access[_-]?token|x[_-]?api[_-]?key|api[_-]?key|"
    r"token|password|passwd|secret|cookie|session(?:[_-]?id)?|private[_-]?key|credential)=)[^&#\s]+"
)
_CREDENTIAL_NAME = (
    r"authorization|access[_-]?token|x[_-]?api[_-]?key|api[_-]?key|"
    r"token|password|passwd|secret|cookie|session(?:[_-]?id)?|private[_-]?key|credential"
)
_CREDENTIAL_BOUNDARY = r"(?<![A-Za-z0-9])"
_BEARER = re.compile(r"(?i)(\bbearer\s+)[^\s,;\]}]+")
_SENSITIVE_QUOTED_ASSIGNMENT = re.compile(
    rf"""(?ix)
    (?P<key_quote>["'])
    (?P<key>{_CREDENTIAL_NAME})
    (?P=key_quote)
    (?P<separator>\s*[:=]\s*)
    (?P<value_quote>["'])
    (?P<bearer>bearer\s+)?
    (?P<value>.*?)
    (?P=value_quote)
    """
)
_SENSITIVE_QUOTED_VALUE = re.compile(
    rf"""(?ix)
    {_CREDENTIAL_BOUNDARY}(?P<key>{_CREDENTIAL_NAME})
    (?P<separator>\s*[:=]\s*)
    (?P<value_quote>["'])
    (?P<bearer>bearer\s+)?
    (?P<value>.*?)
    (?P=value_quote)
    """
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?i){_CREDENTIAL_BOUNDARY}(?P<key>{_CREDENTIAL_NAME})"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<bearer>bearer\s+)?"
    r"(?P<value>[^\s,;}\]]+)"
)


def _redact_quoted_assignment(match: re.Match[str]) -> str:
    return (
        f"{match.group('key_quote')}{match.group('key')}{match.group('key_quote')}"
        f"{match.group('separator')}{match.group('value_quote')}"
        f"{match.group('bearer') or ''}***{match.group('value_quote')}"
    )


def _redact_quoted_value(match: re.Match[str]) -> str:
    return (
        f"{match.group('key')}{match.group('separator')}"
        f"{match.group('value_quote')}{match.group('bearer') or ''}***"
        f"{match.group('value_quote')}"
    )


def _redact_assignment(match: re.Match[str]) -> str:
    return (
        f"{match.group('key')}{match.group('separator')}"
        f"{match.group('bearer') or ''}***"
    )


def redact_text(value: str, *, max_length: int = MAX_SANITIZED_TEXT) -> str:
    """Скрыть учётные данные и локальные пути и ограничить размер текста."""

    value = _URL_USERINFO.sub(r"\g<scheme>***@", value)
    value = _SENSITIVE_QUERY.sub(r"\1***", value)
    value = _BEARER.sub(r"\1***", value)
    value = _SENSITIVE_QUOTED_ASSIGNMENT.sub(_redact_quoted_assignment, value)
    value = _SENSITIVE_QUOTED_VALUE.sub(_redact_quoted_value, value)
    value = _SENSITIVE_ASSIGNMENT.sub(_redact_assignment, value)
    value = _FILE_URI_PATH.sub("file:///[путь скрыт]", value)
    value = _ABSOLUTE_PATH.sub("[путь скрыт]", value)
    if len(value) > max_length:
        return value[:max_length] + "…"
    return value


__all__ = ["MAX_SANITIZED_TEXT", "redact_text"]
