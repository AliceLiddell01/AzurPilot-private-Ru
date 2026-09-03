from module.dock_inventory.mumu_traversal import DockMuMuInventoryTraversal


def test_real_v16_1_overshift_is_rejected() -> None:
    """Реальный v16.1 overshift убрал верхнюю строку из supported scan area."""
    assert not DockMuMuInventoryTraversal._target_nudge_proven(
        shift_x=-0.003,
        shift_y=-36.991,
        response=0.945,
    )


def test_expected_three_row_top_shift_is_accepted() -> None:
    """Около 21.5 px соответствует доказанной трёхстрочной top-геометрии."""
    assert DockMuMuInventoryTraversal._target_nudge_proven(
        shift_x=0.0,
        shift_y=-21.5,
        response=0.9,
    )


def test_initial_nudge_above_geometry_cap_remains_rejected() -> None:
    assert not DockMuMuInventoryTraversal._target_nudge_proven(
        shift_x=0.0,
        shift_y=-24.001,
        response=1.0,
    )
