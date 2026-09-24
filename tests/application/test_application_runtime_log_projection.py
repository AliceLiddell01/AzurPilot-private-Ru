from __future__ import annotations

import logging

import pytest

from module.application import runtime_log_projection as projection
from module.application.runtime_log_projection import (
    RuntimeLogProjectionHandler,
    read_runtime_log_tail,
)


def test_runtime_log_projection_is_bounded_and_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(projection, "_MAX_FILE_BYTES", 240)
    handler = RuntimeLogProjectionHandler("alpha", repository_root=tmp_path)
    logger = logging.getLogger("tests.runtime.log_projection")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for index in range(8):
            logger.info("entry %s credential=secret-value path=C:\\Secrets\\value.txt", index)
    finally:
        logger.removeHandler(handler)
        handler.close()

    lines = read_runtime_log_tail("alpha", 20, repository_root=tmp_path)
    text = "".join(lines)

    assert "entry 7" in text
    assert "secret-value" not in text
    assert "C:\\Secrets\\value.txt" not in text
    assert (tmp_path / "config" / "state" / "bot-runtime" / "logs" / "alpha.log").stat().st_size <= 240


def test_runtime_log_projection_rejects_path_like_profile(tmp_path):
    with pytest.raises(ValueError, match="формат"):
        read_runtime_log_tail("../outside", repository_root=tmp_path)
