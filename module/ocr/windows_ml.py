"""Выбор ONNX Runtime Execution Provider через Windows ML."""

import os
import re
import threading

from module.logger import logger

QNN_EP = "QNNExecutionProvider"
OPENVINO_EP = "OpenVINOExecutionProvider"
DML_EP = "DmlExecutionProvider"

QNN_NPU_DEVICE = "qnn_npu"
OPENVINO_NPU_DEVICE = "openvino_npu"
OPENVINO_GPU_DEVICE = "openvino_gpu"
OPENVINO_CPU_DEVICE = "openvino_cpu"

_MIN_DISCRETE_VIDEO_MEMORY_MIB = 1024
_AMD_INTEGRATED_HD_MODELS = {
    "6250", "6290", "6310", "6320", "6410d", "6530d", "6550d", "7560d", "7660d",
}
_AMD_INTEGRATED_VEGA_MODELS = {"3", "5", "6", "7", "8", "10", "11"}
_AMD_INTEGRATED_RDNA_MODELS = {
    "610m", "660m", "680m", "740m", "760m", "780m", "840m", "860m", "880m", "890m",
}

_provider_lock = threading.Lock()
_prepared_execution_providers = set()


def _provider_download_allowed(explicit):
    if explicit is not None:
        return bool(explicit)
    value = os.environ.get("AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _available_provider_names(ort):
    getter = getattr(ort, "get_available_providers", None)
    if getter is None:
        return ()
    try:
        return tuple(getter())
    except Exception as exc:
        logger.warning(f"[OCR] Не удалось получить registered ONNX Runtime providers: {exc}")
        return ()


def create_onnx_session(
    ort,
    model_path,
    session_options_factory=None,
    allow_acceleration=True,
    allow_vendor_execution_providers=True,
    device_preference="auto",
    allow_provider_download=None,
):
    """Создаёт ONNX Runtime session в сохранённом порядке provider priority."""
    create_options = session_options_factory or ort.SessionOptions
    requested_vendor = _vendor_execution_provider_names(device_preference)
    logger.info(
        f"[OCR] Запрошено устройство '{device_preference}'; vendor providers: "
        f"{requested_vendor or ('none',)}"
    )

    if os.name != "nt" or not allow_acceleration:
        session = ort.InferenceSession(
            str(model_path),
            sess_options=create_options(),
            providers=["CPUExecutionProvider"],
        )
        logger.info(
            f"[OCR] Создана CPU session ONNX Runtime; session providers: {session.get_providers()}"
        )
        return session, "CPUExecutionProvider"

    download_allowed = _provider_download_allowed(allow_provider_download)
    if allow_vendor_execution_providers and requested_vendor:
        _prepare_vendor_execution_providers(
            ort,
            requested_vendor,
            allow_provider_download=download_allowed,
        )
    elif not allow_vendor_execution_providers and requested_vendor:
        logger.info("[OCR] Автоматическая подготовка и использование vendor EP Windows ML отключены")

    registered = _available_provider_names(ort)
    logger.info(f"[OCR] Registered ONNX Runtime providers: {registered}")

    for device in _iter_preferred_devices(
        ort,
        device_preference=device_preference,
        allow_vendor_execution_providers=allow_vendor_execution_providers,
    ):
        options = create_options()
        options.add_provider_for_devices([device], {})
        if device.ep_name == DML_EP:
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        try:
            session = ort.InferenceSession(str(model_path), sess_options=options)
        except Exception as exc:
            logger.warning(
                f"[OCR] Windows ML не смог создать session через {device.ep_name}; "
                f"проверяется следующее устройство: {exc}"
            )
            continue

        session_providers = tuple(session.get_providers())
        try:
            provider_options = session.get_provider_options()
        except Exception:
            provider_options = {}
        if device.ep_name not in session_providers:
            logger.warning(
                f"[OCR] Provider {device.ep_name} не прикреплён к созданной session; "
                f"session providers: {session_providers}. Проверяется fallback."
            )
            continue

        logger.info(
            f"[OCR] Windows ML выбрал {_describe_device(device)}; "
            f"session providers: {session_providers}; options: {provider_options}"
        )
        return session, device.ep_name

    logger.info("[OCR] Подходящее устройство Windows ML не найдено; используется CPU fallback")
    session = ort.InferenceSession(
        str(model_path),
        sess_options=create_options(),
        providers=["CPUExecutionProvider"],
    )
    logger.info(f"[OCR] CPU fallback session providers: {session.get_providers()}")
    return session, "CPUExecutionProvider"


def _prepare_vendor_execution_providers(
    ort,
    provider_names,
    *,
    allow_provider_download=True,
):
    """Обнаруживает и при необходимости готовит разрешённые vendor EP Windows ML."""
    marker = id(ort)
    with _provider_lock:
        pending_provider_names = tuple(
            name
            for name in provider_names
            if (marker, name) not in _prepared_execution_providers
        )
        if not pending_provider_names:
            return

        try:
            import windowsml
        except Exception as exc:
            logger.warning(f"[OCR] Windows ML Runtime недоступен; NPU/OpenVINO пропущены: {exc}")
            _prepared_execution_providers.update(
                (marker, name) for name in pending_provider_names
            )
            return

        try:
            with windowsml.EpCatalog() as catalog:
                providers = {
                    provider.name: provider
                    for provider in catalog.find_all_providers()
                }
                logger.info(
                    f"[OCR] Обнаружены providers Windows ML: {tuple(providers)}"
                )
                for name in pending_provider_names:
                    provider = providers.get(name)
                    if provider is None:
                        logger.info(f"[OCR] Provider Windows ML {name} не обнаружен")
                        continue
                    _ensure_and_register_provider(
                        ort,
                        windowsml,
                        provider,
                        allow_provider_download=allow_provider_download,
                    )
        except Exception as exc:
            logger.warning(f"[OCR] Не удалось перечислить Execution Providers Windows ML: {exc}")

        _prepared_execution_providers.update(
            (marker, name) for name in pending_provider_names
        )


def _ensure_and_register_provider(
    ort,
    windowsml,
    provider,
    *,
    allow_provider_download=True,
):
    try:
        ready = windowsml.EpReadyState.Ready
        logger.info(
            f"[OCR] Provider Windows ML {provider.name}; ready state: {provider.ready_state}"
        )
        if provider.ready_state != ready:
            if not allow_provider_download:
                logger.info(
                    f"[OCR] Подготовка/download provider {provider.name} запрещена текущим режимом; "
                    "provider пропущен без системных изменений"
                )
                return False
            logger.info(f"[OCR] Подготовка provider Windows ML {provider.name}")
            with provider.ensure_ready_async() as operation:
                operation.wait()

        registered_names = {device.ep_name for device in ort.get_ep_devices()}
        if provider.name not in registered_names:
            ort.register_execution_provider_library(
                provider.name,
                provider.library_path,
            )
            logger.info(f"[OCR] Зарегистрирован provider Windows ML {provider.name}")
        return True
    except Exception as exc:
        logger.warning(
            f"[OCR] Не удалось подготовить или зарегистрировать Windows ML {provider.name}: {exc}. "
            "Provider пропущен; будет проверен следующий backend или CPU fallback. "
            "Проверьте Windows Update policy и доступ к службе обновлений."
        )
        return False


def _iter_preferred_devices(
    ort,
    device_preference="auto",
    allow_vendor_execution_providers=True,
):
    try:
        devices = ort.get_ep_devices()
    except Exception as exc:
        logger.warning(f"[OCR] Не удалось перечислить устройства ONNX Runtime: {exc}")
        return ()

    device_types = ort.OrtHardwareDeviceType
    candidates = {
        "auto": (
            (QNN_EP, device_types.NPU, False),
            (OPENVINO_EP, device_types.NPU, False),
            (OPENVINO_EP, device_types.GPU, True),
            (DML_EP, device_types.GPU, True),
            (OPENVINO_EP, device_types.CPU, False),
        ),
        QNN_NPU_DEVICE: ((QNN_EP, device_types.NPU, False),),
        OPENVINO_NPU_DEVICE: ((OPENVINO_EP, device_types.NPU, False),),
        OPENVINO_GPU_DEVICE: ((OPENVINO_EP, device_types.GPU, True),),
        "gpu": ((DML_EP, device_types.GPU, True),),
        OPENVINO_CPU_DEVICE: ((OPENVINO_EP, device_types.CPU, False),),
    }.get(device_preference, ())
    if not allow_vendor_execution_providers:
        candidates = tuple(
            candidate for candidate in candidates if candidate[0] == DML_EP
        )
    return tuple(
        device
        for ep_name, device_type, require_discrete in candidates
        for device in devices
        if device.ep_name == ep_name
        and device.device.type == device_type
        and (not require_discrete or _is_discrete_gpu(device))
    )


def _vendor_execution_provider_names(device_preference):
    if device_preference in ("auto", QNN_NPU_DEVICE):
        names = [QNN_EP]
    else:
        names = []
    if device_preference in (
        "auto",
        OPENVINO_NPU_DEVICE,
        OPENVINO_GPU_DEVICE,
        OPENVINO_CPU_DEVICE,
    ):
        names.append(OPENVINO_EP)
    return tuple(names)


def _is_discrete_gpu(device):
    metadata = device.device.metadata
    discrete = metadata.get("Discrete")
    if discrete is not None:
        return str(discrete).lower() in ("1", "true")

    name = _normalize_gpu_name(metadata.get("Description", ""))
    if _is_known_integrated_gpu_name(name) or _is_software_gpu_name(name):
        return False

    video_memory_mib = _video_memory_mib(metadata.get("DxgiVideoMemory"))
    if video_memory_mib is None:
        return True
    return video_memory_mib >= _MIN_DISCRETE_VIDEO_MEMORY_MIB


def _normalize_gpu_name(name):
    name = str(name).lower()
    name = name.replace("(r)", "").replace("(tm)", "")
    return " ".join(name.split())


def _is_known_integrated_gpu_name(name):
    if name.startswith(
        (
            "intel graphics media accelerator",
            "intel gma ",
            "intel hd graphics",
            "intel iris graphics",
            "intel iris plus graphics",
            "intel iris pro graphics",
            "intel iris xe graphics",
            "intel uhd graphics",
            "intel graphics",
            "intel arc graphics",
            "intel arc 130v",
            "intel arc 140v",
        )
    ):
        return True

    for prefix in ("amd ", "advanced micro devices, inc. "):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break

    if name in {"radeon graphics", "radeon(tm) graphics"}:
        return True
    if re.fullmatch(r"radeon r[2-7] graphics", name):
        return True
    if re.fullmatch(
        r"radeon hd (?:" + "|".join(_AMD_INTEGRATED_HD_MODELS) + r")(?: graphics)?",
        name,
    ):
        return True
    if re.fullmatch(
        r"radeon vega (?:" + "|".join(_AMD_INTEGRATED_VEGA_MODELS) + r")(?: graphics)?",
        name,
    ):
        return True
    return bool(
        re.fullmatch(
            r"radeon (?:" + "|".join(_AMD_INTEGRATED_RDNA_MODELS) + r")(?: graphics)?",
            name,
        )
    )


def _is_software_gpu_name(name):
    return name.startswith(
        (
            "microsoft basic render driver",
            "microsoft remote display adapter",
            "remote display adapter",
        )
    )


def _video_memory_mib(value):
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(MiB|MB|GiB|GB)?\s*", str(value))
    if match is None:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "MiB").lower()
    if unit in {"gib", "gb"}:
        amount *= 1024
    return int(amount)


def _describe_device(device):
    metadata = device.device.metadata
    description = metadata.get("Description", device.device.vendor)
    return f"{device.ep_name}/{device.device.type.name}: {description}"
