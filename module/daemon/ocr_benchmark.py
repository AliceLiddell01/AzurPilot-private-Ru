"""Benchmark Global/English OCR models with explicit reproducibility metadata."""

from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from pathlib import Path

import cv2
from rich.table import Table
from rich.text import Text

from module.config.config import AzurLaneConfig
from module.exception import RequestHumanTakeover
from module.logger import logger
from module.ocr.al_ocr import AlOcr
from module.ocr.ocr import normalize_ocr_text


class OcrBenchmark:
    """Compare only English models supported by the personal EN/Global build."""

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
    def _model_metadata(model_name: str, model_version: str) -> dict[str, str]:
        from module.ocr import al_ocr

        custom_path = al_ocr.CUSTOM_CTC_MODEL_PARAMS.get(model_name, {}).get(model_version)
        if custom_path is not None:
            return {
                "model_path": custom_path,
                "dictionary_path": "<embedded:ALAS_CTC_CHARSET>",
                "ocr_version": "CNN-CTC",
            }
        model_path, dictionary_path, ocr_version = al_ocr.ONNX_MODEL_PARAMS[model_name][model_version]
        return {
            "model_path": str(model_path),
            "dictionary_path": str(dictionary_path),
            "ocr_version": str(ocr_version),
        }

    def _run_single(
        self,
        model_name,
        model_version,
        dataset_prefix,
        subfolder,
        use_gpu=None,
        ocr_device=None,
        inference_count=100,
    ):
        from module.ocr import al_ocr

        if model_name != "azur_lane":
            raise ValueError(f"Benchmark поддерживает только глобальную английскую модель: {model_name}")
        if ocr_device is None and use_gpu is not None:
            ocr_device = "gpu" if use_gpu else "cpu"
        if ocr_device is None:
            ocr_device = str(self.config.ocr_device)
        if ocr_device == "auto":
            ocr_device = "gpu"
        if inference_count < 1:
            raise ValueError("Количество benchmark inference должно быть положительным.")

        metadata = self._model_metadata(model_name, model_version)
        backend = str(self.config.ocr_backend)
        if backend == "auto":
            backend = "onnx"
        logger.hr(
            "Benchmark OCR: "
            f"{model_name} / {model_version} / {backend} / {ocr_device}",
            level=2,
        )
        logger.info(
            "[OCR benchmark] Модель: %s; файл: %s; словарь: %s; dataset: %s",
            model_version,
            metadata["model_path"],
            metadata["dictionary_path"],
            dataset_prefix,
        )

        self.config.override(
            Optimization_OcrDevice=ocr_device,
            Optimization_OcrModelVersionEnglish=model_version,
            Optimization_OcrWindowsMlVendorEp=False,
        )
        archive_path = self._find_archive(dataset_prefix)
        extract_dir = Path("module/daemon") / f"{dataset_prefix}_{model_version}_temp"
        original_config = al_ocr.config
        al_ocr.config = self.config
        try:
            al_ocr.reset_ocr_model()
            ocr = AlOcr(name=model_name)
            ocr.init()
            if archive_path:
                logger.info(f"[OCR benchmark] Распаковка {archive_path}...")
                if extract_dir.exists():
                    shutil.rmtree(extract_dir)
                shutil.unpack_archive(archive_path, extract_dir)

            test_cases = self._load_test_cases(extract_dir, subfolder)
            if not test_cases:
                logger.error(
                    f"[{model_version}] Не удалось загрузить тестовые примеры; набор пропущен"
                )
                return None

            logger.info(f"[{model_version}] Загружено примеров: {len(test_cases)}")
            correct = 0
            errors = 0
            total = len(test_cases)
            log_step = max(1, total // 20)
            for index, (image_input, expected) in enumerate(test_cases, 1):
                try:
                    result = normalize_ocr_text(model_name, ocr.ocr(image_input))
                    if result.strip().upper() == expected.strip().upper():
                        correct += 1
                    else:
                        logger.warning(
                            "Ошибка [%s]: ожидалось \"%s\", получено \"%s\"",
                            os.path.basename(image_input),
                            expected,
                            result,
                        )
                except Exception as exc:
                    errors += 1
                    logger.error(f"[{model_version}] Ошибка OCR для {image_input}: {exc}")
                if index % log_step == 0 or index == total:
                    logger.info(
                        f"[{model_version}] Прогресс точности: "
                        f"{index}/{total} ({index / total * 100:.0f}%)"
                    )

            accuracy = correct / total * 100 if total else 0.0
            benchmark_image = cv2.imread(test_cases[0][0])
            if benchmark_image is None:
                raise RuntimeError("OpenCV не смог загрузить изображение для benchmark.")
            logger.info(f"[{model_version}] Прогрев...")
            for _ in range(3):
                ocr.ocr(benchmark_image)

            logger.info(f"[{model_version}] Inference: {inference_count}")
            completed_count = 0
            started = time.perf_counter()
            for index in range(1, inference_count + 1):
                try:
                    ocr.ocr(benchmark_image)
                    completed_count += 1
                except Exception as exc:
                    logger.error(f"[{model_version}] Ошибка на итерации {index}: {exc}")
                    break
            elapsed = time.perf_counter() - started
            avg_ms = elapsed * 1000 / completed_count if completed_count else float("inf")
            rating, rating_color = self._rate_speed(avg_ms)
            logger.info(
                f"[{model_version}] Точность: {accuracy:.2f}% ({correct}/{total}); "
                f"ошибок выполнения: {errors}; среднее: {avg_ms:.3f} мс; {rating}"
            )
            return {
                "model": model_name,
                "model_version": model_version,
                "model_path": metadata["model_path"],
                "dictionary_path": metadata["dictionary_path"],
                "ocr_version": metadata["ocr_version"],
                "dataset": dataset_prefix,
                "backend": backend,
                "device": ocr_device,
                "accuracy": accuracy,
                "correct": correct,
                "total": total,
                "errors": errors,
                "inference_count": completed_count,
                "cost": elapsed,
                "avg_ms": avg_ms,
                "rating": rating,
                "rating_color": rating_color,
            }
        finally:
            try:
                al_ocr.release_ocr_models()
            finally:
                al_ocr.config = original_config
            if extract_dir.exists():
                shutil.rmtree(extract_dir)

    def run(self):
        logger.hr("Benchmark Global/English OCR", level=1)
        results = []
        for model_name, model_version, dataset_prefix, subfolder in self.BENCHMARKS:
            result = self._run_single(
                model_name,
                model_version,
                dataset_prefix,
                subfolder,
            )
            if result:
                results.append(result)

        if not results:
            logger.error("[OCR benchmark] Результаты benchmark не получены")
            return []

        table = Table(show_lines=True)
        table.add_column("Модель", style="cyan", no_wrap=True)
        table.add_column("Версия", style="bright_cyan", no_wrap=True)
        table.add_column("Файл")
        table.add_column("Backend")
        table.add_column("Устройство")
        table.add_column("Точность", justify="right")
        table.add_column("Среднее", justify="right")
        table.add_column("Статус", justify="center")
        for result in results:
            accuracy = result["accuracy"]
            if accuracy >= 100.0:
                status = Text("ПРОЙДЕНО", style="bold bright_green")
            elif accuracy >= 90.0:
                status = Text("ПРЕДУПРЕЖДЕНИЕ", style="bold yellow")
            else:
                status = Text("ОШИБКА", style="bold red")
            table.add_row(
                result["model"],
                result["model_version"],
                Path(result["model_path"]).name,
                result["backend"],
                result["device"],
                f"{accuracy:.2f}% ({result['correct']}/{result['total']})",
                f"{result['avg_ms']:.3f} мс",
                status,
            )
        logger.hr("Сводка benchmark Global/English OCR", level=1)
        logger.print(table, justify="center")
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
