from types import SimpleNamespace

from module.event_datamine.compiler import (
    EventCompiler,
    _date_part,
    _is_ignored_land_rotation_finding,
)
from module.event_datamine.model import ValidationFinding


class _OverflowingInt:
    def __int__(self):
        raise OverflowError("слишком большое значение")


def test_date_part_treats_overflow_as_invalid_source_time():
    assert _date_part([[_OverflowingInt(), 1, 1], [0, 0, 0]]) == ""


def test_missing_source_name_records_provenance_finding():
    compiler = object.__new__(EventCompiler)
    compiler.source = SimpleNamespace(snapshot=SimpleNamespace(server="EN"))
    compiler.findings = []

    name = compiler._name(None, "Game item 2:10", "shop.7.name")

    assert name == "Game item 2:10"
    assert [finding.code for finding in compiler.findings] == ["source_name_missing"]
    assert compiler.findings[0].path == "shop.7.name"


def test_ignored_land_rotation_matches_exact_typed_rotation():
    patch = SimpleNamespace(ignored_land_rotations=(10,))
    exact = ValidationFinding(
        "unknown_land_rotation",
        "error",
        "Неизвестный land rotation 10",
        "maps.1.land_based",
    )
    different = ValidationFinding(
        "unknown_land_rotation",
        "error",
        "Неизвестный land rotation 100",
        "maps.1.land_based",
    )

    assert _is_ignored_land_rotation_finding(exact, (patch,)) is True
    assert _is_ignored_land_rotation_finding(different, (patch,)) is False
