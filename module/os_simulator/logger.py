"""Журналирование симулятора большого мира через общий runtime logger."""

import logging


class TqdmToLogger:
    """Перенаправить вывод ``tqdm`` в logging для сред без консоли."""

    def __init__(self, logger):
        self.logger = logger

    def write(self, buf):
        msg = buf.strip('\r\n\t ')
        if msg:
            self.logger.info(msg)

    def flush(self):
        pass


class OSSLogger:
    """Предоставить симулятору дочерний logger без локального file sink."""

    def __init__(self):
        self.logger = logging.getLogger('alas.OSSimulator')
        self.logger.setLevel(logging.INFO)
        # Записи проходят к общему console/WebUI/OTel logger через propagation.
        self.logger.propagate = True

    def __getattr__(self, name):
        return getattr(self.logger, name)
