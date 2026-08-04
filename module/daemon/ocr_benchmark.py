"""Сравнительный benchmark OCR-моделей для EN/Global Azur Lane.

Benchmark запускает все установленные английские модели на одном и том же
наборе ``sets_num``. Китайские и японские модели намеренно не участвуют.
Тест измеряет распознавание уже вырезанных строк; детектор текста отдельно
не оценивается.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cv2
from rich.table import Table
from rich.text import Text

from module.config.config import AzurLaneConfig
from module.exception import RequestHumanTakeover
from module.logger import logger
from module.ocr.al_ocr import AlOcr
from module.ocr.model_policy import ENGLISH_ONNX_MODEL_VERSIONS

DATASET_PREFIX = "sets_num"
DATASET_SUBFOLDER = "sets_num"
REPORT_PATH = Path("artifacts/ocr/english-model-benchmark.json")
SPEED_ITERATIONS = 100
WARMUP_ITERATIONS = 3
MAX_REPORTED_MISMATCHES = 10


@dataclass(frozen=True)
class EnglishModelCandidate:
    """Одна конкретная английская модель, участвующая в сравнении."""

    version: str
    backend: str
    model_paths: tuple[str, ...]
    dictionary_path: str | None
    family: str

    @property
    def display_files(self) -> str:
        return ", ".join(Path(path).name for path in self.model_paths)


@dataclass
class BenchmarkResult:
    """Результат одного прогона модели."""

    version: str
    backend: str
    family: str
    device_requested: str
    runtime: str
    model_files: list[str]
    dictionary_file: str | None
    status: str
    accuracy: float = 0.0
    correct: int = 0
    total: int = 0
    errors: int = 0
    load_ms: float = 0.0
    avg_ms: float = 0.0
    p95_ms: float = 0.0
    mismatches: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    recommended: bool = False


class OcrBenchmark:
    """Сравнивает все доступные EN/Global OCR-модели."""

    def __init__(self, config, device=None, task=None):
        if isinstance(config, AzurLaneConfig):
            self.config = config
            if task is not None:
                self.config.init_task(task)
        else:
            self.config = AzurLaneConfig(config, task=task)
        self.device = device

    @staticmethod
    def _find_archive(prefix: str) -> str | None:
        for ext in (".zip", ".tar", ".tar.xz", ".tar.gz"):
            path = f"module/daemon/{prefix}{ext}"
            if os.path.exists(path):
                return path
        return None

    @staticmethod
    def _load_test_cases(extract_dir: str, subfolder: str) -> list[tuple[str, str]]:
        target_val_txt = os.path.join(extract_dir, "val.txt")
        if not os.path.exists(target_val_txt):
            target_val_txt = os.path.join(extract_dir, subfolder, "val.txt")

        test_cases: list[tuple[str, str]] = []
        if not os.path.exists(target_val_txt):
            return test_cases

        val_root = os.path.dirname(target_val_txt)
        with open(target_val_txt, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                image_path = os.path.join(val_root, parts[0])
                if not os.path.exists(image_path):
                    image_path = os.path.join(val_root, "imgs", parts[0])
                test_cases.append((image_path, parts[1]))
        return test_cases

    @contextmanager
    def _prepared_dataset(self):
        archive_path = self._find_archive(DATASET_PREFIX)
        extract_dir = f"module/daemon/{DATASET_PREFIX}_temp"
        try:
            if archive_path:
                logger.info(f"[OCR benchmark] Распаковка {archive_path}...")
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir)
                shutil.unpack_archive(archive_path, extract_dir)

            test_cases = self._load_test_cases(extract_dir, DATASET_SUBFOLDER)
            if not test_cases:
                raise FileNotFoundError(
                    f"Не удалось загрузить набор {DATASET_PREFIX}: отсутствует val.txt или изображения"
                )
            yield test_cases
        finally:
            if os.path.exists(extract_dir):
                try:
                    shutil.rmtree(extract_dir)
                except Exception as exc:
                    logger.error(
                        f"[OCR benchmark] Не удалось очистить {extract_dir}: {exc}"
                    )

    @staticmethod
    def _normalise_result(text: str) -> str:
        """Использует production-нормализатор, когда он доступен."""
        try:
            from module.ocr.ocr import normalize_ocr_text
        except ImportError:
            return text
        return normalize_ocr_text("azur_lane", text)

    @staticmethod
    def _model_candidates() -> list[EnglishModelCandidate]:
        from module.ocr.al_ocr import CUSTOM_CTC_MODEL_PARAMS, ONNX_MODEL_PARAMS

        candidates: list[EnglishModelCandidate] = []
        onnx_specs = ONNX_MODEL_PARAMS["azur_lane"]
        custom_specs = CUSTOM_CTC_MODEL_PARAMS.get("azur_lane", {})

        for version in ENGLISH_ONNX_MODEL_VERSIONS:
            if version in custom_specs:
                candidates.append(
                    EnglishModelCandidate(
                        version=version,
                        backend="onnxruntime",
                        model_paths=(str(custom_specs[version]),),
                        dictionary_path=None,
                        family="CNN-CTC",
                    )
                )
                continue

            model_path, dictionary_path, ocr_version = onnx_specs[version]
            family = getattr(ocr_version, "value", str(ocr_version))
            candidates.append(
                EnglishModelCandidate(
                    version=version,
                    backend="onnxruntime",
                    model_paths=(str(model_path),),
                    dictionary_path=str(dictionary_path),
                    family=family,
                )
            )

        try:
            from module.ocr.ncnn_ocr import MODEL_SPECS

            spec = MODEL_SPECS.get("azur_lane")
            if spec is not None:
                candidates.append(
                    EnglishModelCandidate(
                        version="ncnn_azur_lane",
                        backend="ncnn",
                        model_paths=(str(spec.param_path), str(spec.bin_path)),
                        dictionary_path=str(spec.keys_path),
                        family="NCNN CTC",
                    )
                )
        except Exception as exc:
            logger.warning(f"[OCR benchmark] NCNN-кандидат недоступен: {exc}")

        return candidates

    @staticmethod
    def _candidate_missing_files(candidate: EnglishModelCandidate) -> list[str]:
        paths = [Path(path) for path in candidate.model_paths]
        if candidate.dictionary_path:
            paths.append(Path(candidate.dictionary_path))
        return [str(path) for path in paths if not path.is_file()]

    def _configured_device(self) -> str:
        return str(self.config.ocr_device)

    def _candidate_device(self, candidate: EnglishModelCandidate) -> str:
        configured = self._configured_device()
        if candidate.backend == "onnxruntime":
            return configured
        return "gpu" if configured == "gpu" else "cpu"

    @contextmanager
    def _select_candidate(self, candidate: EnglishModelCandidate, device: str):
        import module.ocr.al_ocr as al_ocr

        original_global_config = al_ocr.config
        original_values = {
            "Optimization_OcrBackend": getattr(
                self.config,
                "Optimization_OcrBackend",
                "auto",
            ),
            "Optimization_OcrDevice": getattr(
                self.config,
                "Optimization_OcrDevice",
                "auto",
            ),
            "Optimization_OcrModelVersionEnglish": getattr(
                self.config,
                "Optimization_OcrModelVersionEnglish",
                "auto",
            ),
        }
        overrides: dict[str, Any] = {
            "Optimization_OcrBackend": candidate.backend,
            "Optimization_OcrDevice": device,
        }
        if candidate.backend == "onnxruntime":
            overrides["Optimization_OcrModelVersionEnglish"] = candidate.version

        self.config.override(**overrides)
        al_ocr.config = self.config
        al_ocr.reset_ocr_model()
        try:
            yield
        finally:
            al_ocr.reset_ocr_model()
            self.config.override(**original_values)
            al_ocr.config = original_global_config

    @staticmethod
    def _runtime_name(ocr: AlOcr, candidate: EnglishModelCandidate) -> str:
        model = ocr.model
        if candidate.backend == "ncnn":
            if getattr(model, "use_vulkan", False):
                gpu_index = getattr(model, "gpu_index", 0)
                return f"Vulkan GPU {gpu_index}"
            return "NCNN CPU"

        session = getattr(model, "session", None)
        if session is None:
            text_rec = getattr(model, "text_rec", None)
            session = getattr(getattr(text_rec, "session", None), "session", None)
        if session is not None:
            try:
                providers = session.get_providers()
            except Exception:
                providers = []
            if providers:
                return ", ".join(str(provider) for provider in providers)
        return "ONNX Runtime"

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    def _run_candidate(
        self,
        candidate: EnglishModelCandidate,
        test_cases: list[tuple[str, str]],
    ) -> BenchmarkResult:
        device = self._candidate_device(candidate)
        missing = self._candidate_missing_files(candidate)
        if missing:
            return BenchmarkResult(
                version=candidate.version,
                backend=candidate.backend,
                family=candidate.family,
                device_requested=device,
                runtime="не загружалась",
                model_files=[Path(path).name for path in candidate.model_paths],
                dictionary_file=(
                    Path(candidate.dictionary_path).name
                    if candidate.dictionary_path
                    else None
                ),
                status="НЕ УСТАНОВЛЕНА",
                error="Отсутствуют файлы: " + ", ".join(missing),
            )

        logger.hr(
            f"EN OCR: {candidate.version} | {candidate.backend} | {device}",
            level=2,
        )
        result = BenchmarkResult(
            version=candidate.version,
            backend=candidate.backend,
            family=candidate.family,
            device_requested=device,
            runtime="не определено",
            model_files=[Path(path).name for path in candidate.model_paths],
            dictionary_file=(
                Path(candidate.dictionary_path).name
                if candidate.dictionary_path
                else None
            ),
            status="ОШИБКА",
            total=len(test_cases),
        )

        try:
            with self._select_candidate(candidate, device):
                load_start = time.perf_counter()
                ocr = AlOcr(name="azur_lane")
                ocr.init()
                result.load_ms = (time.perf_counter() - load_start) * 1000
                result.runtime = self._runtime_name(ocr, candidate)

                correct = 0
                progress_step = max(1, len(test_cases) // 10)
                for index, (image_path, expected) in enumerate(test_cases, 1):
                    try:
                        actual = self._normalise_result(ocr.ocr(image_path))
                    except Exception as exc:
                        actual = f"<OCR ERROR: {type(exc).__name__}>"
                    if actual.strip().upper() == expected.strip().upper():
                        correct += 1
                    elif len(result.mismatches) < MAX_REPORTED_MISMATCHES:
                        result.mismatches.append(
                            {
                                "image": Path(image_path).name,
                                "expected": expected,
                                "actual": actual,
                            }
                        )
                    if index % progress_step == 0 or index == len(test_cases):
                        logger.info(
                            f"[{candidate.version}] Точность: {index}/{len(test_cases)}"
                        )

                result.correct = correct
                result.errors = result.total - correct
                result.accuracy = correct / result.total * 100 if result.total else 0.0

                benchmark_image = cv2.imread(test_cases[0][0])
                if benchmark_image is None:
                    raise FileNotFoundError(test_cases[0][0])
                for _ in range(WARMUP_ITERATIONS):
                    ocr.ocr(benchmark_image)

                samples: list[float] = []
                for _ in range(SPEED_ITERATIONS):
                    started = time.perf_counter()
                    ocr.ocr(benchmark_image)
                    samples.append((time.perf_counter() - started) * 1000)
                result.avg_ms = sum(samples) / len(samples)
                result.p95_ms = self._percentile(samples, 0.95)

                if result.accuracy == 100.0:
                    result.status = "ИДЕАЛЬНО"
                elif result.accuracy >= 99.0:
                    result.status = "ХОРОШО"
                elif result.accuracy >= 95.0:
                    result.status = "РИСК"
                else:
                    result.status = "НЕ ПОДХОДИТ"
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            logger.error(
                f"[OCR benchmark] {candidate.version} завершилась ошибкой: {result.error}"
            )

        return result

    @staticmethod
    def _rank_results(results: list[BenchmarkResult]) -> list[BenchmarkResult]:
        completed = [
            result
            for result in results
            if result.status not in {"ОШИБКА", "НЕ УСТАНОВЛЕНА"}
        ]
        completed.sort(
            key=lambda item: (
                -item.accuracy,
                item.errors,
                item.p95_ms,
                item.avg_ms,
                item.load_ms,
                item.version,
            )
        )
        if completed:
            completed[0].recommended = True
        return completed

    @staticmethod
    def _status_text(result: BenchmarkResult) -> Text:
        styles = {
            "ИДЕАЛЬНО": "bold bright_green",
            "ХОРОШО": "green",
            "РИСК": "bold yellow",
            "НЕ ПОДХОДИТ": "bold red",
            "НЕ УСТАНОВЛЕНА": "dim",
            "ОШИБКА": "bold red",
        }
        label = result.status
        if result.recommended:
            label = f"РЕКОМЕНДУЕТСЯ · {label}"
        return Text(label, style=styles.get(result.status, "white"))

    def _render_summary(self, results: list[BenchmarkResult]) -> None:
        ranked = self._rank_results(results)
        rank_by_version = {
            result.version: index
            for index, result in enumerate(ranked, 1)
        }

        table = Table(show_lines=True)
        table.add_column("Место", justify="right", no_wrap=True)
        table.add_column("Английская модель", style="cyan", no_wrap=True)
        table.add_column("Backend", style="magenta", no_wrap=True)
        table.add_column("Устройство / EP")
        table.add_column("Файл модели")
        table.add_column("Точность", justify="right", no_wrap=True)
        table.add_column("Ошибки", justify="right")
        table.add_column("Среднее", justify="right", no_wrap=True)
        table.add_column("p95", justify="right", no_wrap=True)
        table.add_column("Статус", no_wrap=True)

        for result in results:
            rank = rank_by_version.get(result.version)
            accuracy = "—"
            errors = "—"
            avg = "—"
            p95 = "—"
            if result.total:
                accuracy = f"{result.accuracy:.2f}% ({result.correct}/{result.total})"
                errors = str(result.errors)
            if result.avg_ms:
                avg = f"{result.avg_ms:.3f} мс"
                p95 = f"{result.p95_ms:.3f} мс"
            table.add_row(
                str(rank) if rank is not None else "—",
                result.version,
                result.backend,
                result.runtime,
                ", ".join(result.model_files),
                accuracy,
                errors,
                avg,
                p95,
                self._status_text(result),
            )

        logger.hr("Сводка сравнительного benchmark EN OCR", level=1)
        logger.print(table, justify="center")
        logger.info(
            "[OCR benchmark] Все строки проверены на одном наборе sets_num; "
            "оценивается распознавание готовых кропов, а не поиск текста на полном экране."
        )
        if ranked:
            winner = ranked[0]
            logger.info(
                f"[OCR benchmark] Рекомендуемая модель: {winner.version} — "
                f"{winner.accuracy:.2f}%, среднее {winner.avg_ms:.3f} мс, "
                f"p95 {winner.p95_ms:.3f} мс"
            )
        for result in results:
            if result.error:
                logger.warning(
                    f"[OCR benchmark] {result.version}: {result.error}"
                )

    @staticmethod
    def _write_report(results: list[BenchmarkResult]) -> None:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "scope": "EN/Global only",
            "dataset": DATASET_PREFIX,
            "recognition_only": True,
            "detector_tested": False,
            "speed_iterations": SPEED_ITERATIONS,
            "results": [asdict(result) for result in results],
        }
        REPORT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info(f"[OCR benchmark] JSON-отчёт: {REPORT_PATH}")

    def run(self) -> list[BenchmarkResult]:
        logger.hr("Сравнительный benchmark английских OCR-моделей", level=1)
        logger.info(
            "[OCR benchmark] Проверяются только модели EN/Global. "
            "Китайские и японские модели исключены из персонального форка."
        )

        with self._prepared_dataset() as test_cases:
            logger.info(
                f"[OCR benchmark] Загружено примеров: {len(test_cases)}; "
                f"набор: {DATASET_PREFIX}"
            )
            results = [
                self._run_candidate(candidate, test_cases)
                for candidate in self._model_candidates()
            ]

        self._render_summary(results)
        self._write_report(results)
        return results

    def _configured_candidate(self, backend: str) -> EnglishModelCandidate:
        candidates = self._model_candidates()
        if backend == "ncnn":
            return next(candidate for candidate in candidates if candidate.backend == "ncnn")

        from module.ocr.al_ocr import DEFAULT_ONNX_MODEL_VERSION

        version = self.config.ocr_model_version("azur_lane")
        if version == "auto":
            version = DEFAULT_ONNX_MODEL_VERSION["azur_lane"]
        return next(candidate for candidate in candidates if candidate.version == version)

    def run_simple_ocr_benchmark(self) -> str:
        """Проверяет выбранную модель и возвращает безопасное устройство OCR."""
        logger.hr("Быстрый benchmark OCR", level=1)
        backend = self.config.ocr_backend
        candidate = self._configured_candidate(backend)

        if backend == "ncnn":
            from module.ocr.ncnn_ocr import has_ncnn_vulkan_gpu

            if not has_ncnn_vulkan_gpu():
                logger.info(
                    "[OCR benchmark] Vulkan GPU для NCNN не найден; используется CPU"
                )
                return "cpu"
            device = "gpu"
        elif sys.platform == "darwin" and platform.machine() == "arm64":
            device = "ane"
        else:
            device = "gpu"

        with self._prepared_dataset() as test_cases:
            original_device = self.config.Optimization_OcrDevice
            try:
                self.config.override(Optimization_OcrDevice=device)
                result = self._run_candidate(candidate, test_cases)
            finally:
                self.config.override(Optimization_OcrDevice=original_device)

        if result.accuracy == 100.0:
            logger.info(
                f"[OCR benchmark] {candidate.version} на {device.upper()} прошла "
                "с точностью 100%"
            )
            return device
        logger.info(
            f"[OCR benchmark] {candidate.version} на {device.upper()} не достигла "
            "100%; используется CPU fallback"
        )
        return "cpu"


def run_ocr_benchmark(config) -> bool:
    try:
        OcrBenchmark(config, task="OcrBenchmark").run()
        return True
    except RequestHumanTakeover:
        logger.critical("[Daemon] Ошибка OCR требует ручного вмешательства")
        return False
