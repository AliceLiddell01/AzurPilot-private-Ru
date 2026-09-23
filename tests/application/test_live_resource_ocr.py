from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from module.application.legacy_game_adapters import LegacyGameApplicationAdapter
from tests.support.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "game" / "resources" / "en_main_oil_25000_max_17050.png"


@pytest.fixture(scope="module")
def main_frame() -> np.ndarray:
    image = cv2.imread(str(FIXTURE), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def test_legacy_adapter_uses_one_fresh_frame_and_explicit_authority(
    main_frame: np.ndarray,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object, dict[str, object]]] = []
    appear_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class CampaignStatus:
        def __init__(self, config: object, device: object) -> None:
            self.config = config
            self.device = device

        def get_oil_snapshot(self, **kwargs: object) -> dict[str, int]:
            calls.append((self.config, self.device, kwargs))
            return {"Value": 25000, "Limit": 17050}

        def appear(self, *_args: object, **_kwargs: object) -> bool:
            appear_calls.append((_args, _kwargs))
            return True

    monkeypatch.setattr(
        "module.campaign.campaign_status.CampaignStatus",
        CampaignStatus,
    )

    class Device:
        image = main_frame

        def __init__(self) -> None:
            self.screenshot_calls = 0
            self.release_calls = 0

        def screenshot(self) -> np.ndarray:
            self.screenshot_calls += 1
            return self.image

        def release_resource(self) -> None:
            self.release_calls += 1

    device = Device()
    config = object()
    adapter = LegacyGameApplicationAdapter(
        config_factory=lambda instance: config,
        device_factory=lambda config: device,
    )

    observation = adapter.read_live_resources("ap")

    assert device.screenshot_calls == 1
    assert device.release_calls == 1
    assert len(calls) == 1
    assert len(appear_calls) == 1
    assert appear_calls[0][1] == {"offset": (10, 2)}
    assert calls[0][0] is config
    assert calls[0][1] is device
    assert calls[0][2] == {
        "skip_first_screenshot": True,
        "update": False,
        "record": False,
    }
    assert observation.current_state_authority is True
    assert observation.source == "campaign_status_oil_snapshot"
    assert observation.resources.items[0].value == 25000
    assert observation.resources.items[0].limit == 17050


def test_fixture_is_a_real_sanitized_png() -> None:
    assert FIXTURE.is_file()
    assert FIXTURE.suffix == ".png"
    assert Path(FIXTURE).stat().st_size > 10_000
