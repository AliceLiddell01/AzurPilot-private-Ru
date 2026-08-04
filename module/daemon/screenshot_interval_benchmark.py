from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dev_tools.screenshot_interval_benchmark import (
    COMBAT_RANGE,
    DEFAULT_COMBAT_INTERVALS,
    DEFAULT_MARKDOWN_REPORT,
    DEFAULT_NORMAL_INTERVALS,
    DEFAULT_REPORT,
    NORMAL_RANGE,
    SCRCPY_FORCED_INTERVAL,
    IntervalResult,
    _benchmark_interval,
    _recommend_profiles,
    _result_table,
    _with_current,
    _write_markdown,
)
from module.combat.combat import BATTLE_PREPARATION
from module.logger import logger
from module.os_ash.ash import AshCombat
from module.os_ash.assets import (
    ASH_QUIT,
    ASH_SHOWDOWN,
    BEACON_EMPTY,
    BEACON_LIST,
    DOSSIER_LIST,
    META_MAIN_BEACON_ENTRANCE,
)
from module.os_ash.meta import OpsiAshBeacon
from module.ui.assets import BACK_ARROW
from module.ui.page import page_campaign, page_reward


class ScreenshotIntervalBenchmarkError(RuntimeError):
    """Безопасная остановка автоматического benchmark."""


def _git_head() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_text(value: object, *, limit: int = 400) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _box_bounds(box: Any) -> tuple[int, int, int, int]:
    points = list(box)
    if not points:
        raise ScreenshotIntervalBenchmarkError("OCR вернул пустую область текста.")
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def _compact_ocr_text(value: str) -> str:
    return re.sub(r"[^A-Z]", "", str(value).upper())


class AutomatedScreenshotIntervalBenchmark(OpsiAshBeacon):
    """Автоматический benchmark обычного экрана и бесплатной META simulation."""

    duration_per_candidate_s = 2.0
    warmup_frames = 2
    transition_timeout_s = 25.0
    simulation_button_timeout_s = 8.0
    simulation_button_min_score = 0.35

    def _wait_until(self, predicate, *, description: str, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (timeout or self.transition_timeout_s)
        while time.monotonic() < deadline:
            self.device.screenshot()
            if predicate():
                return
            self.device.sleep(0.25)
        raise ScreenshotIntervalBenchmarkError(
            f"Не удалось дождаться экрана: {description}."
        )

    def _run_phase(
        self,
        *,
        phase: str,
        intervals: list[float],
    ) -> list[IntervalResult]:
        results: list[IntervalResult] = []
        logger.hr(
            "Обычный режим" if phase == "normal" else "Бой META Simulation",
            level=2,
        )
        for index, interval in enumerate(intervals, 1):
            self.device.stuck_record_clear()
            logger.info(
                f"[Screenshot benchmark] {phase} {index}/{len(intervals)}: "
                f"интервал {interval:g} с"
            )
            result = _benchmark_interval(
                self.device,
                phase=phase,
                interval=interval,
                duration=self.duration_per_candidate_s,
                warmup_frames=self.warmup_frames,
            )
            results.append(result)
            logger.info(
                f"[Screenshot benchmark] {interval:g} с: "
                f"{result.achieved_fps:.1f} FPS, p95={result.interval_p95_ms:.1f} мс, "
                f"пропуски={result.deadline_miss_ratio * 100:.0f}%, "
                f"стабильно={'да' if result.stable else 'нет'}"
            )
            if result.error:
                logger.warning(
                    f"[Screenshot benchmark] Ошибка кандидата {interval:g} с: "
                    f"{result.error}"
                )
        return results

    def _prepare_normal_scene(self) -> None:
        logger.info(
            "[Screenshot benchmark] Переход на экран основной кампании для обычной фазы"
        )
        self.ui_ensure(page_campaign)
        self.device.screenshot()

    def _ensure_meta_showdown(self) -> None:
        self.ui_ensure(page_reward)
        self._ensure_meta_page()

        deadline = time.monotonic() + self.transition_timeout_s
        while time.monotonic() < deadline:
            self.device.screenshot()
            if self.appear(ASH_SHOWDOWN, offset=(30, 30)):
                return
            if self.appear(BEACON_LIST, offset=(20, 20)) or self.appear(
                DOSSIER_LIST,
                offset=(20, 20),
            ):
                self.device.click(ASH_QUIT)
                self.device.sleep(0.5)
                continue
            if self.handle_map_event():
                continue
            self.device.sleep(0.25)

        raise ScreenshotIntervalBenchmarkError(
            "Не удалось вернуться на главный экран META Showdown."
        )

    def _enter_current_target(self) -> None:
        self._ensure_meta_showdown()
        logger.info("[Screenshot benchmark] Открытие Current Target")
        self.device.click(META_MAIN_BEACON_ENTRANCE)

        self._wait_until(
            lambda: self.appear(BEACON_LIST, offset=(20, 20))
            or self.appear(BEACON_EMPTY, offset=(20, 20)),
            description="Current Target",
        )
        if self.appear(BEACON_EMPTY, offset=(20, 20)):
            logger.info(
                "[Screenshot benchmark] Активных META-боссов нет; "
                "бесплатная Battle Simulation остаётся доступной"
            )

    def _find_simulation_button(self) -> tuple[int, int]:
        from module.ocr.models import OCR_MODEL

        detections = OCR_MODEL.ppocr_v6.det(self.device.image)
        candidates: list[tuple[float, int, int, str]] = []
        recognized: list[str] = []
        for text, box, score in detections:
            value = str(text).strip()
            if value:
                recognized.append(value)
            compact = _compact_ocr_text(value)
            if "SIMULATION" not in compact:
                continue
            left, top, right, bottom = _box_bounds(box)
            center_x = (left + right) // 2
            center_y = (top + bottom) // 2
            if center_x < 640 or center_y < 300:
                continue
            candidates.append((float(score), center_x, center_y, value))

        if not candidates:
            sample = ", ".join(recognized[:12]) or "OCR не обнаружил текст"
            raise ScreenshotIntervalBenchmarkError(
                "Кнопка Battle Simulation не найдена. "
                f"Распознанный текст: {sample}. Обычная атака не запускалась."
            )

        score, center_x, center_y, text = max(candidates, key=lambda item: item[0])
        if score < self.simulation_button_min_score:
            raise ScreenshotIntervalBenchmarkError(
                f"Battle Simulation распознана с недостаточной уверенностью {score:.2f}; "
                "обычная атака не запускалась."
            )
        logger.info(
            f"[Screenshot benchmark] Найдена кнопка '{text}' "
            f"({score:.2f}) в ({center_x}, {center_y})"
        )
        return center_x, center_y

    def _wait_for_simulation_button(self) -> tuple[int, int]:
        deadline = time.monotonic() + self.simulation_button_timeout_s
        last_error: ScreenshotIntervalBenchmarkError | None = None
        while time.monotonic() < deadline:
            self.device.screenshot()
            try:
                return self._find_simulation_button()
            except ScreenshotIntervalBenchmarkError as exc:
                last_error = exc
                self.device.sleep(0.5)

        if last_error is not None:
            raise last_error
        raise ScreenshotIntervalBenchmarkError(
            "Кнопка Battle Simulation не найдена; обычная атака не запускалась."
        )

    def _enter_meta_simulation(self) -> AshCombat:
        if self.config.SERVER != "en":
            raise ScreenshotIntervalBenchmarkError(
                "Автоматический маршрут Battle Simulation пока разрешён только для "
                "EN/Global: безопасное распознавание кнопки проверено на английском UI."
            )

        self._enter_current_target()
        center_x, center_y = self._wait_for_simulation_button()
        logger.info("[Screenshot benchmark] Запуск бесплатной Battle Simulation")
        self.device.click((center_x, center_y))

        self._wait_until(
            lambda: self.appear(BATTLE_PREPARATION, offset=(30, 30)),
            description="Formation после Battle Simulation",
        )

        combat = AshCombat(config=self.config, device=self.device)
        self._benchmark_combat = combat
        combat.combat_preparation(
            balance_hp=False,
            emotion_reduce=False,
            auto="combat_auto",
        )
        self.device.screenshot()
        if not combat.is_combat_executing():
            raise ScreenshotIntervalBenchmarkError(
                "Battle Simulation не перешла в активный бой."
            )
        logger.info("[Screenshot benchmark] Battle Simulation активна")
        return combat

    def _leave_meta_simulation(self, combat: AshCombat) -> None:
        logger.info("[Screenshot benchmark] Безопасный выход из Battle Simulation")
        deadline = time.monotonic() + 35
        pause_clicked = False
        while time.monotonic() < deadline:
            self.device.screenshot()

            pause = combat.is_combat_executing()
            if pause and not pause_clicked:
                self.device.click(pause)
                pause_clicked = True
                self.device.sleep(0.5)
                continue
            if combat.handle_combat_quit(interval=0):
                continue
            if combat.handle_combat_quit_reconfirm(interval=0):
                continue
            if self._in_meta_page():
                break
            if self.appear(BATTLE_PREPARATION, offset=(30, 30)):
                self.device.click(BACK_ARROW)
                self.device.sleep(0.5)
                continue
            if combat.handle_battle_status():
                continue
            if combat.handle_get_items():
                continue
            if combat.handle_exp_info():
                continue
            self.device.sleep(0.25)
        else:
            logger.warning(
                "[Screenshot benchmark] Не удалось подтвердить выход из simulation "
                "за 35 секунд; выполняется переход на главный экран"
            )

        self.device.screenshot_interval_set()
        self.ui_goto_main()

    def run(self) -> dict[str, Any]:
        profile = str(getattr(self.config, "config_name", "alas"))
        config_path = Path("config") / f"{profile}.json"
        config_hash_before = _sha256(config_path)
        current_normal = float(self.config.Optimization_ScreenshotInterval)
        current_combat = float(self.config.Optimization_CombatScreenshotInterval)
        screenshot_backend = str(self.config.Emulator_ScreenshotMethod)
        forced_interval = (
            SCRCPY_FORCED_INTERVAL if screenshot_backend == "scrcpy" else None
        )

        if forced_interval is None:
            normal_intervals = _with_current(
                list(DEFAULT_NORMAL_INTERVALS),
                current_normal,
                NORMAL_RANGE,
            )
            combat_intervals = _with_current(
                list(DEFAULT_COMBAT_INTERVALS),
                current_combat,
                COMBAT_RANGE,
            )
        else:
            normal_intervals = [forced_interval]
            combat_intervals = [forced_interval]

        logger.hr("Benchmark интервалов снимков экрана", level=1)
        logger.info(
            f"[Screenshot benchmark] Backend={screenshot_backend}, "
            f"текущие интервалы={current_normal:g}/{current_combat:g} с"
        )
        logger.info(
            "[Screenshot benchmark] Настройки профиля автоматически не изменяются"
        )

        combat: AshCombat | None = None
        self._benchmark_combat: AshCombat | None = None
        returned_to_main = False
        normal_results: list[IntervalResult] = []
        combat_results: list[IntervalResult] = []
        try:
            self._prepare_normal_scene()
            normal_results = self._run_phase(
                phase="normal",
                intervals=normal_intervals,
            )
            combat = self._enter_meta_simulation()
            combat_results = self._run_phase(
                phase="combat",
                intervals=combat_intervals,
            )
        finally:
            self.device.screenshot_interval_set(current_normal)
            cleanup_combat = combat or self._benchmark_combat
            try:
                if cleanup_combat is not None:
                    self._leave_meta_simulation(cleanup_combat)
                else:
                    self.ui_goto_main()
                returned_to_main = True
            except Exception as exc:  # noqa: BLE001 - cleanup must not hide result.
                logger.warning(
                    "[Screenshot benchmark] Ошибка возврата на главный экран: "
                    f"{_safe_text(exc)}"
                )
            finally:
                self._benchmark_combat = None

        config_hash_after = _sha256(config_path)
        config_unchanged = config_hash_before == config_hash_after
        if not config_unchanged:
            raise ScreenshotIntervalBenchmarkError(
                "Benchmark обнаружил изменение постоянного profile config."
            )
        if not returned_to_main:
            raise ScreenshotIntervalBenchmarkError(
                "Benchmark завершил измерения, но не подтвердил возврат на главный экран."
            )
        if not normal_results or not combat_results:
            raise ScreenshotIntervalBenchmarkError(
                "Benchmark не завершил обе измерительные фазы."
            )

        recommendations = _recommend_profiles(
            normal_results,
            combat_results,
            current_normal=current_normal,
            current_combat=current_combat,
            forced_interval_s=forced_interval,
        )
        report = {
            "status": "PASS",
            "head_sha": _git_head(),
            "profile": profile,
            "package": str(self.config.Emulator_PackageName),
            "serial_redacted": True,
            "screenshot_backend": screenshot_backend,
            "backend_forced_interval_s": forced_interval,
            "duration_per_candidate_s": self.duration_per_candidate_s,
            "warmup_frames": self.warmup_frames,
            "normal_context": "campaign_page",
            "combat_context": "meta_current_target_battle_simulation",
            "automation": {
                "webui_tool": True,
                "normal_navigation": "page_campaign",
                "combat_navigation": (
                    "page_reward -> META Showdown -> Current Target -> "
                    "Battle Simulation -> Formation -> Battle"
                ),
                "simulation_button_requires_ocr_token": "SIMULATION",
                "ordinary_meta_attack_allowed": False,
                "returned_to_main": returned_to_main,
            },
            "task_stuck_guard_reset_per_candidate": True,
            "resources_released_during_interactive_wait": False,
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

        DEFAULT_REPORT.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_REPORT.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_markdown(report, DEFAULT_MARKDOWN_REPORT)
        logger.print(_result_table(normal_results, "Обычный режим"), justify="center")
        logger.print(_result_table(combat_results, "Бой META Simulation"), justify="center")

        selected_name = recommendations["recommended_profile"]
        selected = recommendations["profiles"][selected_name]
        logger.info(
            "[Screenshot benchmark] Рекомендация: "
            f"Optimization_ScreenshotInterval={selected['normal_s']}, "
            f"Optimization_CombatScreenshotInterval={selected['combat_s']}"
        )
        logger.info(f"[Screenshot benchmark] JSON: {DEFAULT_REPORT}")
        logger.info(f"[Screenshot benchmark] Markdown: {DEFAULT_MARKDOWN_REPORT}")
        return report


def run_screenshot_interval_benchmark(config: Any, device: Any) -> bool:
    try:
        AutomatedScreenshotIntervalBenchmark(config=config, device=device).run()
        return True
    except ScreenshotIntervalBenchmarkError as exc:
        logger.error(f"[Screenshot benchmark] {exc}")
        return False
