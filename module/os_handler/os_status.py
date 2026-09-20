"""Модуль отслеживания состояния Операции «Сирена».

Управляет информацией о состоянии режима Операции «Сирена», включая отслеживание
показателей жетонов зон (жёлтых/фиолетовых монет) по OCR, идентификацию типов задач,
расчёт времени восстановления (CD) подзадач в реальном времени и фиксацию
связанных ресурсов в журнале.
"""
# Этот файл управляет состоянием режима Operation Siren.
# Он отслеживает морские жетоны (жёлтые/фиолетовые), распознаёт типы задач и в реальном времени рассчитывает откат (CD) подзадач.
import threading
import typing as t
from datetime import timedelta

import module.config.server as server
from module.application.errors import StorageError
from module.base.timer import Timer
from module.config.config import Function
from module.config.time_source import now as current_time
from module.config.utils import get_server_next_update
from module.logger import logger
from module.map.map_grids import SelectedGrids
from module.ocr.ocr import Digit
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2, GET_SHIP
from module.os_shop.assets import OS_SHOP_CHECK, OS_SHOP_PURPLE_COINS, SHOP_PURPLE_COINS, SHOP_YELLOW_COINS
from module.ui.ui import UI
from module.log_res.log_res import LogRes


if server.server != 'jp':
    OCR_SHOP_YELLOW_COINS = Digit(SHOP_YELLOW_COINS, letter=(239, 239, 239), threshold=160, name='OCR_SHOP_YELLOW_COINS')
else:
    OCR_SHOP_YELLOW_COINS = Digit(SHOP_YELLOW_COINS, letter=(201, 201, 201), threshold=200, name='OCR_SHOP_YELLOW_COINS')
OCR_SHOP_PURPLE_COINS = Digit(SHOP_PURPLE_COINS, letter=(255, 255, 255), name='OCR_SHOP_PURPLE_COINS')
OCR_OS_SHOP_PURPLE_COINS = Digit(OS_SHOP_PURPLE_COINS, letter=(255, 255, 255), name='OCR_OS_SHOP_PURPLE_COINS')


class OSStatus(UI):
    _shop_yellow_coins = 0
    _shop_purple_coins = 0
    _cache_lock = threading.Lock()
    _last_yellow_coins = 0

    @property
    def is_in_task_explore(self) -> bool:
        return self.config.task.command == 'OpsiExplore'

    @property
    def is_in_task_cl1_leveling(self) -> bool:
        return self.config.task.command == 'OpsiHazard1Leveling'

    @property
    def is_running_cl1_leveling(self) -> bool:
        """Определить, является ли текущий контекст выполнения прокачкой в зоне коррозии 1 (CL1)."""
        return (
            self.is_in_task_cl1_leveling
            or getattr(self.config, '_bind_task_override', None) == 'OpsiHazard1Leveling'
        )

    @property
    def is_in_task_meow(self) -> bool:
        """Определить, является ли текущая задача фармом Meowfficer."""
        return self.config.task.command == 'OpsiMeowfficerFarming'

    @property
    def is_cl1_enabled(self) -> bool:
        return self.config.is_task_enabled('OpsiHazard1Leveling')

    @property
    def is_cl1_mode_enabled(self) -> bool:
        """Определить, включены ли стратегии зоны коррозии 1, включая интеллектуальное планирование и режим делегирования."""
        is_smart_scheduling_enabled = getattr(self, 'is_smart_scheduling_enabled', None)
        return self.is_cl1_enabled or (
            is_smart_scheduling_enabled is not None
            and is_smart_scheduling_enabled()
        )

    @property
    def is_meow_enabled(self) -> bool:
        """Определить, включена ли задача фарма Meowfficer."""
        return self.config.is_task_enabled('OpsiMeowfficerFarming')

    @property
    def cl1_enough_yellow_coins(self) -> bool:
        return self.get_yellow_coins() >= self.config.cross_get(
            keys='OpsiHazard1Leveling.OpsiHazard1Leveling.OperationCoinsPreserve')

    @property
    def nearest_task_cooling_down(self) -> t.Optional[Function]:
        """
        If having any tasks cooling down,
        such as recon scan cooldown and submarine call cooldown.
        """
        now = current_time()
        update = get_server_next_update('00:00')
        cd_tasks = [
            'OpsiObscure',
            'OpsiAbyssal',
            'OpsiStronghold',
            'OpsiDaily',
        ]

        def func(task: Function):
            if task.command in cd_tasks and task.enable:
                if task.next_run != update and task.next_run - now <= timedelta(minutes=60):
                    return True

            return False

        tasks = SelectedGrids(self.config.pending_task + self.config.waiting_task).filter(func).sort('next_run')
        return tasks.first_or_none()

    def get_yellow_coins(self) -> int:
        yellow_coins = 0
        timeout = Timer(5, count=10).start()  # Увеличиваем тайм-аут и число повторных попыток
        last_valid_value = None
        
        for _ in self.loop():
            # End
            if self.appear_then_click(GET_ITEMS_1, offset=True, interval=1):
                timeout.reset()
                continue
            if self.appear_then_click(GET_ITEMS_2, offset=True, interval=1):
                timeout.reset()
                continue
            if self.appear_then_click(GET_SHIP, interval=1):
                timeout.reset()
                continue

            current_value = OCR_SHOP_YELLOW_COINS.ocr(self.device.image)
            if timeout.reached():
                logger.warning('[Операция «Сирена» — состояние] Истекло время получения жёлтых монет')
                break

            if current_value == 0:
                # OCR may get 0 when amount is not immediately loaded
                # Or when popups are obscuring the top bar
                logger.info('[Операция «Сирена» — состояние] Жёлтые монеты равны 0: возможно, ошибка OCR или экран ещё не загрузился')
                continue
            else:
                # Проверяем стабильность распознавания: подтверждаем значение только после двух одинаковых результатов подряд
                if last_valid_value is None:
                    last_valid_value = current_value
                    self.device.sleep(0.2)  # Короткая пауза перед повторной проверкой
                elif last_valid_value == current_value:
                    yellow_coins = current_value
                    break
                else:
                    last_valid_value = current_value
                    self.device.sleep(0.2)
        
        # Если валидное значение так и не получено, используем последнее кэшированное значение (потокобезопасно)
        with self._cache_lock:
            if yellow_coins == 0:
                logger.info(f'[Операция «Сирена» — состояние] Используется кэшированное значение жёлтых монет: {self._last_yellow_coins}')
                yellow_coins = self._last_yellow_coins
            
            # Кэшируем текущее значение для резервного использования
            self._last_yellow_coins = yellow_coins
        
        LogRes(self.config).YellowCoin = yellow_coins
        logger.info(f'[Операция «Сирена» — состояние] Жёлтые монеты: {yellow_coins}')

        return yellow_coins

    def get_purple_coins(self) -> int:
        if self.appear(OS_SHOP_CHECK):
            purple_coins = OCR_OS_SHOP_PURPLE_COINS.ocr(self.device.image)
        else:
            purple_coins = OCR_SHOP_PURPLE_COINS.ocr(self.device.image)
        LogRes(self.config).PurpleCoin = purple_coins
        return purple_coins

    def os_shop_get_coins(self):
        self._shop_yellow_coins = self.get_yellow_coins()
        self._shop_purple_coins = self.get_purple_coins()
        logger.info(f'[Операция «Сирена» — состояние] Жёлтые монеты: {self._shop_yellow_coins}, фиолетовые монеты: {self._shop_purple_coins}')

        # Записываем снимок ваучеров в базу данных для графика их изменения в WebUI
        try:
            instance_name = getattr(self.config, 'config_name', 'default')
            source = 'cl1' if self.is_running_cl1_leveling else ('meow' if self.is_in_task_meow else 'other')
            from module.application.runtime_storage import get_runtime_storage

            get_runtime_storage().record_coins_snapshot(
                instance_name,
                self._shop_yellow_coins,
                purple_coins=self._shop_purple_coins,
                source=source,
            )
            # LogRes уже записал значение в config.modified; здесь сохраняем его на диск
            self.config.save()
        except StorageError:
            raise
        except Exception:
            logger.exception('[Операция «Сирена» — состояние] Не удалось записать снимок ваучеров')
