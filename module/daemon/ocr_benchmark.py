"""Benchmark точности и скорости поддерживаемых OCR-моделей."""

import os
import platform
import shutil
import sys
import time

import cv2
from rich.table import Table
from rich.text import Text

from module.config.config import AzurLaneConfig
from module.exception import RequestHumanTakeover
from module.logger import logger
from module.ocr.al_ocr import AlOcr
from module.ocr.stage8b_runtime import install_stage8b_runtime_patches

install_stage8b_runtime_patches()


class OcrBenchmark:
    BENCHMARKS = [
        ('azur_lane', 'sets_num', 'sets_num'),
        ('azur_lane_jp', 'sets_azur_lane_jp', 'azur_lane_jp'),
        ('cn', 'sets_zhcn', 'sets_zhcn'),
    ]

    def __init__(self, config, device=None, task=None):
        if isinstance(config, AzurLaneConfig):
            self.config = config
            if task is not None:
                self.config.init_task(task)
        else:
            self.config = AzurLaneConfig(config, task=task)

    def _find_archive(self, prefix):
        for extension in ['.zip', '.tar', '.tar.xz', '.tar.gz']:
            path = f'module/daemon/{prefix}{extension}'
            if os.path.exists(path):
                return path
        return None

    def _load_test_cases(self, extract_dir, subfolder):
        target_val_txt = os.path.join(extract_dir, 'val.txt')
        if not os.path.exists(target_val_txt):
            target_val_txt = os.path.join(extract_dir, subfolder, 'val.txt')
        test_cases = []
        if os.path.exists(target_val_txt):
            val_root = os.path.dirname(target_val_txt)
            with open(target_val_txt, 'r', encoding='utf-8') as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        image_path = os.path.join(val_root, parts[0])
                        if not os.path.exists(image_path):
                            image_path = os.path.join(val_root, 'imgs', parts[0])
                        test_cases.append((image_path, parts[1]))
        return test_cases

    @staticmethod
    def _rate_speed(avg_ms):
        if avg_ms < 5.0:
            return 'Экстремально быстро', 'bold bright_green'
        if avg_ms < 10.0:
            return 'Очень быстро', 'bright_green'
        if avg_ms < 20.0:
            return 'Быстро', 'green1'
        if avg_ms < 40.0:
            return 'Достаточно быстро', 'yellow'
        if avg_ms < 80.0:
            return 'Средне', 'orange1'
        if avg_ms < 150.0:
            return 'Медленно', 'bright_red'
        if avg_ms < 300.0:
            return 'Очень медленно', 'red'
        return 'Критически медленно', 'bold red'

    def _run_single(self, model_name, dataset_prefix, subfolder, use_gpu=None, ocr_device=None):
        logger.hr(
            f'Benchmark: модель {model_name.upper()} | набор данных {dataset_prefix}',
            level=2,
        )

        if ocr_device is None and use_gpu is not None:
            ocr_device = 'gpu' if use_gpu else 'cpu'
        if ocr_device is not None:
            self.config.override(Optimization_OcrDevice=ocr_device)
            from module.ocr.al_ocr import reset_ocr_model
            reset_ocr_model()

        ocr = AlOcr(name=model_name)
        ocr.init()

        archive_path = self._find_archive(dataset_prefix)
        extract_dir = f'module/daemon/{dataset_prefix}_temp'

        try:
            if archive_path:
                logger.info(f'[OCR benchmark] Распаковка {archive_path}...')
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir)
                shutil.unpack_archive(archive_path, extract_dir)

            test_cases = self._load_test_cases(extract_dir, subfolder)
            if not test_cases:
                logger.error(
                    f'[{model_name}] Не удалось загрузить тестовые примеры; набор пропущен'
                )
                return None

            logger.info(
                f'[{model_name}] Загружено тестовых примеров: {len(test_cases)}'
            )

            correct = 0
            total = len(test_cases)
            log_step = max(1, total // 20)

            for index, (image_input, expected) in enumerate(test_cases, 1):
                try:
                    result = ocr.ocr(image_input)
                    if result.strip().upper() == expected.strip().upper():
                        correct += 1
                    else:
                        name = os.path.basename(image_input)
                        logger.warning(
                            f'Ошибка [{name}]: ожидалось "{expected}", получено "{result}"'
                        )
                except Exception as exc:
                    logger.error(
                        f'[{model_name}] Ошибка OCR для {image_input}: {exc}'
                    )

                if index % log_step == 0 or index == total:
                    percent = index / total * 100
                    logger.info(
                        f'[{model_name}] Прогресс точности: {index}/{total} ({percent:.0f}%)'
                    )

            accuracy = (correct / total) * 100 if total > 0 else 0
            if accuracy >= 100.0:
                accuracy_color = 'bright_green'
            elif accuracy >= 90.0:
                accuracy_color = 'yellow'
            else:
                accuracy_color = 'red'

            logger.info(
                f"[{model_name}] Точность: [{accuracy_color}]{accuracy:.2f}% "
                f"({correct}/{total})[/{accuracy_color}]",
                extra={"markup": True},
            )

            benchmark_image = cv2.imread(test_cases[0][0])
            count = 100

            logger.info(f'[{model_name}] Прогрев...')
            for _ in range(3):
                ocr.ocr(benchmark_image)

            logger.info(f'[{model_name}] Запуск выводов модели: {count}')
            start = time.time()
            for iteration in range(1, count + 1):
                try:
                    ocr.ocr(benchmark_image)
                except Exception as exc:
                    logger.error(
                        f'[{model_name}] Ошибка на итерации {iteration}: {exc}'
                    )
                    break
                if iteration % 5 == 0 or iteration == count:
                    logger.info(
                        f'[{model_name}] Прогресс скорости: {iteration}/{count}'
                    )

            cost = time.time() - start
            avg_ms = cost * 1000 / count if cost > 0 else 0
            rating, rating_color = self._rate_speed(avg_ms)

            logger.info(
                f"[{model_name}] Выводов: {count}; время: {cost:.3f} с; "
                f"среднее: {avg_ms:.3f} мс; [{rating_color}]{rating}[/{rating_color}]",
                extra={"markup": True},
            )

            return {
                'model': model_name,
                'dataset': dataset_prefix,
                'accuracy': accuracy,
                'correct': correct,
                'total': total,
                'cost': cost,
                'avg_ms': avg_ms,
                'rating': rating,
                'rating_color': rating_color,
                'acc_color': accuracy_color,
            }
        finally:
            if os.path.exists(extract_dir):
                try:
                    shutil.rmtree(extract_dir)
                except Exception as exc:
                    logger.error(
                        f'[OCR benchmark] Не удалось очистить {extract_dir}: {exc}'
                    )

    def run(self):
        logger.hr('Benchmark OCR', level=1)
        results = []
        for model_name, dataset_prefix, subfolder in self.BENCHMARKS:
            result = self._run_single(model_name, dataset_prefix, subfolder)
            if result:
                results.append(result)

        if not results:
            logger.hr('Сводка benchmark OCR', level=1)
            logger.error('[OCR benchmark] Результаты benchmark не получены')
            return

        table = Table(show_lines=True)
        table.add_column('Модель', header_style='bright_cyan', style='cyan', no_wrap=True)
        table.add_column('Набор данных', style='magenta')
        table.add_column('Точность', justify='right')
        table.add_column('Среднее время', justify='right')
        table.add_column('Оценка скорости')
        table.add_column('Статус', justify='center')

        for result in results:
            accuracy = result['accuracy']
            if accuracy >= 100.0:
                status = Text('ПРОЙДЕНО', style='bold bright_green')
            elif accuracy >= 90.0:
                status = Text('ПРЕДУПРЕЖДЕНИЕ', style='bold yellow')
            else:
                status = Text('ОШИБКА', style='bold red')

            table.add_row(
                result['model'].upper(),
                result['dataset'],
                Text(
                    f"{accuracy:.2f}% ({result['correct']}/{result['total']})",
                    style=result['acc_color'],
                ),
                f"{result['avg_ms']:.3f} мс",
                Text(result['rating'], style=result['rating_color']),
                status,
            )

        logger.hr('Сводка benchmark OCR', level=1)
        logger.print(table, justify='center')
        logger.info(
            '[Daemon] Если статус содержит ОШИБКА или ПРЕДУПРЕЖДЕНИЕ, '
            'используйте CPU для OCR'
        )

    def run_simple_ocr_benchmark(self):
        logger.hr('Быстрый benchmark OCR', level=1)
        backend = self.config.ocr_backend
        logger.info(f'[OCR benchmark] Backend: {backend}')

        if backend == 'ncnn':
            from module.ocr.ncnn_ocr import has_ncnn_vulkan_gpu
            if not has_ncnn_vulkan_gpu():
                logger.info('[OCR benchmark] Vulkan GPU для ncnn не найден; используется CPU')
                return 'cpu'
            logger.info('[OCR benchmark] Проверка OCR через ncnn Vulkan GPU...')
            device = 'gpu'
        else:
            if sys.platform == 'darwin' and platform.machine() == 'arm64':
                logger.info('[OCR benchmark] Проверка OCR через ANE...')
                device = 'ane'
            else:
                logger.info('[OCR benchmark] Проверка OCR через GPU DirectML...')
                device = 'gpu'

        result = self._run_single(
            'azur_lane',
            'sets_num',
            'sets_num',
            ocr_device=device,
        )

        if result and result['accuracy'] >= 100.0:
            logger.info(
                f'[OCR benchmark] Точность OCR через {device.upper()} равна 100%; '
                f'выбрано {device.upper()}'
            )
            return device

        logger.info(
            f'[OCR benchmark] Точность OCR через {device.upper()} ниже 100% '
            'или проверка завершилась ошибкой; используется CPU fallback'
        )
        return 'cpu'


def run_ocr_benchmark(config):
    try:
        OcrBenchmark(config, task='OcrBenchmark').run()
        return True
    except RequestHumanTakeover:
        logger.critical('[Daemon] Ошибка OCR требует ручного вмешательства')
        return False
