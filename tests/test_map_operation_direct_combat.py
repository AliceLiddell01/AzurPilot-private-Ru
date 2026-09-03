from module.map.map_operation import MapOperation


def test_direct_combat_loading_switches_to_auto_search(monkeypatch):
    operation = MapOperation.__new__(MapOperation)
    operation.map_is_auto_search = False

    monkeypatch.setattr(operation, "is_combat_loading", lambda: True, raising=False)

    assert operation._handle_direct_combat_loading() is True
    assert operation.map_is_auto_search is True


def test_direct_combat_loading_keeps_map_mode_without_loading(monkeypatch):
    operation = MapOperation.__new__(MapOperation)
    operation.map_is_auto_search = False

    monkeypatch.setattr(operation, "is_combat_loading", lambda: False, raising=False)

    assert operation._handle_direct_combat_loading() is False
    assert operation.map_is_auto_search is False
