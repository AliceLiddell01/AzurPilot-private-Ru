"""Сравнительный benchmark OCR-моделей для EN/Global Azur Lane.

Все установленные английские модели проверяются на одном наборе ``sets_num``.
Benchmark измеряет распознавание уже вырезанных строк; детектор текста и полные
игровые экраны этим набором не оцениваются.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import cv2
from rich.table import Table
from rich.text import Text

from module.config.config import AzurLaneConfig
from module.exception import RequestHumanTakeover
from module.logger import logger
from module.ocr.al_ocr import AlOcr
from module.ocr.ocr import normalize_ocr_text

REPORT_PATH = Path("artifacts/ocr/english-model-benchmark.json")
SPEED_ITERATIONS = 100
WARMUP_ITERATIONS = 3
MAX_REPORTED_MISMATCHES = 10


class OcrBenchmark:
    """Сравнивает только модели английского EN/Global-контура."""

    # model, version, fixture archive prefix, extracted subfolder
    BENCHMARKS = [
        ("azur_lane", "alocr_en_900k", "sets_num", "sets_num"),
        ("azur_lane", "azur_lane_v6_6", "sets_num", "sets_num"),
        ("azur_lane", "azur_lane_v6_5", "sets_num", "sets_num"),
        ("azur_lane", "ppocr_v6", "sets_num", "sets_num"),
        ("azur_lane", "alocr_en_v2_6", "sets_num", "sets_num"),
        ("azur_lane", "alocr_en_v2_0", "sets_num", "sets_num"),
        ("azur_lane", "alocr_en_v1_0", "sets_num", "sets_num"),
    ]

    def __init__(self, config, device=None, task=None):
        if isinstance(config, AzurLaneConfig):
            self.config = config
            if task is not None:
                self.config.init_task(task)
        else:
            self.config = AzurLaneConfig(config, task=task)
        self.device = device

    @staticmethod
    def _find_archive(prefix):
        for extension in (".zip", ".tar", ".tar.xz", ".tar.gz"):
            path = Path("module/daemon") / f"{prefix}{extension}"
            if path.is_file():
                return str(path)
        return None

    @staticmethod
    def _load_test_cases(extract_dir, subfolder):
        extract_root = Path(extract_dir)
        validation = extract_root / "val.txt"
        if not validation.is_file():
            validation = extract_root / subfolder / "val.txt"
        test_cases = []
        if validation.is_file():
            validation_root = validation.parent
            for line in validation.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                image_path = validation_root / parts[0]
                if not image_path.is_file():
                    image_path = validation_root / "imgs" / parts[0]
                test_cases.append((str(image_path), parts[1]))
        return test_cases

    @staticmethod
    def _rate_speed(avg_ms):
        if avg_ms < 5.0:
            return "Экстремально быстро", "bold bright_green"
        if avg_ms < 10.0:
            return "Очень быстро", "bright_green"
        if avg_ms < 20.0:
            return "Быстро", "green1"
        if avg_ms < 40.0:
            return "Достаточно быстро", "yellow"
        if avg_ms < 80.0:
            return "Средне", "orange1"
        if avg_ms < 150.0:
            return "Медленно", "bright_red"
        if avg_ms < 300.0:
            return "Очень медленно", "red"
        return "Критически медленно", "bold red"

    @staticmethod
    def _model_metadata(model_name: str, model_version: str) -> dict[str, Any]:
        from module.ocr import al_ocr

        if model_version in {"ncnn", "ncnn_azur_lane"}:
            from module.ocr.ncnn_ocr import MODEL_SPECS

            spec = MODEL_SPECS[model_name]
            return {
                "model_paths": [str(spec.param_path), str(spec.bin_path)],
                "dictionary_path": str(spec.keys_path),
                "ocr_version": "NCNN CTC",
            }

        custom_path = al_ocr.CUSTOM_CTC_MODEL_PARAMS.get(model_name, {}).get(
            model_version
        )
        if custom_path is not None:
            return {
                "model_paths": [str(custom_path)],
                "dictionary_path": "<embedded:ALAS_CTC_CHARSET>",
                "ocr_version": "CNN-CTC",
            }
        model_path, dictionary_path, ocr_version = al_ocr.ONNX_MODEL_PARAMS[
            model_name
        ][model_version]
        return {
            "model_paths": [str(model_path)],
            "dictionary_path": str(dictionary_path),
            "ocr_version": getattr(ocr_version, "value", str(ocr_version)),
        }

    @staticmethod
    def _missing_files(metadata: dict[str, Any]) -> list[str]:
        paths = [Path(path) for path in metadata["model_paths"]]
        dictionary_path = metadata["dictionary_path"]
        if dictionary_path and not str(dictionary_path).startswith("<embedded:"):
            paths.append(Path(dictionary_path))
        return [str(path) for path in paths if not path.is_file()]

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    @staticmethod
    def _runtime_name(ocr: AlOcr, backend: str) -> str:
        model = ocr.model
        if backend == "ncnn":
            if getattr(model, "use_vulkan", False):
                gpu_index = getattr(model, "gpu_index", 0)
                return f"Vulkan GPU {gpu_index}"
            return "NCNN CPU"

        session = getattr(model, "session", None)
        if session is None:
            text_rec = getattr(model, "text_rec", None)
            wrapper = getattr(text_rec, "session", None)
            session = getattr(wrapper, "session", wrapper)
        if session is not None and hasattr(session, "get_providers"):
            try:
                providers = session.get_providers()
            except Exception:
                providers = []
            if providers:
                return ", ".join(str(provider) for provider in providers)
        return "ONNX Runtime"

    @staticmethod
    def _result_status(accuracy: float) -> str:
        if accuracy == 100.0:
            return "ИДЕАЛЬНО"
        if accuracy >= 99.0:
            return "ХОРОШО"
        if accuracy >= 95.0:
            return "РИСК"
        return "НЕ ПОДХОДИТ"

    @staticmethod
    def _status_text(result: dict[str, Any]) -> Text:
        styles = {
            "ИДЕАЛЬНО": "bold bright_green",
            "ХОРОШО": "green",
            "РИСК": "bold yellow",
            "НЕ ПОДХОДИТ": "bold red",
            "НЕ УСТАНОВЛЕНА": "dim",
            "ОШИБКА": "bold red",
        }
        label = result["status"]
        if result.get("recommended"):
            label = f"РЕКОМЕНДУЕТСЯ · {label}"
        return Text(label, style=styles.get(result["status"], "white"))

    def _resolve_device(self, backend: str, requested: str | None) -> str:
        if requested is None:
            requested = str(
                getattr(self.config, "Optimization_OcrDevice", "auto")
            )
        if backend != "ncnn":
            return requested
        if requested == "gpu":
            return "gpu"
        if requested == "auto":
            from module.ocr.ncnn_ocr import has_ncnn_vulkan_gpu

            return "gpu" if has_ncnn_vulkan_gpu() else "cpu"
        return "cpu"

    def _run_single(
        self,
        model_name,
        model_version,
        dataset_prefix,
        subfolder,
        use_gpu=None,
        ocr_device=None,
        inference_count=SPEED_ITERATIONS,
        backend_override=None,
    ):
        from module.ocr import al_ocr

        if model_name != "azur_lane":
            raise ValueError(
                f"Benchmark поддерживает только глобальную английскую модель: {model_name}"
            )
        if ocr_device is None and use_gpu is not None:
            ocr_device = "gpu" if use_gpu else "cpu"
        backend = backend_override or str(self.config.ocr_backend)
        if backend == "auto":
            backend = "onnxruntime"
        if backend not in {"onnxruntime", "ncnn"}:
            raise ValueError(f"Неподдерживаемый backend OCR: {backend}")
        ocr_device = self._resolve_device(backend, ocr_device)
        if inference_count < 1:
            raise ValueError(
                "Количество benchmark inference должно быть положительным."
            )

        metadata = self._model_metadata(model_name, model_version)
        model_files = [Path(path).name for path in metadata["model_paths"]]
        base_result = {
            "model": model_name,
            "model_version": model_version,
            "model_paths": metadata["model_paths"],
            "model_files": model_files,
            "dictionary_path": metadata["dictionary_path"],
            "dictionary_file": (
                Path(metadata["dictionary_path"]).name
                if metadata["dictionary_path"]
                and not str(metadata["dictionary_path"]).startswith("<embedded:")
                else metadata["dictionary_path"]
            ),
            "ocr_version": metadata["ocr_version"],
            "dataset": dataset_prefix,
            "backend": backend,
            "device_requested": ocr_device,
            "runtime": "не определено",
            "accuracy": 0.0,
            "correct": 0,
            "total": 0,
            "errors": 0,
            "runtime_errors": 0,
            "load_ms": 0.0,
            "avg_ms": None,
            "p95_ms": None,
            "inference_count": 0,
            "mismatches": [],
            "status": "ОШИБКА",
            "error": None,
            "recommended": False,
        }

        missing = self._missing_files(metadata)
        if missing:
            base_result["status"] = "НЕ УСТАНОВЛЕНА"
            base_result["error"] = "Отсутствуют файлы: " + ", ".join(missing)
            return base_result

        logger.hr(
            f"Benchmark OCR: {model_version} / {backend} / {ocr_device}",
            level=2,
        )
        logger.info(
            "[OCR benchmark] Версия: %s; файлы: %s; словарь: %s; набор: %s",
            model_version,
            ", ".join(metadata["model_paths"]),
            metadata["dictionary_path"],
            dataset_prefix,
        )

        archive_path = self._find_archive(dataset_prefix)
        original_global_config = al_ocr.config
        original_values = {
            "Optimization_OcrBackend": getattr(
                self.config, "Optimization_OcrBackend", "auto"
            ),
            "Optimization_OcrDevice": getattr(
                self.config, "Optimization_OcrDevice", "auto"
            ),
            "Optimization_OcrModelVersionEnglish": getattr(
                self.config, "Optimization_OcrModelVersionEnglish", "auto"
            ),
            "Optimization_OcrWindowsMlVendorEp": getattr(
                self.config, "Optimization_OcrWindowsMlVendorEp", False
            ),
        }
        overrides = {
            "Optimization_OcrBackend": backend,
            "Optimization_OcrDevice": ocr_device,
            "Optimization_OcrWindowsMlVendorEp": False,
        }
        if backend == "onnxruntime":
            overrides["Optimization_OcrModelVersionEnglish"] = model_version

        try:
            self.config.override(**overrides)
            al_ocr.config = self.config
            al_ocr.reset_ocr_model()

            load_started = time.perf_counter()
            ocr = AlOcr(name=model_name)
            ocr.init()
            base_result["load_ms"] = (
                time.perf_counter() - load_started
            ) * 1000
            base_result["runtime"] = self._runtime_name(ocr, backend)

            with tempfile.TemporaryDirectory(
                prefix=f"azurpilot-{dataset_prefix}-"
            ) as extract_dir:
                if archive_path:
                    logger.info(f"[OCR benchmark] Распаковка {archive_path}...")
                    shutil.unpack_archive(archive_path, extract_dir)

                test_cases = self._load_test_cases(extract_dir, subfolder)
                if not test_cases:
                    raise FileNotFoundError(
                        f"Не удалось загрузить набор {dataset_prefix}: "
                        "отсутствует val.txt или изображения"
                    )

                total = len(test_cases)
                correct = 0
                runtime_errors = 0
                progress_step = max(1, total // 10)
                for index, (image_input, expected) in enumerate(test_cases, 1):
                    try:
                        actual = normalize_ocr_text(
                            model_name, ocr.ocr(image_input)
                        )
                    except Exception as exc:
                        runtime_errors += 1
                        actual = f"<OCR ERROR: {type(exc).__name__}>"
                    if actual.strip().upper() == expected.strip().upper():
                        correct += 1
                    elif (
                        len(base_result["mismatches"])
                        < MAX_REPORTED_MISMATCHES
                    ):
                        base_result["mismatches"].append(
                            {
                                "image": os.path.basename(image_input),
                                "expected": expected,
                                "actual": actual,
                            }
                        )
                    if index % progress_step == 0 or index == total:
                        logger.info(
                            f"[{model_version}] Точность: {index}/{total}"
                        )

                accuracy = correct / total * 100 if total else 0.0
                base_result.update(
                    {
                        "accuracy": accuracy,
                        "correct": correct,
                        "total": total,
                        "errors": total - correct,
                        "runtime_errors": runtime_errors,
                    }
                )

                benchmark_image = cv2.imread(test_cases[0][0])
                if benchmark_image is None:
                    raise RuntimeError(
                        "OpenCV не смог загрузить изображение для benchmark."
                    )
                for _ in range(WARMUP_ITERATIONS):
                    ocr.ocr(benchmark_image)

                samples = []
                for _ in range(inference_count):
                    started = time.perf_counter()
                    ocr.ocr(benchmark_image)
                    samples.append((time.perf_counter() - started) * 1000)

                avg_ms = sum(samples) / len(samples)
                p95_ms = self._percentile(samples, 0.95)
                rating, rating_color = self._rate_speed(avg_ms)
                base_result.update(
                    {
                        "avg_ms": avg_ms,
                        "p95_ms": p95_ms,
                        "inference_count": len(samples),
                        "rating": rating,
                        "rating_color": rating_color,
                        "status": self._result_status(accuracy),
                    }
                )
                logger.info(
                    f"[{model_version}] Точность: {accuracy:.2f}% "
                    f"({correct}/{total}); среднее: {avg_ms:.3f} мс; "
                    f"p95: {p95_ms:.3f} мс"
                )
        except Exception as exc:
            base_result["status"] = "ОШИБКА"
            base_result["error"] = f"{type(exc).__name__}: {exc}"
            logger.error(
                f"[OCR benchmark] {model_version} завершилась ошибкой: "
                f"{base_result['error']}"
            )
        finally:
            try:
                al_ocr.release_ocr_models()
            finally:
                self.config.override(**original_values)
                al_ocr.config = original_global_config

        return base_result

    @staticmethod
    def _rank_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for result in results:
            result["recommended"] = False
        ranked = [
            result
            for result in results
            if result["status"] not in {"ОШИБКА", "НЕ УСТАНОВЛЕНА"}
        ]
        ranked.sort(
            key=lambda item: (
                -item["accuracy"],
                item["errors"],
                item["p95_ms"],
                item["avg_ms"],
                item["load_ms"],
                item["model_version"],
            )
        )
        if ranked:
            ranked[0]["recommended"] = True
        return ranked

    @staticmethod
    def _write_report(results: list[dict[str, Any]]) -> None:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "scope": "EN/Global only",
            "dataset": "sets_num",
            "recognition_only": True,
            "detector_tested": False,
            "speed_iterations": SPEED_ITERATIONS,
            "generated_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "opencv": cv2.__version__,
            },
            "results": results,
        }
        REPORT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info(f"[OCR benchmark] JSON-отчёт: {REPORT_PATH}")

    def run(self):
        logger.hr("Сравнительный benchmark английских OCR-моделей", level=1)
        results = []
        for model_name, model_version, dataset_prefix, subfolder in self.BENCHMARKS:
            result = self._run_single(
                model_name,
                model_version,
                dataset_prefix,
                subfolder,
                backend_override="onnxruntime",
            )
            if result:
                results.append(result)

        from module.ocr.ncnn_ocr import MODEL_SPECS

        if "azur_lane" in MODEL_SPECS:
            results.append(
                self._run_single(
                    "azur_lane",
                    "ncnn_azur_lane",
                    "sets_num",
                    "sets_num",
                    backend_override="ncnn",
                )
            )

        if not results:
            logger.error("[OCR benchmark] Результаты benchmark не получены")
            return []

        ranked = self._rank_results(results)
        rank_by_version = {
            result["model_version"]: index
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
            avg = (
                f"{result['avg_ms']:.3f} мс"
                if result["avg_ms"] is not None
                else "—"
            )
            p95 = (
                f"{result['p95_ms']:.3f} мс"
                if result["p95_ms"] is not None
                else "—"
            )
            accuracy = (
                f"{result['accuracy']:.2f}% "
                f"({result['correct']}/{result['total']})"
                if result["total"]
                else "—"
            )
            table.add_row(
                str(rank_by_version.get(result["model_version"], "—")),
                result["model_version"],
                result["backend"],
                result["runtime"],
                ", ".join(result["model_files"]),
                accuracy,
                str(result["errors"]) if result["total"] else "—",
                avg,
                p95,
                self._status_text(result),
            )

        logger.hr("Сводка сравнительного benchmark EN OCR", level=1)
        logger.print(table, justify="center")
        logger.info(
            "[OCR benchmark] Проверяется распознавание готовых кропов "
            "на одном наборе sets_num; детектор текста не оценивается."
        )
        if ranked:
            winner = ranked[0]
            logger.info(
                f"[OCR benchmark] Рекомендуемая модель: "
                f"{winner['model_version']} — "
                f"{winner['accuracy']:.2f}%, "
                f"среднее {winner['avg_ms']:.3f} мс, "
                f"p95 {winner['p95_ms']:.3f} мс"
            )
        for result in results:
            if result["error"]:
                logger.warning(
                    f"[OCR benchmark] {result['model_version']}: "
                    f"{result['error']}"
                )

        self._write_report(results)
        return results

    def run_simple_ocr_benchmark(self):
        backend = str(self.config.ocr_backend)
        model_version = str(self.config.ocr_model_version("azur_lane"))
        if model_version == "auto":
            from module.ocr.al_ocr import DEFAULT_ONNX_MODEL_VERSION

            model_version = DEFAULT_ONNX_MODEL_VERSION["azur_lane"]

        if backend == "ncnn":
            from module.ocr.ncnn_ocr import has_ncnn_vulkan_gpu

            device = "gpu" if has_ncnn_vulkan_gpu() else "cpu"
        elif sys.platform == "darwin" and platform.machine() == "arm64":
            device = "ane"
        else:
            device = "gpu"

        result = self._run_single(
            "azur_lane",
            model_version,
            "sets_num",
            "sets_num",
            ocr_device=device,
        )
        if result and result["accuracy"] >= 100.0:
            logger.info(
                f"[OCR benchmark] {model_version} через {device.upper()} имеет точность 100%"
            )
            return device
        logger.info(
            f"[OCR benchmark] {model_version} через {device.upper()} не прошёл; используется CPU"
        )
        return "cpu"


def run_ocr_benchmark(config):
    try:
        OcrBenchmark(config, task="OcrBenchmark").run()
        return True
    except RequestHumanTakeover:
        logger.critical("[Daemon] Ошибка OCR требует ручного вмешательства")
        return False
