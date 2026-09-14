from datetime import datetime

import pytest

from module.webui.event_observation_update import persist_current_pt_observation


def test_naive_evidence_timestamp_is_rejected_before_write(tmp_path):
    with pytest.raises(ValueError, match="должна содержать часовой пояс"):
        persist_current_pt_observation(
            instance="test-instance",
            event_id="event-test",
            server="EN",
            source_revision="c" * 40,
            value=100,
            observed_at=datetime(2026, 8, 20, 12, 0, 0),
            root=tmp_path,
        )

    assert list(tmp_path.rglob("*.json")) == []
