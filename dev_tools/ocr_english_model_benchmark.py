"""CLI для сравнительного benchmark английских OCR-моделей."""

from __future__ import annotations

import argparse

from module.config.config import AzurLaneConfig
from module.daemon.ocr_benchmark import OcrBenchmark, REPORT_PATH

SUPPORTED_DEVICES = (
    "auto",
    "qnn_npu",
    "openvino_npu",
    "openvino_gpu",
    "gpu",
    "openvino_cpu",
    "cpu",
    "ane",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Сравнить все установленные OCR-модели для EN/Global Azur Lane "
            "на одном наборе sets_num."
        )
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Имя файла config/<profile>.json без расширения.",
    )
    parser.add_argument(
        "--device",
        choices=SUPPORTED_DEVICES,
        help=(
            "Временное устройство для ONNX-моделей. По умолчанию используется "
            "текущая настройка профиля."
        ),
    )
    args = parser.parse_args(argv)

    config = AzurLaneConfig(args.profile, task="OcrBenchmark")
    if args.device is not None:
        config.override(Optimization_OcrDevice=args.device)

    results = OcrBenchmark(config).run()
    completed = [
        result
        for result in results
        if result.status not in {"ОШИБКА", "НЕ УСТАНОВЛЕНА"}
    ]
    print(f"JSON-отчёт: {REPORT_PATH}")
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
