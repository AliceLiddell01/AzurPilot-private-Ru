from module.device import connection as connection_module
from module.device.connection import Connection


def test_orientation_descriptions_are_russian():
    assert Connection._orientation_description == {
        0: 'обычная',
        1: 'кнопка «Домой» справа',
        2: 'кнопка «Домой» сверху',
        3: 'кнопка «Домой» слева',
    }


def test_get_orientation_logs_russian_description(monkeypatch):
    device = object.__new__(Connection)
    monkeypatch.setattr(
        device,
        'adb_shell',
        lambda command: (
            'DisplayViewport{valid=true, orientation=1, '
            'deviceWidth=720, deviceHeight=1280}'
        ),
    )
    captured = []
    monkeypatch.setattr(
        connection_module.logger,
        'attr',
        lambda name, value: captured.append((name, value)),
    )

    assert device.get_orientation() == 1
    assert captured == [
        ('Ориентация устройства', '1 (кнопка «Домой» справа)'),
    ]


def test_get_orientation_logs_original_invalid_value(monkeypatch):
    device = object.__new__(Connection)
    monkeypatch.setattr(
        device,
        'adb_shell',
        lambda command: (
            'DisplayViewport{valid=true, orientation=7, '
            'deviceWidth=720, deviceHeight=1280}'
        ),
    )
    warnings = []
    monkeypatch.setattr(connection_module.logger, 'warning', warnings.append)
    monkeypatch.setattr(connection_module.logger, 'attr', lambda *args: None)

    assert device.get_orientation() == 0
    assert warnings == [
        '[Устройство — соединение] Недопустимая ориентация устройства: 7; '
        'используется обычная ориентация'
    ]


def test_get_orientation_warns_when_viewport_is_missing(monkeypatch):
    device = object.__new__(Connection)
    monkeypatch.setattr(device, 'adb_shell', lambda command: 'DisplayDeviceInfo{}')
    warnings = []
    monkeypatch.setattr(connection_module.logger, 'warning', warnings.append)
    monkeypatch.setattr(connection_module.logger, 'attr', lambda *args: None)

    assert device.get_orientation() == 0
    assert warnings == [
        '[Устройство — соединение] Не удалось получить ориентацию устройства; '
        'используется обычная ориентация'
    ]
