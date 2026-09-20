"""Модуль диспетчеризации бесплатных наград и бонусов (Freebies).

Обеспечивает единое управление подмодулями бесплатных наград в строгом порядке:
- Сбор наград боевого пропуска (Battle Pass)
- Сбор ключей данных (Data Key)
- Получение наград из почты (Mail)
- Получение наборов снабжения (Supply Pack)

Каждый подмодуль запускается в соответствии с настройками пользователя; по завершении планируется следующее время запуска.
"""
from module.base.base import ModuleBase
from module.freebies.battle_pass import BattlePass
from module.freebies.data_key import DataKey
from module.freebies.mail_white import MailWhite
from module.freebies.supply_pack import SupplyPack_250814
from module.logger import logger


class Freebies(ModuleBase):
    """Главный диспетчер задач бесплатных наград и бонусов.

    Последовательно запускает сбор боевого пропуска, ключей данных, почты и наборов снабжения.
    Каждый подмодуль независимо проверяет флаги в пользовательской конфигурации.
    После завершения всех активных подмодулей время следующего запуска откладывается
    до момента серверного сброса через task_delay(server_update=True).
    """
    def run(self):
        """Запустить все включенные модули бесплатных наград."""
        if self.config.BattlePass_Collect:
            logger.hr('Боевой пропуск', level=1)
            BattlePass(self.config, self.device).run()

        if self.config.DataKey_Collect:
            logger.hr('Ключи данных', level=1)
            DataKey(self.config, self.device).run()

        logger.hr('Почта', level=1)
        MailWhite(self.config, self.device).run()

        if self.config.SupplyPack_Collect:
            logger.hr('Наборы снабжения', level=1)
            SupplyPack_250814(self.config, self.device).run()

        self.config.task_delay(server_update=True)
