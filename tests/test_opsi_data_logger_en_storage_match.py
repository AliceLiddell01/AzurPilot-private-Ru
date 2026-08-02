"""Regression tests for EN Data Logger matching in Operation Siren Storage."""

from types import SimpleNamespace

import module.config.server as server
from module.config.opsi_data_logger import (
    DATA_LOGGER_STORAGE_EN_FALLBACK_SIMILARITY,
    DATA_LOGGER_STORAGE_STRICT_SIMILARITY,
    _DataLoggerUnlockTemplateProxy,
)


class FakeTemplate:
    def __init__(self, fallback_points):
        self.fallback_points = iter(fallback_points)
        self.similarities = []

    def match_multi(self, _image, *, scaling, similarity, threshold, name):
        self.similarities.append(similarity)
        if similarity == DATA_LOGGER_STORAGE_STRICT_SIMILARITY:
            return []
        if similarity != DATA_LOGGER_STORAGE_EN_FALLBACK_SIMILARITY:
            raise AssertionError(f"unexpected similarity: {similarity}")
        try:
            x, y = next(self.fallback_points)
        except StopIteration:
            return []
        return [SimpleNamespace(area=(x, y, x + 40, y + 40))]


def _match(proxy):
    return proxy.match_multi(
        object(),
        scaling=1.0,
        similarity=DATA_LOGGER_STORAGE_STRICT_SIMILARITY,
        threshold=3,
        name=None,
    )


def test_en_fallback_requires_three_stable_frames(monkeypatch):
    monkeypatch.setattr(server, "server", "en")
    template = FakeTemplate([(352, 131), (353, 131), (352, 132)])
    proxy = _DataLoggerUnlockTemplateProxy(template)

    assert _match(proxy) == []
    assert _match(proxy) == []
    assert len(_match(proxy)) == 1
    assert template.similarities == [
        0.75,
        0.60,
        0.75,
        0.60,
        0.75,
        0.60,
    ]


def test_en_fallback_resets_when_position_moves(monkeypatch):
    monkeypatch.setattr(server, "server", "en")
    template = FakeTemplate(
        [(352, 131), (390, 131), (391, 132), (390, 131)]
    )
    proxy = _DataLoggerUnlockTemplateProxy(template)

    assert _match(proxy) == []
    assert _match(proxy) == []
    assert _match(proxy) == []
    assert len(_match(proxy)) == 1


def test_non_en_server_never_uses_relaxed_match(monkeypatch):
    monkeypatch.setattr(server, "server", "jp")
    template = FakeTemplate([(352, 131)])
    proxy = _DataLoggerUnlockTemplateProxy(template)

    assert _match(proxy) == []
    assert template.similarities == [DATA_LOGGER_STORAGE_STRICT_SIMILARITY]
