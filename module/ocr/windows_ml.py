import os
import threading

from module.logger import logger


QNN_EP = "QNNExecutionProvider"
OPENVINO_EP = "OpenVINOExecutionProvider"
DML_EP = "DmlExecutionProvider"

QNN_NPU_DEVICE = "qnn_npu"
OPENVINO_NPU_DEVICE = "openvino_npu"
OPENVINO_GPU_DEVICE = "openvino_gpu"
OPENVINO_CPU_DEVICE = "openvino_cpu"

_provider_lock = threading.Lock()
_prepared_execution_providers = set()


def create_onnx_session(
    ort,
    model_path,
    session_options_factory=None,
    allow_acceleration=True,
    allow_vendor_execution_providers=True,
    device_preference="auto",
):
    """按固定优先级创建 Windows ML 或 CPU ONNX Runtime session。"""
    create_options = session_options_factory or ort.SessionOptions

    if os.name != "nt" or not allow_acceleration:
        return (
            ort.InferenceSession(
                str(model_path),
                sess_options=create_options(),
                providers=["CPUExecutionProvider"],
            ),
            "CPUExecutionProvider",
        )

    vendor_execution_providers = _vendor_execution_provider_names(device_preference)
    if allow_vendor_execution_providers and vendor_execution_providers:
        _prepare_vendor_execution_providers(ort, vendor_execution_providers)
    elif not allow_vendor_execution_providers and vendor_execution_providers:
        logger.info("[OCR] Windows ML 厂商 EP 自动安装和使用已禁用")

    for device in _iter_preferred_devices(
        ort,
        device_preference=device_preference,
        allow_vendor_execution_providers=allow_vendor_execution_providers,
    ):
        options = create_options()
        # OrtEpDevice 已包含目标硬件的标识；重复传入 ep_options 会导致
        # ONNX Runtime 重复设置 DirectML 的 device_id 并输出无意义警告。
        options.add_provider_for_devices([device], {})
        if device.ep_name == DML_EP:
            options.enable_mem_pattern = False
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        try:
            session = ort.InferenceSession(str(model_path), sess_options=options)
        except Exception as exc:
            logger.warning(
                f"[OCR] Windows ML 无法使用 {device.ep_name}，尝试下一个设备: {exc}"
            )
            continue

        logger.info(f"[OCR] Windows ML 选择 {_describe_device(device)}")
        return session, device.ep_name

    logger.info("[OCR] 未找到符合条件的 Windows ML 加速设备，使用 CPU")
    return (
        ort.InferenceSession(
            str(model_path),
            sess_options=create_options(),
            providers=["CPUExecutionProvider"],
        ),
        "CPUExecutionProvider",
    )


def _prepare_vendor_execution_providers(ort, provider_names):
    """通过 Windows Update 获取并注册本项目允许使用的厂商 EP。"""
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
            logger.warning(f"[OCR] Windows ML Runtime 不可用，跳过 NPU/OpenVINO: {exc}")
            _prepared_execution_providers.update(
                (marker, name) for name in pending_provider_names
            )
            return

        try:
            # ExecutionProvider 句柄由 EpCatalog 所有，必须在目录关闭前完成注册。
            with windowsml.EpCatalog() as catalog:
                providers = {
                    provider.name: provider
                    for provider in catalog.find_all_providers()
                }
                for name in pending_provider_names:
                    provider = providers.get(name)
                    if provider is None:
                        continue
                    _ensure_and_register_provider(ort, windowsml, provider)
        except Exception as exc:
            logger.warning(f"[OCR] 无法枚举 Windows ML 执行提供程序: {exc}")

        _prepared_execution_providers.update(
            (marker, name) for name in pending_provider_names
        )


def _ensure_and_register_provider(ort, windowsml, provider):
    try:
        ready = windowsml.EpReadyState.Ready
        if provider.ready_state != ready:
            logger.info(f"[OCR] 准备 Windows ML {provider.name}: {provider.ready_state}")
            with provider.ensure_ready_async() as operation:
                operation.wait()

        registered_names = {device.ep_name for device in ort.get_ep_devices()}
        if provider.name not in registered_names:
            ort.register_execution_provider_library(provider.name, provider.library_path)
            logger.info(f"[OCR] 已注册 Windows ML {provider.name}")
    except Exception as exc:
        logger.warning(
            f"[OCR] Windows ML {provider.name} 自动安装或更新失败: {exc}。"
            "已跳过该 EP 并继续尝试后备设备；请检查 Windows Update 服务未被禁用、"
            "Windows 更新策略没有被组织管理器关闭，以及网络可访问 Windows 更新服务。"
        )


def _iter_preferred_devices(
    ort,
    device_preference="auto",
    allow_vendor_execution_providers=True,
):
    try:
        devices = ort.get_ep_devices()
    except Exception as exc:
        logger.warning(f"[OCR] 无法枚举 ONNX Runtime 设备: {exc}")
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
        candidates = tuple(candidate for candidate in candidates if candidate[0] == DML_EP)
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
    return str(metadata.get("Discrete", "")).lower() in ("1", "true")


def _describe_device(device):
    metadata = device.device.metadata
    description = metadata.get("Description", device.device.vendor)
    return f"{device.ep_name}/{device.device.type.name}: {description}"
