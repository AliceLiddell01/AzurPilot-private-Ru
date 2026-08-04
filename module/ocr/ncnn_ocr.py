"""NCNN backend для английской OCR-модели Azur Lane.

Персональный форк поддерживает только EN/Global. Китайские, японские и
традиционно-китайские NCNN-веса удалены из поставки и реестра.
"""

from __future__ import annotations

import atexit
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from rapidocr.ch_ppocr_rec.typings import TextRecOutput
from rapidocr.ch_ppocr_rec.utils import CTCLabelDecode
from rapidocr.utils.load_image import LoadImage
from rapidocr.utils.process_img import resize_image_within_bounds

from module.logger import logger

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = REPO_ROOT / "bin/ocr_models/ncnn"
REC_IMAGE_SHAPE = (3, 48, 320)
INPUT_NAME = "in0"
OUTPUT_NAME = "out0"


@dataclass(frozen=True)
class NcnnRecModelSpec:
    name: str
    param_path: Path
    bin_path: Path
    keys_path: Path
    output_name: str
    disable_fp16: bool = False


MODEL_SPECS = {
    "azur_lane": NcnnRecModelSpec(
        name="azur_lane",
        param_path=MODEL_ROOT / "azur_lane.param",
        bin_path=MODEL_ROOT / "azur_lane.bin",
        keys_path=REPO_ROOT / "bin/ocr_models/azur_lane/ppocrv6_azurlane_dict.txt",
        output_name=OUTPUT_NAME,
        disable_fp16=True,
    ),
}

MODEL_ALIASES = {
    "en": "azur_lane",
}

_ncnn = None
_ncnn_lock = threading.Lock()
_gpu_lock = threading.Lock()
_gpu_instance_created = False
_gpu_instance_registered = False


def normalize_model_name(name: str) -> str:
    return MODEL_ALIASES.get(name, name)


def supports_ncnn_model(name: str) -> bool:
    return normalize_model_name(name) in MODEL_SPECS


def _load_ncnn():
    global _ncnn
    if _ncnn is not None:
        return _ncnn

    with _ncnn_lock:
        if _ncnn is None:
            try:
                import ncnn
            except ImportError as exc:
                raise RuntimeError(
                    "Для OCR через NCNN требуется Python-пакет 'ncnn'."
                ) from exc
            _ncnn = ncnn
    return _ncnn


def _destroy_gpu_instance():
    """Безопасно освобождает глобальный экземпляр NCNN GPU при завершении."""
    global _gpu_instance_created, _gpu_instance_registered
    try:
        ncnn = _load_ncnn()
        destroy = getattr(ncnn, "destroy_gpu_instance", None)
        if destroy is not None and _gpu_instance_created:
            destroy()
            _gpu_instance_created = False
    except Exception:
        pass


def _ensure_gpu_instance(ncnn) -> None:
    global _gpu_instance_created, _gpu_instance_registered
    if _gpu_instance_created:
        return

    with _gpu_lock:
        if not _gpu_instance_created:
            create_gpu_instance = getattr(ncnn, "create_gpu_instance", None)
            if create_gpu_instance is not None:
                create_gpu_instance()
                if not _gpu_instance_registered:
                    atexit.register(_destroy_gpu_instance)
                    _gpu_instance_registered = True
            _gpu_instance_created = True


def get_ncnn_vulkan_gpu_count() -> int:
    ncnn = _load_ncnn()
    _ensure_gpu_instance(ncnn)

    get_gpu_count = getattr(ncnn, "get_gpu_count", None)
    if get_gpu_count is None:
        return 0
    return int(get_gpu_count())


def has_ncnn_vulkan_gpu() -> bool:
    try:
        return get_ncnn_vulkan_gpu_count() > 0
    except Exception as exc:
        logger.warning(f"Не удалось обнаружить NCNN Vulkan GPU: {exc}")
        return False


def _resolve_gpu_index(ncnn, requested_index: int) -> int:
    gpu_count = get_ncnn_vulkan_gpu_count()
    if gpu_count <= 0:
        raise RuntimeError("Запрошен NCNN Vulkan, но Vulkan GPU не обнаружен.")

    if requested_index < 0:
        get_default_gpu_index = getattr(ncnn, "get_default_gpu_index", None)
        requested_index = get_default_gpu_index() if get_default_gpu_index else 0

    if not 0 <= requested_index < gpu_count:
        raise RuntimeError(
            f"Индекс NCNN Vulkan GPU {requested_index} вне диапазона; "
            f"обнаружено устройств: {gpu_count}."
        )
    return requested_index


def _gpu_info_value(ncnn, gpu_index: int, name: str):
    try:
        info = ncnn.get_gpu_info(gpu_index)
        value = getattr(info, name)
        return value() if callable(value) else value
    except Exception:
        return None


class RecPreprocessor:
    def __init__(
        self,
        rec_image_shape: tuple[int, int, int] = REC_IMAGE_SHAPE,
    ):
        self.rec_image_shape = rec_image_shape

    def resize_norm_img(self, img: np.ndarray) -> np.ndarray:
        img_channel, img_height, img_width = self.rec_image_shape
        if img.shape[2] != img_channel:
            raise ValueError(
                f"Ожидалось каналов: {img_channel}; получено: {img.shape[2]}"
            )

        height, width = img.shape[:2]
        ratio = width / float(height)
        resized_width = min(img_width, int(math.ceil(img_height * ratio)))

        resized_image = cv2.resize(img, (resized_width, img_height))
        resized_image = resized_image.astype("float32")
        resized_image = resized_image.transpose((2, 0, 1)) / 255.0
        resized_image -= 0.5
        resized_image /= 0.5

        padding_image = np.zeros(
            (img_channel, img_height, img_width),
            dtype=np.float32,
        )
        padding_image[:, :, :resized_width] = resized_image
        return padding_image


class NcnnRecOCR:
    def __init__(
        self,
        model_name: str,
        device: str = "cpu",
        gpu_index: int = -1,
    ):
        normalized_name = normalize_model_name(model_name)
        if normalized_name not in MODEL_SPECS:
            raise ValueError(
                f"Неподдерживаемая NCNN OCR-модель: {model_name}. "
                "В персональном форке доступна только 'azur_lane'."
            )

        self.spec = MODEL_SPECS[normalized_name]
        self.device = device
        self.gpu_index = gpu_index
        self.use_vulkan = False
        self.ncnn = _load_ncnn()
        self.preprocess = RecPreprocessor()
        self.load_image = LoadImage()
        self.decoder = CTCLabelDecode(character_path=self.spec.keys_path)
        self.class_count = len(self.decoder.character)
        self.net = None

        self._check_model_files()
        self._create_net()

    def _check_model_files(self) -> None:
        missing = [
            str(path)
            for path in (
                self.spec.param_path,
                self.spec.bin_path,
                self.spec.keys_path,
            )
            if not path.is_file()
        ]
        if missing:
            raise FileNotFoundError(
                "Отсутствуют файлы NCNN OCR-модели: " + ", ".join(missing)
            )

    def _create_net(self) -> None:
        if self.device == "gpu":
            self.gpu_index = _resolve_gpu_index(self.ncnn, self.gpu_index)
            self.use_vulkan = True
        elif self.device == "cpu":
            self.use_vulkan = False
        else:
            raise RuntimeError(f"Неподдерживаемое устройство NCNN OCR: {self.device}")

        self.net = self.ncnn.Net()
        if hasattr(self.net, "opt"):
            self.net.opt.use_vulkan_compute = self.use_vulkan
            if self.spec.disable_fp16:
                self.net.opt.use_fp16_packed = False
                self.net.opt.use_fp16_storage = False
                self.net.opt.use_fp16_arithmetic = False

        if self.use_vulkan and hasattr(self.net, "set_vulkan_device"):
            self.net.set_vulkan_device(self.gpu_index)

        self._check_return(
            self.net.load_param(str(self.spec.param_path)),
            "load_param",
            self.spec.param_path,
        )
        self._check_return(
            self.net.load_model(str(self.spec.bin_path)),
            "load_model",
            self.spec.bin_path,
        )

        if self.use_vulkan:
            gpu_name = _gpu_info_value(self.ncnn, self.gpu_index, "device_name")
            backend = f"Vulkan GPU {self.gpu_index}"
            if gpu_name:
                backend = f"{backend} ({gpu_name})"
        else:
            backend = "CPU"
        logger.info(
            f"[OCR-NCNN] Загружена модель '{self.spec.name}' через {backend}"
        )

    @staticmethod
    def _check_return(value, operation: str, path: Path) -> None:
        if isinstance(value, int) and value != 0:
            raise RuntimeError(
                f"NCNN {operation} завершился ошибкой для {path}; код {value}"
            )

    def close(self) -> None:
        self.net = None

    def __call__(self, image_or_path) -> TextRecOutput:
        started = time.perf_counter()
        image = self.load_image(image_or_path)
        image, _, _ = resize_image_within_bounds(image, 30, 2000)

        normalized = self.preprocess.resize_norm_img(image)
        predictions = self._infer(normalized)
        line_results, _ = self.decoder(predictions)
        text = line_results[0][0]
        score = (
            float(line_results[0][1])
            if len(line_results[0]) > 1
            else 0.0
        )
        return TextRecOutput(
            imgs=[image],
            txts=(text,),
            scores=(score,),
            word_results=(),
            elapse=time.perf_counter() - started,
        )

    def _infer(self, input_array: np.ndarray) -> np.ndarray:
        if self.net is None:
            raise RuntimeError("NCNN OCR-модель уже закрыта")

        extractor = self.net.create_extractor()
        matrix_input = self._to_ncnn_mat(input_array)
        result = extractor.input(INPUT_NAME, matrix_input)
        if isinstance(result, int) and result != 0:
            raise RuntimeError(
                f"NCNN input('{INPUT_NAME}') завершился с кодом {result}"
            )

        extracted = extractor.extract(self.spec.output_name)
        if isinstance(extracted, tuple):
            status, matrix_output = extracted
            if isinstance(status, int) and status != 0:
                raise RuntimeError(
                    f"NCNN extract('{self.spec.output_name}') "
                    f"завершился с кодом {status}"
                )
        else:
            matrix_output = extracted

        return self._normalize_output(np.array(matrix_output))

    def _to_ncnn_mat(self, input_array: np.ndarray):
        array = np.ascontiguousarray(input_array, dtype=np.float32)
        if array.ndim != 3:
            raise ValueError(
                f"Ожидался вход NCNN CHW; получена форма {array.shape}"
            )

        channels, height, width = array.shape
        matrix = self.ncnn.Mat()
        matrix.create(width, height, channels)
        matrix.numpy("f")[...] = array
        return matrix

    def _normalize_output(self, output: np.ndarray) -> np.ndarray:
        array = np.asarray(output, dtype=np.float32)

        if array.ndim == 3 and array.shape[-1] == self.class_count:
            return array
        if array.ndim == 2 and array.shape[-1] == self.class_count:
            return array[np.newaxis, :, :]
        if array.ndim == 2 and array.shape[0] == self.class_count:
            return array.T[np.newaxis, :, :]
        if array.ndim == 3 and array.shape[0] == self.class_count:
            return np.moveaxis(array, 0, -1).reshape(
                1,
                -1,
                self.class_count,
            )

        raise RuntimeError(
            "Не удалось интерпретировать выход NCNN формы "
            f"{array.shape}; ожидалась размерность классов {self.class_count}."
        )
