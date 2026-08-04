from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
import warnings
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import psutil
from rich.console import Console
from rich.table import Table

from dev_tools.stage8a_device_acceptance import (
    AcceptanceFailure,
    _check_android_boot_completed,
    _detect_package,
    _git_head_sha,
    _load_profile,
    _resolve_adb,
    _resolve_serial,
    _safe_text,
    _validate_bgr_image,
    _validate_profile_name,
)

DEFAULT_REPORT = Path("artifacts/device/screenshot-interval-benchmark.json")
DEFAULT_MARKDOWN_REPORT = Path("artifacts/device/screenshot-interval-benchmark.md")
DEFAULT_NORMAL_INTERVALS = (
    0.001,
    0.005,
    0.01,
    0.0167,
    0.025,
    0.0333,
    0.05,
    0.075,
    0.1,
    0.15,
    0.2,
    0.3,
)
DEFAULT_COMBAT_INTERVALS = (
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.15,
    0.2,
    0.3,
    0.5,
    0.75,
    1.0,
)
NORMAL_RANGE = (0.001, 0.3)
COMBAT_RANGE = (0.001, 1.0)
MIN_FRAMES = 5


@dataclass(frozen=True)
class IntervalResult:
    phase: str
    requested_interval_s: float
    target_fps: float
    frames: int
    wall_s: float
    achieved_fps: float
    target_achievement_ratio: float
    interval_p50_ms: float
    interval_p95_ms: float
    interval_max_ms: float
    deadline_miss_ratio: float
    process_cpu_percent: float
    system_cpu_percent: float
    rss_delta_mib: float
    stable: bool
    frame_contract: dict[str, Any] | None
    error: str | None = None


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _parse_intervals(
    raw: str | None,
    defaults: tuple[float, ...],
    bounds: tuple[float, float],
) -> list[float]:
    values = defaults if raw is None else tuple(
        float(part.strip()) for part in raw.split(",") if part.strip()
    )
    if not values:
        raise AcceptanceFailure("Список интервалов benchmark не может быть пустым.")
    low, high = bounds
    normalized: list[float] = []
    for value in values:
        if not math.isfinite(value) or not low <= value <= high:
            raise AcceptanceFailure(
                f"Интервал {value!r} вне разрешённого диапазона {low}–{high} с."
            )
        rounded = round(float(value), 6)
        if rounded not in normalized:
            normalized.append(rounded)
    return sorted(normalized)


def _with_current(
    values: list[float],
    current: float,
    bounds: tuple[float, float],
) -> list[float]:
    low, high = bounds
    current = round(float(current), 6)
    if low <= current <= high and current not in values:
        values = [*values, current]
    return sorted(values)


def _process_cpu_seconds(process: psutil.Process) -> float:
    cpu = process.cpu_times()
    return float(cpu.user + cpu.system)


def _system_cpu_percent(before: Any, after: Any) -> float:
    before_values = before._asdict()
    after_values = after._asdict()
    deltas = {
        key: max(0.0, float(after_values.get(key, 0.0) - value))
        for key, value in before_values.items()
    }
    total = sum(deltas.values())
    idle = deltas.get("idle", 0.0) + deltas.get("iowait", 0.0)
    return 0.0 if total <= 0 else max(0.0, min(100.0, (total - idle) / total * 100.0))


def _summarize_interval(
    *,
    phase: str,
    requested_interval_s: float,
    starts: list[float],
    ends: list[float],
    process_cpu_seconds: float,
    system_cpu_percent: float,
    rss_delta_bytes: int,
    frame_contract: dict[str, Any] | None,
    error: str | None = None,
) -> IntervalResult:
    if not starts or not ends:
        return IntervalResult(
            phase=phase,
            requested_interval_s=requested_interval_s,
            target_fps=1.0 / requested_interval_s,
            frames=0,
            wall_s=0.0,
            achieved_fps=0.0,
            target_achievement_ratio=0.0,
            interval_p50_ms=0.0,
            interval_p95_ms=0.0,
            interval_max_ms=0.0,
            deadline_miss_ratio=1.0,
            process_cpu_percent=0.0,
            system_cpu_percent=system_cpu_percent,
            rss_delta_mib=rss_delta_bytes / (1024 * 1024),
            stable=False,
            frame_contract=frame_contract,
            error=error or "Снимки не получены.",
        )

    wall_s = max(ends[-1] - starts[0], 1e-9)
    start_intervals = [later - earlier for earlier, later in pairwise(starts)]
    if not start_intervals:
        start_intervals = [ends[0] - starts[0]]
    p50 = _percentile(start_intervals, 0.50)
    p95 = _percentile(start_intervals, 0.95)
    target_fps = 1.0 / requested_interval_s
    if len(starts) > 1:
        measurement_span = max(starts[-1] - starts[0], 1e-9)
        achieved_fps = (len(starts) - 1) / measurement_span
    else:
        achieved_fps = 1.0 / wall_s
    achievement = achieved_fps / target_fps
    tolerance = max(0.002, requested_interval_s * 0.10)
    misses = sum(value > requested_interval_s + tolerance for value in start_intervals)
    miss_ratio = misses / len(start_intervals)
    stable = (
        error is None
        and len(ends) >= MIN_FRAMES
        and achievement >= 0.90
        and miss_ratio <= 0.10
        and p95 <= requested_interval_s + tolerance
    )
    return IntervalResult(
        phase=phase,
        requested_interval_s=requested_interval_s,
        target_fps=target_fps,
        frames=len(ends),
        wall_s=wall_s,
        achieved_fps=achieved_fps,
        target_achievement_ratio=achievement,
        interval_p50_ms=p50 * 1000,
        interval_p95_ms=p95 * 1000,
        interval_max_ms=max(start_intervals) * 1000,
        deadline_miss_ratio=miss_ratio,
        process_cpu_percent=max(0.0, process_cpu_seconds / wall_s * 100.0),
        system_cpu_percent=system_cpu_percent,
        rss_delta_mib=rss_delta_bytes / (1024 * 1024),
        stable=stable,
        frame_contract=frame_contract,
        error=error,
    )


def _benchmark_interval(
    device: Any,
    *,
    phase: str,
    interval: float,
    duration: float,
    warmup_frames: int,
) -> IntervalResult:
    device.screenshot_interval_set(float(interval))
    for _ in range(warmup_frames):
        device.screenshot()

    process = psutil.Process()
    process_cpu_before = _process_cpu_seconds(process)
    system_cpu_before = psutil.cpu_times()
    rss_before = int(process.memory_info().rss)
    starts: list[float] = []
    ends: list[float] = []
    frame_contract: dict[str, Any] | None = None
    error: str | None = None
    deadline = time.perf_counter() + duration

    try:
        while time.perf_counter() < deadline or len(ends) < MIN_FRAMES:
            started = time.perf_counter()
            image = device.screenshot()
            ended = time.perf_counter()
            starts.append(started)
            ends.append(ended)
            if frame_contract is None:
                frame_contract = _validate_bgr_image(image)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {_safe_text(str(exc))}"

    process_cpu_after = _process_cpu_seconds(process)
    system_cpu_after = psutil.cpu_times()
    rss_after = int(process.memory_info().rss)
    return _summarize_interval(
        phase=phase,
        requested_interval_s=interval,
        starts=starts,
        ends=ends,
        process_cpu_seconds=max(0.0, process_cpu_after - process_cpu_before),
        system_cpu_percent=_system_cpu_percent(system_cpu_before, system_cpu_after),
        rss_delta_bytes=rss_after - rss_before,
        frame_contract=frame_contract,
        error=error,
    )


def _pick_at_least(results: list[IntervalResult], minimum: float) -> float | None:
    stable = sorted(result.requested_interval_s for result in results if result.stable)
    if not stable:
        return None
    for value in stable:
        if value + 1e-9 >= minimum:
            return value
    return stable[-1]


def _recommend_profiles(
    normal: list[IntervalResult],
    combat: list[IntervalResult],
    *,
    current_normal: float,
    current_combat: float,
) -> dict[str, Any]:
    minimum_normal = _pick_at_least(normal, 0.0)
    minimum_combat = _pick_at_least(combat, 0.0)
    if minimum_normal is None or minimum_combat is None:
        return {
            "status": "INSUFFICIENT_STABLE_SAMPLES",
            "recommended_profile": "current",
            "profiles": {
                "current": {
                    "normal_s": current_normal,
                    "combat_s": current_combat,
                }
            },
        }

    fast_normal = minimum_normal
    fast_combat = _pick_at_least(combat, max(minimum_combat, fast_normal * 2))
    balanced_normal = _pick_at_least(normal, max(minimum_normal * 2, 0.05))
    if balanced_normal is None:
        balanced_normal = minimum_normal
    balanced_combat = _pick_at_least(
        combat,
        max(minimum_combat * 2, balanced_normal * 3, 0.15),
    )
    low_normal = _pick_at_least(normal, max(minimum_normal * 4, 0.1))
    if low_normal is None:
        low_normal = minimum_normal
    low_combat = _pick_at_least(
        combat,
        max(minimum_combat * 4, low_normal * 3, 0.3),
    )
    return {
        "status": "PASS",
        "recommended_profile": "balanced",
        "minimum_stable": {
            "normal_s": minimum_normal,
            "combat_s": minimum_combat,
        },
        "profiles": {
            "fast": {
                "normal_s": fast_normal,
                "combat_s": fast_combat,
                "intent": (
                    "Минимальная устойчивая задержка; максимальная частота снимков."
                ),
            },
            "balanced": {
                "normal_s": balanced_normal,
                "combat_s": balanced_combat,
                "intent": (
                    "Запас к p95 и ограничение обычного режима примерно 20 FPS, "
                    "боя — примерно 6–7 FPS."
                ),
            },
            "low_load": {
                "normal_s": low_normal,
                "combat_s": low_combat,
                "intent": "Приоритет снижения нагрузки и температуры.",
            },
            "current": {
                "normal_s": current_normal,
                "combat_s": current_combat,
                "intent": "Текущие значения профиля до benchmark.",
            },
        },
        "heuristic": {
            "stable_requires": (
                "не менее 90% целевого FPS, не более 10% пропусков, "
                "p95 в пределах 10% + 2 мс"
            ),
            "automatic_config_write": False,
        },
    }


def _confirm(token: str, message: str, non_interactive: bool) -> None:
    if non_interactive:
        return
    entered = input(f"{message}\nВведите {token}, чтобы продолжить: ").strip()
    if entered != token:
        raise AcceptanceFailure("Benchmark отменён пользователем.")


def _load_config(profile: str):
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*invalid escape sequence.*",
            category=SyntaxWarning,
            module=r"module\.device\.method\.uiautomator_2",
        )
        from module.config.config import AzurLaneConfig
        from module.device.device import Device
    config = AzurLaneConfig(profile, task=None)
    return config, Device(config)


def _run_phase(
    device: Any,
    *,
    phase: str,
    intervals: list[float],
    duration: float,
    warmup_frames: int,
) -> list[IntervalResult]:
    results: list[IntervalResult] = []
    console = Console()
    for index, interval in enumerate(intervals, 1):
        console.print(
            f"[{phase}] {index}/{len(intervals)}: интервал {interval:g} с",
            highlight=False,
        )
        results.append(
            _benchmark_interval(
                device,
                phase=phase,
                interval=interval,
                duration=duration,
                warmup_frames=warmup_frames,
            )
        )
    return results


def _result_table(results: list[IntervalResult], title: str) -> Table:
    table = Table(title=title)
    table.add_column("Интервал", justify="right")
    table.add_column("FPS", justify="right")
    table.add_column("p50", justify="right")
    table.add_column("p95", justify="right")
    table.add_column("Пропуски", justify="right")
    table.add_column("CPU процесса", justify="right")
    table.add_column("Стабильно", justify="center")
    for result in results:
        table.add_row(
            f"{result.requested_interval_s:g} с",
            f"{result.achieved_fps:.1f}",
            f"{result.interval_p50_ms:.1f} мс",
            f"{result.interval_p95_ms:.1f} мс",
            f"{result.deadline_miss_ratio * 100:.0f}%",
            f"{result.process_cpu_percent:.1f}%",
            "да" if result.stable else "нет",
        )
    return table


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    recommendations = report["recommendations"]
    lines = [
        "# Benchmark интервала снимков экрана",
        "",
        f"- Exact head: `{report['head_sha']}`",
        f"- Профиль: `{report['profile']}`",
        f"- Backend: `{report['screenshot_backend']}`",
        f"- Config unchanged: `{str(report['config_unchanged']).lower()}`",
        "",
        "## Рекомендация",
        "",
    ]
    profile_name = recommendations["recommended_profile"]
    profile = recommendations["profiles"][profile_name]
    lines.extend(
        [
            f"Профиль: **{profile_name}**",
            "",
            f"- `Optimization_ScreenshotInterval`: `{profile['normal_s']}`",
            f"- `Optimization_CombatScreenshotInterval`: `{profile['combat_s']}`",
            "",
            "## Результаты",
            "",
            (
                "| Режим | Интервал | FPS | p50 | p95 | Пропуски | "
                "CPU процесса | Стабильно |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|:---:|",
        ]
    )
    for result in [*report["normal_results"], *report["combat_results"]]:
        lines.append(
            "| {phase} | {interval:g} с | {fps:.1f} | {p50:.1f} мс | "
            "{p95:.1f} мс | {miss:.0f}% | {cpu:.1f}% | {stable} |".format(
                phase=result["phase"],
                interval=result["requested_interval_s"],
                fps=result["achieved_fps"],
                p50=result["interval_p50_ms"],
                p95=result["interval_p95_ms"],
                miss=result["deadline_miss_ratio"] * 100,
                cpu=result["process_cpu_percent"],
                stable="да" if result["stable"] else "нет",
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    _validate_profile_name(args.profile)
    head = _git_head_sha()
    if args.expected_head and args.expected_head != head:
        raise AcceptanceFailure(
            f"Benchmark head mismatch: ожидался {args.expected_head}, получен {head}."
        )
    if not 0.5 <= args.duration <= 30:
        raise AcceptanceFailure("--duration должен быть в диапазоне 0.5–30 секунд.")
    if not 0 <= args.warmup_frames <= 20:
        raise AcceptanceFailure("--warmup-frames должен быть в диапазоне 0–20.")

    profile = _load_profile(args.profile)
    serial = _resolve_serial(args, profile)
    configured_serial = str(profile["serial"]).strip()
    if serial != configured_serial:
        raise AcceptanceFailure(
            "Serial benchmark должен совпадать с Emulator_Serial выбранного профиля."
        )
    adb = _resolve_adb(args.adb)
    _check_android_boot_completed(adb, serial)
    package = _detect_package(adb, serial, profile["package"])
    config_path = Path("config") / f"{args.profile}.json"
    config_hash_before = _sha256(config_path)
    config, device = _load_config(args.profile)
    current_normal = float(config.Optimization_ScreenshotInterval)
    current_combat = float(config.Optimization_CombatScreenshotInterval)
    normal_intervals = _with_current(
        _parse_intervals(args.normal_intervals, DEFAULT_NORMAL_INTERVALS, NORMAL_RANGE),
        current_normal,
        NORMAL_RANGE,
    )
    combat_intervals = _with_current(
        _parse_intervals(args.combat_intervals, DEFAULT_COMBAT_INTERVALS, COMBAT_RANGE),
        current_combat,
        COMBAT_RANGE,
    )

    print(
        "Benchmark не отправляет игре touch/key-команды и использует только "
        "настроенный screenshot backend."
    )
    print(f"Exact head: {head}")
    print(f"Профиль/backend: {args.profile} / {profile['screenshot_backend']}")
    print(f"Текущие интервалы: обычный={current_normal:g} с, бой={current_combat:g} с")
    _confirm(
        "NORMAL",
        "Откройте типичный статический экран игры без UID/чата.",
        args.non_interactive,
    )
    normal_results = _run_phase(
        device,
        phase="normal",
        intervals=normal_intervals,
        duration=args.duration,
        warmup_frames=args.warmup_frames,
    )

    if args.same_screen:
        combat_context = "same_screen"
    else:
        _confirm(
            "COMBAT",
            "Откройте типичный бой с активной анимацией и оставьте его запущенным.",
            args.non_interactive,
        )
        combat_context = (
            "combat_screen"
            if not args.non_interactive
            else "unverified_current_screen"
        )
    combat_results = _run_phase(
        device,
        phase="combat",
        intervals=combat_intervals,
        duration=args.duration,
        warmup_frames=args.warmup_frames,
    )

    device.screenshot_interval_set(current_normal)
    config_hash_after = _sha256(config_path)
    config_unchanged = config_hash_before == config_hash_after
    if not config_unchanged:
        raise AcceptanceFailure(
            "Benchmark обнаружил изменение постоянного profile config."
        )

    recommendations = _recommend_profiles(
        normal_results,
        combat_results,
        current_normal=current_normal,
        current_combat=current_combat,
    )
    return {
        "status": "PASS",
        "head_sha": head,
        "profile": args.profile,
        "package": package,
        "serial_redacted": True,
        "screenshot_backend": profile["screenshot_backend"],
        "duration_per_candidate_s": args.duration,
        "warmup_frames": args.warmup_frames,
        "combat_context": combat_context,
        "current": {
            "Optimization_ScreenshotInterval": current_normal,
            "Optimization_CombatScreenshotInterval": current_combat,
        },
        "recommendations": recommendations,
        "normal_results": [asdict(result) for result in normal_results],
        "combat_results": [asdict(result) for result in combat_results],
        "config_unchanged": config_unchanged,
        "automatic_config_write": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark интервалов снимков экрана на реальном устройстве"
    )
    parser.add_argument("--profile", required=True)
    serial_group = parser.add_mutually_exclusive_group(required=True)
    serial_group.add_argument("--serial")
    serial_group.add_argument("--serial-from-config", action="store_true")
    parser.add_argument("--adb")
    parser.add_argument("--expected-head")
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--warmup-frames", type=int, default=2)
    parser.add_argument("--normal-intervals")
    parser.add_argument("--combat-intervals")
    parser.add_argument("--same-screen", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown-report", type=Path, default=DEFAULT_MARKDOWN_REPORT)
    args = parser.parse_args(argv)

    try:
        report = run_benchmark(args)
    except Exception as exc:  # noqa: BLE001
        failure = {
            "status": "FAIL",
            "error": _safe_text(str(exc)),
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Benchmark: FAIL — {failure['error']}", file=sys.stderr)
        return 1

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_markdown(report, args.markdown_report)
    console = Console()
    console.print(
        _result_table(
            [IntervalResult(**row) for row in report["normal_results"]],
            "Обычный режим",
        )
    )
    console.print(
        _result_table(
            [IntervalResult(**row) for row in report["combat_results"]],
            "Бой",
        )
    )
    recommendation = report["recommendations"]
    selected = recommendation["profiles"][recommendation["recommended_profile"]]
    print("Benchmark: PASS")
    print(
        "Рекомендация: "
        f"Optimization_ScreenshotInterval={selected['normal_s']}, "
        f"Optimization_CombatScreenshotInterval={selected['combat_s']}"
    )
    print(f"JSON: {args.report}")
    print(f"Markdown: {args.markdown_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
