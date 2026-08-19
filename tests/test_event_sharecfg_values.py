import module.event_datamine.map_compiler as map_compiler
from module.event_datamine.map_compiler import sharecfg_values


def test_sharecfg_values_preserves_sequence_and_orders_mapping_keys():
    assert sharecfg_values(["a", "b"]) == ["a", "b"]
    assert sharecfg_values(("a", "b")) == ["a", "b"]
    assert sharecfg_values({2: "two", 0: "zero", "1": "text-one"}) == [
        "zero",
        "two",
        "text-one",
    ]
    assert sharecfg_values(None) == []


def test_private_values_helper_is_removed():
    assert not hasattr(map_compiler, "_values")
