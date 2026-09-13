"""Проверки отсутствия локального runtime file sink у симулятора."""

import logging

from module.os_simulator.logger import OSSLogger


def test_os_simulator_logger_uses_parent_without_creating_text_file(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    child = logging.getLogger("alas.OSSimulator")
    handlers = list(child.handlers)
    propagate = child.propagate
    try:
        child.handlers.clear()
        wrapper = OSSLogger()

        assert wrapper.logger is child
        assert child.propagate is True
        assert not any(
            isinstance(handler, logging.FileHandler) for handler in child.handlers
        )
        assert not (tmp_path / "log").exists()
    finally:
        child.handlers[:] = handlers
        child.propagate = propagate
