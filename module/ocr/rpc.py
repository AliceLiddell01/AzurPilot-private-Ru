"""OCR RPC 服务模块。

基于 zerorpc 实现的 OCR 分布式推理框架，支持将 OCR 识别任务分发到独立的服务器进程。
客户端通过 ModelProxyFactory 获取对应语言的代理对象，自动处理连接失败的回退逻辑。
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


class ModelProxy:
    """OCR 模型的 RPC 代理客户端。

    通过 zerorpc 连接本机 OCR 服务器，当服务器不可用时自动回退到本地模型。
    """
    client = None
    online = True

    @classmethod
    def init(cls, address="127.0.0.1:22268"):
        """初始化 RPC 客户端并连接本机 OCR 服务器。"""
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
            logger.info('Отключение от локального сервера OCR')
            cls.client.close()
            logger.info('Соединение с локальным сервером OCR закрыто')
            cls.client = None
            cls.online = True

    def __init__(self, lang) -> None:
        """初始化模型代理。

        Args:
            lang: OCR 模型语言标识，如 'azur_lane'、'ppocr_v6'、'cnocr'、'jp'、'tw'。
        """
        self.lang = lang

    def _rpc_or_fallback(self, method, fallback, *args):
        if self.online:
            try:
                return self.client(method, self.lang, *args)
            except Exception as exc:
                self.online = False
                logger.warning(
                    f"Вызов OCR RPC {method} завершился ошибкой; используется локальная модель: {exc}"
                )
        return fallback()

    def ocr(self, img_fp):
        """对图像执行 OCR 文本识别。"""
        from module.ocr.models import OCR_MODEL
        return self._rpc_or_fallback(
            "ocr",
            lambda: OCR_MODEL.__getattribute__(self.lang).ocr(img_fp),
            encode_image_payload(img_fp),
        )

    def ocr_for_single_line(self, img_fp):
        """对单行文本图像执行 OCR 识别。"""
        from module.ocr.models import OCR_MODEL
        return self._rpc_or_fallback(
            "ocr_for_single_line",
            lambda: OCR_MODEL.__getattribute__(self.lang).ocr_for_single_line(img_fp),
            encode_image_payload(img_fp),
        )

    def ocr_for_single_lines(self, img_list):
        """对多张单行文本图像批量执行 OCR 识别。"""
        from module.ocr.models import OCR_MODEL
        payloads = [encode_image_payload(img_fp) for img_fp in img_list]
        return self._rpc_or_fallback(
            "ocr_for_single_lines",
            lambda: OCR_MODEL.__getattribute__(self.lang).ocr_for_single_lines(img_list),
            payloads,
        )

    def set_cand_alphabet(self, cand_alphabet: str):
        """设置 OCR 识别的候选字符集。"""
        from module.ocr.models import OCR_MODEL
        return self._rpc_or_fallback(
            "set_cand_alphabet",
            lambda: OCR_MODEL.__getattribute__(self.lang).set_cand_alphabet(cand_alphabet),
            cand_alphabet,
        )

    def atomic_ocr(self, img_fp, cand_alphabet=None):
        """使用候选字符集对图像执行原子 OCR 识别。"""
        from module.ocr.models import OCR_MODEL
        return self._rpc_or_fallback(
            "atomic_ocr",
            lambda: OCR_MODEL.__getattribute__(self.lang).atomic_ocr(img_fp, cand_alphabet),
            encode_image_payload(img_fp),
            cand_alphabet,
        )

    def atomic_ocr_for_single_line(self, img_fp, cand_alphabet=None):
        """使用候选字符集对单行文本图像执行原子 OCR 识别。"""
        from module.ocr.models import OCR_MODEL
        return self._rpc_or_fallback(
            "atomic_ocr_for_single_line",
            lambda: OCR_MODEL.__getattribute__(self.lang).atomic_ocr_for_single_line(
                img_fp,
                cand_alphabet,
            ),
            encode_image_payload(img_fp),
            cand_alphabet,
        )

    def atomic_ocr_for_single_lines(self, img_list, cand_alphabet=None):
        """使用候选字符集批量执行单行文本原子 OCR 识别。"""
        from module.ocr.models import OCR_MODEL
        payloads = [encode_image_payload(img_fp) for img_fp in img_list]
        return self._rpc_or_fallback(
            "atomic_ocr_for_single_lines",
            lambda: OCR_MODEL.__getattribute__(self.lang).atomic_ocr_for_single_lines(
                img_list,
                cand_alphabet,
            ),
            payloads,
            cand_alphabet,
        )

    def debug(self, img_list):
        """对图像列表执行调试模式 OCR 识别。"""
        from module.ocr.models import OCR_MODEL
        payloads = [encode_image_payload(img_fp) for img_fp in img_list]
        return self._rpc_or_fallback(
            "debug",
            lambda: OCR_MODEL.__getattribute__(self.lang).debug(img_list),
            payloads,
        )


class ModelProxyFactory:
    """OCR 模型代理工厂。"""

    def __getattribute__(self, __name: str) -> ModelProxy:
        if __name in ["azur_lane", "ppocr_v6", "cnocr", "jp", "tw", "azur_lane_jp"]:
            if ModelProxy.client is None:
                ModelProxy.init(address=State.deploy_config.OcrClientAddress)
            return ModelProxy(lang=__name)
        else:
            return super().__getattribute__(__name)

    def close(self):
        """关闭底层 RPC 客户端连接。"""
        ModelProxy.close()


def start_ocr_server(port=22268):
    """Запускает OCR RPC только на loopback-интерфейсе."""
    import zerorpc
    import zmq
    from module.ocr.al_ocr import AlOcr
    from module.ocr.models import OcrModel

    class OCRServer(OcrModel):
        """OCR RPC 服务端实现，继承 OcrModel 以复用模型加载逻辑。"""

        def hello(self):
            return "hello"

        def ocr(self, lang, img_fp):
            image = decode_image_payload(img_fp)
            model: AlOcr = self.__getattribute__(lang)
            return model.ocr(image)

        def ocr_for_single_line(self, lang, img_fp):
            image = decode_image_payload(img_fp)
            model: AlOcr = self.__getattribute__(lang)
            return model.ocr_for_single_line(image)

        def ocr_for_single_lines(self, lang, img_list):
            images = [decode_image_payload(img_fp) for img_fp in img_list]
            model: AlOcr = self.__getattribute__(lang)
            return model.ocr_for_single_lines(images)

        def set_cand_alphabet(self, lang, cand_alphabet):
            model: AlOcr = self.__getattribute__(lang)
            return model.set_cand_alphabet(cand_alphabet)

        def atomic_ocr(self, lang, img_fp, cand_alphabet):
            image = decode_image_payload(img_fp)
            model: AlOcr = self.__getattribute__(lang)
            return model.atomic_ocr(image, cand_alphabet)

        def atomic_ocr_for_single_line(self, lang, img_fp, cand_alphabet):
            image = decode_image_payload(img_fp)
            model: AlOcr = self.__getattribute__(lang)
            return model.atomic_ocr_for_single_line(image, cand_alphabet)

        def atomic_ocr_for_single_lines(self, lang, img_list, cand_alphabet):
            images = [decode_image_payload(img_fp) for img_fp in img_list]
            model: AlOcr = self.__getattribute__(lang)
            return model.atomic_ocr_for_single_lines(images, cand_alphabet)

        def debug(self, lang, img_list):
            images = [decode_image_payload(img_fp) for img_fp in img_list]
            model: AlOcr = self.__getattribute__(lang)
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
    """在独立子进程中启动 OCR 服务器。"""
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
    """检查 OCR 服务器子进程是否存活。"""
    global process
    if process is not None:
        return process.is_alive()
    else:
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
