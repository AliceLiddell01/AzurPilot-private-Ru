import numpy as np

from module.shop_event.clerk import EventShopClerk


class ScannerFact:
    def __init__(
        self,
        name,
        *,
        price,
        count,
        total_count,
        cost="pt",
        amount=1,
        image=None,
    ):
        self.name = name
        self.price = price
        self.count = count
        self.total_count = total_count
        self.cost = cost
        self.amount = amount
        self.image = image


def _image(value):
    return np.full((63, 63, 3), value, dtype=np.uint8)


def test_scanner_fact_match_rejects_count_and_total_count_changes():
    left = ScannerFact("DefaultItem", price=300, count=10, total_count=10)
    right = ScannerFact("Chip", price=300, count=10, total_count=10)

    assert EventShopClerk._same_scanner_row(left, right) is True

    right.count = 9
    assert EventShopClerk._same_scanner_row(left, right) is False

    right.count = 10
    right.total_count = 11
    assert EventShopClerk._same_scanner_row(left, right) is False


def test_scanner_overlap_requires_visual_evidence():
    old_row = [
        ScannerFact("A", price=300, count=4, total_count=4),
        ScannerFact("B", price=500, count=5, total_count=5),
    ]
    new_row = [
        ScannerFact("A", price=300, count=4, total_count=4),
        ScannerFact("B", price=500, count=5, total_count=5),
    ]

    assert EventShopClerk._scanner_overlap_proven(old_row, new_row) is False


def test_scanner_overlap_fails_open_for_visually_identical_row():
    old_row = [
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
    ]
    new_row = [
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
    ]

    assert EventShopClerk._scanner_overlap_proven(old_row, new_row) is False


def test_scanner_overlap_accepts_repeated_visually_distinct_row():
    old_row = [
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("Part", price=30, count=30, total_count=30, image=_image(160)),
    ]
    new_row = [
        ScannerFact("BoxT4", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("Part", price=30, count=30, total_count=30, image=_image(160)),
    ]

    assert EventShopClerk._scanner_overlap_proven(old_row, new_row) is True

    new_row[1].image = _image(200)
    assert EventShopClerk._scanner_overlap_proven(old_row, new_row) is False

def test_scanner_fact_match_uses_scanned_amount_not_derived_cost():
    left = ScannerFact(
        "Part", price=30, count=30, total_count=30, cost="pt", amount=2
    )
    right = ScannerFact(
        "Part", price=30, count=30, total_count=30, cost="URpt", amount=2
    )

    assert EventShopClerk._same_scanner_row(left, right) is True

    right.amount = 1
    assert EventShopClerk._same_scanner_row(left, right) is False


def test_partial_overlap_keeps_homogeneous_matched_subset():
    old_row = [
        ScannerFact("A", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("A", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("C", price=700, count=2, total_count=2, image=_image(160)),
    ]
    new_row = [
        ScannerFact("A", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("A", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("D", price=900, count=1, total_count=1, image=_image(200)),
    ]

    assert EventShopClerk._scanner_overlap_remainder(old_row, new_row) == new_row


def test_partial_overlap_deduplicates_visually_distinct_matched_subset():
    old_row = [
        ScannerFact("A", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("B", price=500, count=5, total_count=5, image=_image(160)),
        ScannerFact("C", price=700, count=2, total_count=2, image=_image(220)),
    ]
    new_row = [
        ScannerFact("A", price=300, count=4, total_count=4, image=_image(80)),
        ScannerFact("B", price=500, count=5, total_count=5, image=_image(160)),
        ScannerFact("D", price=900, count=1, total_count=1, image=_image(200)),
    ]

    assert EventShopClerk._scanner_overlap_remainder(old_row, new_row) == [new_row[2]]
