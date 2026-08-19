import numpy as np
import pytest

from module.dock_inventory.catalog import (
    DockCanonicalShip,
    DockCatalogProvenance,
    DockIdentityCatalog,
)
from module.dock_inventory.model import IdentityStatus
from module.formation.model import (
    FormationFleetSide,
    FormationFleetSnapshot,
)
from module.formation.scanner import (
    FormationFleetInfoScanner,
    FormationFleetInputError,
    GLOBAL_FORMATION_INFO_LAYOUT_1280_720,
)


def _catalog() -> DockIdentityCatalog:
    return DockIdentityCatalog(
        records=(
            DockCanonicalShip("azur_lane_ship_group:1", "Alabama"),
            DockCanonicalShip("azur_lane_ship_group:2", "Belfast"),
        ),
        provenance=DockCatalogProvenance(
            source_repository="fixture/repo",
            source_commit="1" * 40,
            source_path="ship_data.json",
            source_blob_sha="2" * 40,
            source_sha256="3" * 64,
            source_generator_path="extractor.py",
            source_generator_blob_sha="4" * 40,
            supplemental_source_repository="fixture/lua",
            supplemental_source_commit="5" * 40,
            supplemental_source_path="fleet_tech_ship_class.lua",
            supplemental_source_blob_sha="6" * 40,
            selection_contract="fixture",
        ),
    )


class _FakeOcr:
    def __init__(self, values: tuple[str, ...]) -> None:
        self.values = values
        self.calls = []

    def read_names(self, frame, areas):
        self.calls.append(tuple(areas))
        return self.values


def _frame_with_slots(*indices: int) -> np.ndarray:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    for index in indices:
        area = GLOBAL_FORMATION_INFO_LAYOUT_1280_720.slots[index].portrait_area
        x1, y1, x2, y2 = area
        yy, xx = np.indices((y2 - y1, x2 - x1))
        checker = ((xx + yy) % 2 * 255).astype(np.uint8)
        frame[y1:y2, x1:x2, 0] = checker
        frame[y1:y2, x1:x2, 1] = checker
        frame[y1:y2, x1:x2, 2] = checker
    return frame


def test_scanner_reads_only_occupied_slots_and_preserves_fixed_order() -> None:
    ocr = _FakeOcr(("Alabama", "Belfast (Retrofit)"))
    scanner = FormationFleetInfoScanner(_catalog(), name_ocr=ocr)

    result = scanner.scan(_frame_with_slots(0, 3), fleet_index=6)

    assert result.fleet_index == 6
    assert result.occupied_count == 2
    assert result.complete is True
    assert [(slot.side, slot.position, slot.occupied) for slot in result.slots] == [
        (FormationFleetSide.MAIN, 1, True),
        (FormationFleetSide.MAIN, 2, False),
        (FormationFleetSide.MAIN, 3, False),
        (FormationFleetSide.VANGUARD, 1, True),
        (FormationFleetSide.VANGUARD, 2, False),
        (FormationFleetSide.VANGUARD, 3, False),
    ]
    assert result.slots[0].canonical_name == "Alabama"
    assert result.slots[3].canonical_name == "Belfast"
    assert result.slots[3].displayed_name == "Belfast (Retrofit)"
    assert ocr.calls == [(
        GLOBAL_FORMATION_INFO_LAYOUT_1280_720.slots[0].name_area,
        GLOBAL_FORMATION_INFO_LAYOUT_1280_720.slots[3].name_area,
    )]


def test_blank_name_on_occupied_slot_is_unresolved_not_empty() -> None:
    scanner = FormationFleetInfoScanner(_catalog(), name_ocr=_FakeOcr(("",)))

    result = scanner.scan(_frame_with_slots(0), fleet_index=1)

    assert result.slots[0].occupied is True
    assert result.slots[0].identity_status is IdentityStatus.UNRESOLVED
    assert result.slots[0].raw_name_ocr == ""
    assert result.complete is False


def test_presence_is_independent_from_name_ocr() -> None:
    scanner = FormationFleetInfoScanner(_catalog(), name_ocr=_FakeOcr(()))
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    result = scanner.scan(frame, fleet_index=2)

    assert result.occupied_count == 0
    assert result.complete is True


@pytest.mark.parametrize("fleet_index", [0, 7, True, 1.0])
def test_invalid_fleet_index_is_rejected(fleet_index) -> None:
    scanner = FormationFleetInfoScanner(_catalog(), name_ocr=_FakeOcr(()))

    with pytest.raises(FormationFleetInputError, match="fleet_index"):
        scanner.scan(np.zeros((720, 1280, 3), dtype=np.uint8), fleet_index=fleet_index)


def test_wrong_frame_geometry_is_rejected() -> None:
    scanner = FormationFleetInfoScanner(_catalog(), name_ocr=_FakeOcr(()))

    with pytest.raises(FormationFleetInputError, match="геометрию"):
        scanner.scan(np.zeros((1080, 1920, 3), dtype=np.uint8), fleet_index=1)


def test_snapshot_requires_canonical_slot_order() -> None:
    scanner = FormationFleetInfoScanner(_catalog(), name_ocr=_FakeOcr(()))
    result = scanner.scan(np.zeros((720, 1280, 3), dtype=np.uint8), fleet_index=1)

    with pytest.raises(ValueError, match="порядок"):
        FormationFleetSnapshot(
            fleet_index=1,
            slots=tuple(reversed(result.slots)),
            catalog_fingerprint=result.catalog_fingerprint,
        )
