"""OCR RPC 服务模块。

基于 zerorpc 实现的本机 OCR 推理服务。客户端通过 ModelProxyFactory
获取对应语言的代理对象；当本机服务不可用时自动回退到进程内模型。
"""

import argparse
import multiprocessing

from module.logger import logger
from module.ocr.stage8b_rpc_security import (
    client_uri,
    decode_image_payload,
    encode_image_payload,
    loopback_bind_uri,
    normalize_loopback_address,
)
from module.webui.setting import State

process: multiprocessing.Process = None

SUPPORTED_OCR_MODELS = frozenset(
    {
        "azur_lane",
        "azur_lane_jp",
        "ppocr_v6",
        "cnocr",
        "jp",
        "tw",
    }
)
MAX_RPC_BATCH_IMAGES = 64
MAX_RPC_BATCH_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_ALPHABET_LENGTH = 8192


def _validate_model_name(lang: str) -> str:
    if not isinstance(lang, str) or lang not in SUPPORTED_OCR_MODELS:
        raise ValueError(f"Неподдерживаемая модель OCR RPC: {lang!r}")
    return lang


def _validate_batch(items):
    if not isinstance(items, (list, tuple)):
        raise ValueError("Пакет OCR RPC должен быть списком или кортежем.")
    if not 1 <= len(items) <= MAX_RPC_BATCH_IMAGES:
        raise ValueError(
            "Количество изображений OCR RPC должно быть в диапазоне "
            f"1–{MAX_RPC_BATCH_IMAGES}."
        )
    return list(items)


def _validate_candidate_alphabet(cand_alphabet):
    if cand_alphabet is None:
        return None
    if not isinstance(cand_alphabet, str):
        raise ValueError("Алфавит OCR RPC должен быть строкой или None.")
    if len(cand_alphabet) > MAX_CANDIDATE_ALPHABET_LENGTH:
        raise ValueError(
            "Алфавит OCR RPC превышает допустимую длину "
            f"{MAX_CANDIDATE_ALPHABET_LENGTH}."
        )
    return cand_alphabet


def _encode_batch(images):
    payloads = []
    total_bytes = 0
    for image in _validate_batch(images):
        payload = encode_image_payload(image)
        total_bytes += len(payload)
        if total_bytes > MAX_RPC_BATCH_BYTES:
            raise ValueError(
                "Суммарный размер пакета OCR RPC превышает допустимый предел."
            )
        payloads.append(payload)
    return payloads


def _decode_batch(payloads):
    payloads = _validate_batch(payloads)
    if sum(len(payload) for payload in payloads) > MAX_RPC_BATCH_BYTES:
        raise ValueError("Суммарный размер пакета OCR RPC превышает допустимый предел.")
    return [decode_image_payload(payload) for payload in payloads]


def _get_server_model(container, lang):
    return getattr(container, _validate_model_name(lang))


def _get_local_model(lang):
    from module.ocr.models import OCR_MODEL

    return _get_server_model(OCR_MODEL, lang)


class ModelProxy:
    """OCR 模型的 RPC 代理客户端。

    代理仅连接本机 loopback 服务。连接或调用失败后，同一进程中的后续
    调用回退到本地模型，不再重复尝试已失效的 RPC transport。
    """

    client = None
    online = True

    @classmethod
    def init(cls, address="127.0.0.1:22268"):
        """初始化 RPC 客户端并连接本机 OCR 服务器。

        Args:
            address: OCR 服务器地址，必须是 loopback host:port。
        """
        import zerorpc

        safe_address = normalize_loopback_address(address)
        logger.info(f"Подключение к локальному серверу OCR {safe_address}")
        cls.client = zerorpc.Client(timeout=5)
        cls.client.connect(client_uri(safe_address))
        cls.online = True
        try:
            cls.client.hello()
            logger.info("Соединение с локальным сервером OCR установлено")
        except Exception as exc:
            cls.online = False
            logger.warning(
                f"Локальный сервер OCR недоступен; используется локальная модель: {exc}"
            )

    @classmethod
    def close(cls):
        """关闭 RPC 客户端连接。"""
        if cls.client is not None:
            logger.info("Отключение от локального сервера OCR")
            cls.client.close()
            logger.info("Соединение с локальным сервером OCR закрыто")
            cls.client = None
        cls.online = True

    def __init__(self, lang) -> None:
        """初始化模型代理。

        Args:
            lang: 受支持的 OCR 模型语言标识。
        """
        self.lang = _validate_model_name(lang)

    def _rpc_or_fallback(self, method, fallback, args_factory):
        if self.online:
            args = args_factory()
            try:
                return self.client(method, self.lang, *args)
            except Exception as exc:
                self.online = False
                type(self).online = False
                logger.warning(
                    f"Вызов OCR RPC {method} завершился ошибкой; "
                    f"используется локальная модель: {exc}"
                )
        return fallback()

    def ocr(self, img_fp):
        """对图像执行 OCR 文本识别。"""
        return self._rpc_or_fallback(
            "ocr",
            lambda: _get_local_model(self.lang).ocr(img_fp),
            lambda: (encode_image_payload(img_fp),),
        )

    def ocr_for_single_line(self, img_fp):
        """对单行文本图像执行 OCR 识别。"""
        return self._rpc_or_fallback(
            "ocr_for_single_line",
            lambda: _get_local_model(self.lang).ocr_for_single_line(img_fp),
            lambda: (encode_image_payload(img_fp),),
        )

    def ocr_for_single_lines(self, img_list):
        """对多张单行文本图像批量执行 OCR 识别。"""
        return self._rpc_or_fallback(
            "ocr_for_single_lines",
            lambda: _get_local_model(self.lang).ocr_for_single_lines(img_list),
            lambda: (_encode_batch(img_list),),
        )

    def set_cand_alphabet(self, cand_alphabet: str):
        """设置 OCR 识别的候选字符集。"""
        return self._rpc_or_fallback(
            "set_cand_alphabet",
            lambda: _get_local_model(self.lang).set_cand_alphabet(cand_alphabet),
            lambda: (_validate_candidate_alphabet(cand_alphabet),),
        )

    def atomic_ocr(self, img_fp, cand_alphabet=None):
        """使用候选字符集对图像执行原子 OCR 识别。"""
        return self._rpc_or_fallback(
            "atomic_ocr",
            lambda: _get_local_model(self.lang).atomic_ocr(img_fp, cand_alphabet),
            lambda: (
                encode_image_payload(img_fp),
                _validate_candidate_alphabet(cand_alphabet),
            ),
        )

    def atomic_ocr_for_single_line(self, img_fp, cand_alphabet=None):
        """使用候选字符集对单行文本图像执行原子 OCR 识别。"""
        return self._rpc_or_fallback(
            "atomic_ocr_for_single_line",
            lambda: _get_local_model(self.lang).atomic_ocr_for_single_line(
                img_fp,
                cand_alphabet,
            ),
            lambda: (
                encode_image_payload(img_fp),
                _validate_candidate_alphabet(cand_alphabet),
            ),
        )

    def atomic_ocr_for_single_lines(self, img_list, cand_alphabet=None):
        """使用候选字符集批量执行单行文本原子 OCR 识别。"""
        return self._rpc_or_fallback(
            "atomic_ocr_for_single_lines",
            lambda: _get_local_model(self.lang).atomic_ocr_for_single_lines(
                img_list,
                cand_alphabet,
            ),
            lambda: (
                _encode_batch(img_list),
                _validate_candidate_alphabet(cand_alphabet),
            ),
        )

    def debug(self, img_list):
        """对图像列表执行调试模式 OCR 识别。"""
        return self._rpc_or_fallback(
            "debug",
            lambda: _get_local_model(self.lang).debug(img_list),
            lambda: (_encode_batch(img_list),),
        )


class ModelProxyFactory:
    """OCR 模型代理工厂。

    通过 __getattribute__ 拦截受支持的语言模型属性访问，返回对应代理。
    """

    def __getattribute__(self, __name: str) -> ModelProxy:
        if __name in SUPPORTED_OCR_MODELS:
            if ModelProxy.client is None:
                ModelProxy.init(address=State.deploy_config.OcrClientAddress)
            return ModelProxy(lang=__name)
        return super().__getattribute__(__name)

    def close(self):
        """关闭底层 RPC 客户端连接。"""
        ModelProxy.close()


def start_ocr_server(port=22268):
    """启动只监听 loopback 的 OCR RPC 服务器。

    Args:
        port: 服务器监听端口，默认 22268。
    """
    import zerorpc
    import zmq

    from module.ocr.al_ocr import AlOcr
    from module.ocr.models import OcrModel

    class OCRServer(OcrModel):
        """OCR RPC 服务端实现，继承 OcrModel 以复用模型加载逻辑。"""

        def hello(self):
            """心跳检测，用于客户端验证服务器是否存活。"""
            return "hello"

        def ocr(self, lang, img_fp):
            image = decode_image_payload(img_fp)
            model: AlOcr = _get_server_model(self, lang)
            return model.ocr(image)

        def ocr_for_single_line(self, lang, img_fp):
            image = decode_image_payload(img_fp)
            model: AlOcr = _get_server_model(self, lang)
            return model.ocr_for_single_line(image)

        def ocr_for_single_lines(self, lang, img_list):
            images = _decode_batch(img_list)
            model: AlOcr = _get_server_model(self, lang)
            return model.ocr_for_single_lines(images)

        def set_cand_alphabet(self, lang, cand_alphabet):
            model: AlOcr = _get_server_model(self, lang)
            return model.set_cand_alphabet(
                _validate_candidate_alphabet(cand_alphabet)
            )

        def atomic_ocr(self, lang, img_fp, cand_alphabet):
            image = decode_image_payload(img_fp)
            model: AlOcr = _get_server_model(self, lang)
            return model.atomic_ocr(
                image,
                _validate_candidate_alphabet(cand_alphabet),
            )

        def atomic_ocr_for_single_line(self, lang, img_fp, cand_alphabet):
            image = decode_image_payload(img_fp)
            model: AlOcr = _get_server_model(self, lang)
            return model.atomic_ocr_for_single_line(
                image,
                _validate_candidate_alphabet(cand_alphabet),
            )

        def atomic_ocr_for_single_lines(self, lang, img_list, cand_alphabet):
            images = _decode_batch(img_list)
            model: AlOcr = _get_server_model(self, lang)
            return model.atomic_ocr_for_single_lines(
                images,
                _validate_candidate_alphabet(cand_alphabet),
            )

        def debug(self, lang, img_list):
            images = _decode_batch(img_list)
            model: AlOcr = _get_server_model(self, lang)
            return model.debug(images)

    server = zerorpc.Server(OCRServer())
    bind_uri = loopback_bind_uri(port)
    try:
        server.bind(bind_uri)
    except zmq.error.ZMQError as exc:
        logger.error(f"[OCR-RPC] Сервер OCR не смог привязаться к {bind_uri}: {exc}")
        return
    logger.info(f"[OCR-RPC] Сервер OCR слушает {bind_uri}")
    server.run()


def start_ocr_server_process(port=22268):
    """在独立子进程中启动 OCR 服务器。

    Args:
        port: 服务器监听端口，默认 22268。
    """
    global process
    if not alive():
        process = multiprocessing.Process(target=start_ocr_server, args=(port,))
        process.start()
        logger.info(f"[OCR-RPC] Запущен процесс сервера OCR на loopback-порту {port}")


def stop_ocr_server_process():
    """终止 OCR 服务器子进程。"""
    global process
    if alive():
        process.kill()
        process = None
        logger.info("[OCR-RPC] Процесс сервера OCR остановлен")


def alive() -> bool:
    """检查 OCR 服务器子进程是否存活。

    Returns:
        子进程是否正在运行。
    """
    global process
    if process is not None:
        return process.is_alive()
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Локальный сервис OCR AzurPilot")
    parser.add_argument(
        "--port",
        type=int,
        help="Loopback-порт; по умолчанию используется OcrServerPort из deploy config",
    )
    args, _ = parser.parse_known_args()
    port = args.port or State.deploy_config.OcrServerPort
    start_ocr_server(port=port)
