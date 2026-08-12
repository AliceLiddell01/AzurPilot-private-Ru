from module.dock_inventory.mumu_traversal import DockMuMuInventoryTraversal


def test_real_v16_initial_nudge_evidence_is_accepted() -> None:
    """Закрепить реальный MuMu v16 shift, который старый cap 36 px отклонял."""
    assert DockMuMuInventoryTraversal._target_nudge_proven(
        shift_x=-0.009,
        shift_y=-36.906,
        response=0.930,
    )


def test_initial_nudge_above_v16_safety_cap_remains_rejected() -> None:
    assert not DockMuMuInventoryTraversal._target_nudge_proven(
        shift_x=0.0,
        shift_y=-40.001,
        response=1.0,
    )
