from module.logger import logger
from module.os.radar import Radar


def test_opsi_radar_ascii_grid_logs_debug_not_info(monkeypatch):
    debug_messages = []
    info_messages = []

    monkeypatch.setattr(logger, 'debug', lambda message, *args, **kwargs: debug_messages.append(str(message)))
    monkeypatch.setattr(logger, 'info', lambda message, *args, **kwargs: info_messages.append(str(message)))

    Radar(config=None).show()

    assert len(debug_messages) == 11
    assert info_messages == []
    assert any('FL' in message for message in debug_messages)
