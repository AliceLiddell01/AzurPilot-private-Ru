"""Локальный OCR RPC с безопасной loopback-границей и локальным fallback."""

import argparse
import multiprocessing

from module.logger import logger
from module.ocr.stage8b_rpc_security import (
    client_uri,
    decode_trusted_local_image,
    loopback_bind_uri,
    normalize_loopback_address,
)
from module.webui.setting import State

process: multiprocessing.Process = None


class ModelProxy:
    """Прокси OCR-модели с автоматическим fallback на локальную модель."""

    client = None
    online = True

    @classmethod
    def init(cls, address="127.0.0.1:22268"):
        import zerorpc

        safe_address = normalize_loopback_address(address)
        logger.info(f"Подключение к локальному серверу OCR {safe_address}")
        cls.client = zerorpc.Client(timeout=5)
        cls.client.connect(client_uri(safe_address))
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
        if cls.client is not None:
            logger.info("Отключение от локального сервера OCR")
            cls.client.close()
            logger.info("Соединение с локальным сервером OCR закрыто")
            cls.client = None

    def __init__(self, lang) -> None:
        self.lang = lang

    def ocr(self, img_fp):
        if self.online:
            img_str = img_fp.dumps()
            try:
                return self.client("ocr", self.lang, img_str)
            except Exception as exc:
                self.online = False
                logger.warning(
                    f"Вызов OCR RPC завершился ошибкой; используется локальная модель: {exc}"
                )
        from module.ocr.models import OCR_MODEL
        return OCR_MODEL.__getattribute__(self.lang).ocr(img_fp)

    def ocr_for_single_line(self, img_fp):
        if self.online:
            img_str = img_fp.dumps()
            try:
                return self.client("ocr_for_single_line", self.lang, img_str)
            except Exception as exc:
                self.online = False
                logger.warning(
                    f"Вызов OCR RPC для одной строки завершился ошибкой; используется локальная модель: {exc}"
                )
        from module.ocr.models import OCR_MODEL
        return OCR_MODEL.__getattribute__(self.lang).ocr_for_single_line(img_fp)

    def ocr_for_single_lines(self, img_list):
        if self.online:
            img_str_list = [img_fp.dumps() for img_fp in img_list]
            try:
                return self.client("ocr_for_single_lines", self.lang, img_str_list)
            except Exception as exc:
                self.online = False
                logger.warning(
                    f"Пакетный вызов OCR RPC завершился ошибкой; используется локальная модель: {exc}"
                )
        from module.ocr.models import OCR_MODEL
        return OCR_MODEL.__getattribute__(self.lang).ocr_for_single_lines(img_list)

    def set_cand_alphabet(self, cand_alphabet: str):
        if self.online:
            try:
                return self.client("set_cand_alphabet", self.lang, cand_alphabet)
            except Exception as exc:
                self.online = False
                logger.warning(
                    f"Не удалось передать alphabet через OCR RPC; используется локальная модель: {exc}"
                )
        from module.ocr.models import OCR_MODEL
        return OCR_MODEL.__getattribute__(self.lang).set_cand_alphabet(cand_alphabet)

    def atomic_ocr(self, img_fp, cand_alphabet=None):
        if self.online:
            img_str = img_fp.dumps()
            try:
                return self.client("atomic_ocr", self.lang, img_str, cand_alphabet)
            except Exception as exc:
                self.online = False
                logger.warning(
                    f"Вызов atomic OCR RPC завершился ошибкой; используется локальная модель: {exc}"
                )
        from module.ocr.models import OCR_MODEL
        return OCR_MODEL.__getattribute__(self.lang).atomic_ocr(img_fp, cand_alphabet)

    def atomic_ocr_for_single_line(self, img_fp, cand_alphabet=None):
        if self.online:
            img_str = img_fp.dumps()
            try:
                return self.client(
                    "atomic_ocr_for_single_line",
                    self.lang,
                    img_str,
                    cand_alphabet,
                )
            except Exception as exc:
                self.online = False
                logger.warning(
                    "Вызов atomic OCR RPC для одной строки завершился ошибкой; "
                    f"используется локальная модель: {exc}"
                )
        from module.ocr.models import OCR_MODEL
        return OCR_MODEL.__getattribute__(self.lang).atomic_ocr_for_single_line(
            img_fp,
            cand_alphabet,
        )

    def atomic_ocr_for_single_lines(self, img_list, cand_alphabet=None):
        if self.online:
            img_str_list = [img_fp.dumps() for img_fp in img_list]
            try:
                return self.client(
                    "atomic_ocr_for_single_lines",
                    self.lang,
                    img_str_list,
                    cand_alphabet,
                )
            except Exception as exc:
                self.online = False
                logger.warning(
                    "Пакетный вызов atomic OCR RPC завершился ошибкой; "
                    f"используется локальная модель: {exc}"
                )
        from module.ocr.models import OCR_MODEL
        return OCR_MODEL.__getattribute__(self.lang).atomic_ocr_for_single_lines(
            img_list,
            cand_alphabet,
        )

    def debug(self, img_list):
        if self.online:
            img_str_list = [img_fp.dumps() for img_fp in img_list]
            try:
                return self.client("debug", self.lang, img_str_list)
            except Exception as exc:
                self.online = False
                logger.warning(
                    f"Отладочный вызов OCR RPC завершился ошибкой; используется локальная модель: {exc}"
                )
        from module.ocr.models import OCR_MODEL
        return OCR_MODEL.__getattribute__(self.lang).debug(img_list)


class ModelProxyFactory:
    """Фабрика прокси поддерживаемых OCR-моделей."""

    def __getattribute__(self, __name: str) -> ModelProxy:
        if __name in ["azur_lane", "ppocr_v6", "cnocr", "jp", "tw", "azur_lane_jp"]:
            if ModelProxy.client is None:
                ModelProxy.init(address=State.deploy_config.OcrClientAddress)
            return ModelProxy(lang=__name)
        return super().__getattribute__(__name)

    def close(self):
        ModelProxy.close()


def start_ocr_server(port=22268):
    """Запускает OCR RPC только на loopback-интерфейсе."""
    import zerorpc
    import zmq
    from module.ocr.al_ocr import AlOcr
    from module.ocr.models import OcrModel

    class OCRServer(OcrModel):
        def hello(self):
            return "hello"

        def ocr(self, lang, img_fp):
            image = decode_trusted_local_image(img_fp)
            model: AlOcr = self.__getattribute__(lang)
            return model.ocr(image)

        def ocr_for_single_line(self, lang, img_fp):
            image = decode_trusted_local_image(img_fp)
            model: AlOcr = self.__getattribute__(lang)
            return model.ocr_for_single_line(image)

        def ocr_for_single_lines(self, lang, img_list):
            images = [decode_trusted_local_image(img_fp) for img_fp in img_list]
            model: AlOcr = self.__getattribute__(lang)
            return model.ocr_for_single_lines(images)

        def set_cand_alphabet(self, lang, cand_alphabet):
            model: AlOcr = self.__getattribute__(lang)
            return model.set_cand_alphabet(cand_alphabet)

        def atomic_ocr(self, lang, img_fp, cand_alphabet):
            image = decode_trusted_local_image(img_fp)
            model: AlOcr = self.__getattribute__(lang)
            return model.atomic_ocr(image, cand_alphabet)

        def atomic_ocr_for_single_line(self, lang, img_fp, cand_alphabet):
            image = decode_trusted_local_image(img_fp)
            model: AlOcr = self.__getattribute__(lang)
            return model.atomic_ocr_for_single_line(image, cand_alphabet)

        def atomic_ocr_for_single_lines(self, lang, img_list, cand_alphabet):
            images = [decode_trusted_local_image(img_fp) for img_fp in img_list]
            model: AlOcr = self.__getattribute__(lang)
            return model.atomic_ocr_for_single_lines(images, cand_alphabet)

        def debug(self, lang, img_list):
            images = [decode_trusted_local_image(img_fp) for img_fp in img_list]
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
    global process
    if not alive():
        process = multiprocessing.Process(target=start_ocr_server, args=(port,))
        process.start()
        logger.info(f"[OCR-RPC] Запущен процесс сервера OCR на loopback-порту {port}")


def stop_ocr_server_process():
    global process
    if alive():
        process.kill()
        process = None
        logger.info("[OCR-RPC] Процесс сервера OCR остановлен")


def alive() -> bool:
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
