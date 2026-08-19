"""Минимальные типизированные runtime-policy для unit-тестов генератора."""

from module.event_datamine.runtime_policy import (
    BossClearPolicy,
    MapRuntimePolicy,
    SirenRecognitionPolicy,
)
from module.event_datamine.runtime_semantics import (
    BattlePlanPolicy,
    CameraCalibrationPolicy,
    DetectorCalibrationPolicy,
    LinePeaksPolicy,
    SwipeCalibrationPolicy,
)


def runtime_policy(
    *,
    map_id: int = 1,
    chapter_name: str = "T",
    strategy: str = "campaign",
    siren: SirenRecognitionPolicy | None = None,
    camera_data: tuple[str, ...] = ("A1",),
    spawn_points: tuple[str, ...] = ("A1",),
) -> MapRuntimePolicy:
    return MapRuntimePolicy(
        map_id=map_id,
        chapter_name=chapter_name,
        source_path="campaign/event/fixture.py",
        siren_recognition=siren,
        boss_clear=BossClearPolicy(strategy),
        camera_calibration=CameraCalibrationPolicy(
            camera_data=camera_data,
            spawn_points=spawn_points,
        ),
        detector_calibration=DetectorCalibrationPolicy(
            internal_lines=LinePeaksPolicy(
                height=(80.0, 238.0),
                prominence=10.0,
                distance=35.0,
                width=(0.9, 10.0),
            ),
            edge_lines=LinePeaksPolicy(
                height=(238.0, 255.0),
                prominence=10.0,
                distance=50.0,
                wlen=1000.0,
            ),
            swipe=SwipeCalibrationPolicy(
                adb=(1.0, 1.1),
                minitouch=(1.0, 1.1),
                maatouch=(1.0, 1.1),
            ),
        ),
        battle_plan=BattlePlanPolicy(
            enemy_filter="1L > 1M > 1E > 1C",
            siren_filter_steps=(),
        ),
    )
