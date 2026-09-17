"""Система управления настроением флота.

Отслеживает и управляет показателем настроения флота (morale). В Azur Lane корабли
тратят настроение во время боёв; слишком низкое настроение приводит к потере бонуса опыта
и негативным эффектам. Настроение восстанавливается следующими способами:
- Отдых в порту (вне общежития): +20 очков каждые 6 минут
- 1-й этаж общежития: +40 очков каждые 6 минут
- 2-й этаж общежития: +50 очков каждые 6 минут
- Бонус клятвы: дополнительные +10 очков каждые 6 минут
- Бонус онсэна: дополнительные +10 очков каждые 6 минут

Стратегии контроля настроения:
- Сохранять бонус счастья (>120): максимизация бонуса опыта
- Не допускать зелёного лица (>40): избежание штрафов
- Не допускать жёлтого лица (>30): избежание сильных штрафов
- Не допускать красного лица (>2): минимальная защита от истощения

Клиент игры имеет известный баг: при длительной непрерывной работе расчёт настроения сбивается,
поэтому требуется периодический перезапуск.
"""

from datetime import datetime, timedelta
from time import sleep

import numpy as np

from module.base.decorator import cached_property
from module.base.utils import random_normal_distribution_int
from module.config.config import AzurLaneConfig
from module.config.time_source import now as current_time
from module.exception import ScriptEnd, ScriptError, RequestHumanTakeover
from module.logger import logger

# Порог контроля настроения: при снижении ниже него запускается ожидание/отсрочка
DIC_LIMIT = {
    'keep_exp_bonus': 120,     # Сохранять бонус опыта (хорошее настроение)
    'prevent_green_face': 40,  # Не допускать зелёное лицо
    'prevent_yellow_face': 30, # Не допускать жёлтое лицо
    'prevent_red_face': 2,     # Не допускать красное лицо
}
# Скорость восстановления настроения: количество очков за каждые 6 минут
DIC_RECOVER = {
    'not_in_dormitory': 20,    # Отдых в порту
    'dormitory_floor_1': 40,   # Первый этаж общежития
    'dormitory_floor_2': 50,   # Второй этаж общежития
}
# Максимальное настроение
DIC_RECOVER_MAX = {
    'not_in_dormitory': 119,
    'dormitory_floor_1': 150,
    'dormitory_floor_2': 150,
}
OATH_RECOVER = 10    # Дополнительная скорость восстановления от клятвы
ONSEN_RECOVER = 10   # Дополнительная скорость восстановления от онсэна


class FleetEmotion:
    """Трекер настроения отдельного флота.

    Управляет значением настроения, скоростью восстановления и порогом контроля одного флота.
    Поддерживает независимую конфигурацию и режим флота открытого моря (Public Fleet).

    Attributes:
        config (AzurLaneConfig): Объект конфигурации.
        fleet (str): Индекс флота (1, 2 или 'Public').
        current (int): Текущее рассчитанное значение настроения.
    """

    def __init__(self, config, fleet):
        """
        Args:
            config (AzurLaneConfig): Объект конфигурации.
            fleet (str): Индекс флота.
        """
        self.config = config
        self.fleet = fleet
        self.current = 0

    @property
    def _key_prefix(self):
        if self.fleet == 'Public':
            return 'PublicEmotion_Fleet'
        return f'Emotion_Fleet{self.fleet}'

    @property
    def value(self):
        """
        Returns:
            int: От 0 до 150.
        """
        return getattr(self.config, f'{self._key_prefix}Value')

    @property
    def value_name(self):
        """
        Returns:
            str: Имя параметра значения настроения.
        """
        return f'{self._key_prefix}Value'

    @property
    def record(self):
        """
        Returns:
            datetime.datetime: Временная метка последней записи.
        """
        return getattr(self.config, f'{self._key_prefix}Record')

    @property
    def recover(self):
        """
        Returns:
            str: not_in_dormitory, dormitory_floor_1, dormitory_floor_2.
        """
        return getattr(self.config, f'{self._key_prefix}Recover')

    @property
    def control(self):
        """
        Returns:
            str: keep_exp_bonus, prevent_green_face, prevent_yellow_face, prevent_red_face.
        """
        return getattr(self.config, f'{self._key_prefix}Control')

    @property
    def oath(self):
        """
        Returns:
            bool: Дана ли клятва всем кораблям.
        """
        return getattr(self.config, f'{self._key_prefix}Oath')

    @property
    def onsen(self):
        """
        Returns:
            bool: Находятся ли все корабли в онсэне.
        """
        return getattr(self.config, f'{self._key_prefix}Onsen')

    @property
    def speed(self):
        """
        Returns:
            int: Скорость восстановления за 6 минут.
        """
        speed = DIC_RECOVER[self.recover]
        if self.oath:
            speed += OATH_RECOVER
        if self.onsen:
            speed += ONSEN_RECOVER
        return speed // 10

    @property
    def limit(self):
        """
        Returns:
            int: Минимальный порог контроля настроения.
        """
        return DIC_LIMIT[self.control]

    @property
    def max(self):
        """
        Returns:
            int: Максимальное значение настроения.
        """
        return DIC_RECOVER_MAX[self.recover]

    def update(self):
        """Вычисляет восстановление настроения на основе реально прошедшего времени.

        Использует непрерывный расчёт восстановления по времени, сохраняя дробную часть для накопления.
        Сервер игры точно рассчитывает восстановление по фактически прошедшему времени: каждые 6 минут
        восстанавливается speed очков.
        Прежний метод усекал восстановление через int(), и после каждого сброса метки в record()
        остаток менее 1 очка терялся, что приводило к сильной недооценке настроения при длительной работе.
        Теперь берётся целая часть, а в record() при изменении целого значения компенсируются дробные секунды,
        обеспечивая накопление остатка между итерациями.
        """
        time_diff = current_time().timestamp() - self.record.timestamp()
        time_diff = max(time_diff, 0)
        # speed — восстановление за 360 секунд; переводим в скорость speed/360 очка в секунду
        recovery = self.speed * time_diff / 360
        self.current = min(max(self.value, 0) + int(recovery), self.max)
        # Сохраняем число секунд, соответствующее дробному остатку восстановления меньше 1 очка, для компенсации в record()
        self._fractional_seconds = recovery - int(recovery)

    def get_recovered(self, expected_reduce=0):
        """Вычисляет время, когда настроение восстановится до порога контроля.

        Args:
            expected_reduce (int): Ожидаемое снижение настроения.

        Returns:
            datetime.datetime: Момент времени, когда настроение >= порогу контроля. Если уже восстановилось, возвращает текущее или прошедшее время.
        """
        if self.control == 'keep_exp_bonus' and self.recover == 'not_in_dormitory':
            logger.critical(f'[Бой] Для флота {self.fleet} одновременно выбраны контроль настроения "сохранять бонус счастья" и восстановление "в порту". Эти настройки несовместимы; проверьте параметры настроения')
            raise RequestHumanTakeover
        # При использовании книги двойного опыта на 14-4 ожидаемое снижение настроения равно 32, поэтому нельзя сохранить бонус хорошего настроения (>120)
        # Иначе это приведёт к бесконечной отсрочке задачи
        if self.control == 'keep_exp_bonus' and expected_reduce >= 29:
            expected_reduce = 29
            logger.info(f'[Настроение — флот] Для флота {self.fleet} ожидаемое снижение ограничено значением 29, '
                        f'когда контроль настроения="сохранять бонус счастья"')

        emotion_needed = self.limit + expected_reduce - self.current
        if emotion_needed <= 0:
            return current_time()
        # speed — восстановление за 360 секунд; вычисляем требуемое время восстановления в секундах
        seconds_needed = emotion_needed * 360 / self.speed
        return current_time() + timedelta(seconds=seconds_needed)

class Emotion:
    """Главный класс управления настроением.

    Координирует отслеживание, ожидание и списание настроения двух флотов (и опционально флота открытого моря).
    Перед началом кампании проверяет достаточность настроения, после боя списывает очки настроения,
    а при нехватке откладывает выполнение задачи.

    Attributes:
        total_reduced (int): Суммарно списанное настроение за текущий сеанс для выявления бага клиента.
        map_is_2x_book (bool): Используется ли книга двойного опыта (влияет на расход настроения).
        fleet_1 (FleetEmotion): Трекер настроения первого флота.
        fleet_2 (FleetEmotion): Трекер настроения второго флота.
        using_public (bool): Используется ли общий трекер настроения флота открытого моря.
    """
    total_reduced = 0
    map_is_2x_book = False

    def __init__(self, config):
        """
        Args:
            config (AzurLaneConfig): Объект конфигурации.
        """
        self.config = config
        self.fleet_1 = FleetEmotion(self.config, fleet=1)
        self.fleet_2 = FleetEmotion(self.config, fleet=2)
        self.fleets = [self.fleet_1, self.fleet_2]
        self.using_public = self._handle_public()
    
    def _handle_public(self):
        if not getattr(self.config, 'PublicEmotion_Enable'):
            return False
        
        tasks = getattr(self.config, 'PublicEmotion_Tasks')

        if not tasks:
            return False

        tasks = [task.strip() for task in tasks.split(',')]

        if self.config.task.command not in tasks:
            return False

        self.public_fleet = FleetEmotion(self.config, fleet='Public')
        return True

    @property
    def is_calculate(self):
        return 'calculate' in self.config.Emotion_Mode

    @property
    def is_ignore(self):
        return 'ignore' in self.config.Emotion_Mode

    def update(self):
        """Обновляет значения настроения. Должен вызываться перед выполнением любых действий."""
        if self.using_public:
            self.public_fleet.update()
            return
        
        for fleet in self.fleets:
            fleet.update()

    def record(self):
        """Сохраняет текущие значения настроения в конфигурации.

        Обновляет временную метку Record только при изменении целого значения настроения,
        компенсируя Record на число секунд, соответствующее fractional_seconds,
        чтобы дробный остаток восстановления накапливался при следующем update().

        Примечание: FleetEmotion.value и FleetEmotion.record являются @property,
        считывающимися напрямую из self.config. При setattr в config свойства обновляются автоматически.
        """
        if self.using_public:
            fleet = self.public_fleet
            old_value = fleet.value
            new_value = fleet.current
            # Сбрасываем временную метку только при изменении целого значения и компенсируем дробные секунды
            if new_value != old_value:
                record_time = current_time().replace(microsecond=0)
                fractional = getattr(fleet, '_fractional_seconds', 0)
                if fractional > 0:
                    # Компенсируем число секунд, соответствующее fractional_seconds
                    record_time = record_time - timedelta(seconds=fractional * 360 / fleet.speed)
                with self.config.multi_set():
                    setattr(self.config, fleet.value_name, new_value)
                    setattr(self.config, fleet.value_name.replace('Value', 'Record'), record_time)
            return

        with self.config.multi_set():
            for fleet in self.fleets:
                old_value = fleet.value
                new_value = fleet.current
                if new_value != old_value:
                    record_time = current_time().replace(microsecond=0)
                    fractional = getattr(fleet, '_fractional_seconds', 0)
                    if fractional > 0:
                        record_time = record_time - timedelta(seconds=fractional * 360 / fleet.speed)
                    setattr(self.config, fleet.value_name, new_value)
                    setattr(self.config, fleet.value_name.replace('Value', 'Record'), record_time)

    def show(self):
        """Отображает текущее рассчитанное настроение (включая восстановление по времени), а не последнее сохранённое значение."""
        if self.using_public:
            logger.attr(f'Настроение флота в открытом море', self.public_fleet.current)
            return

        for fleet in self.fleets:
            logger.attr(f'Настроение флота {fleet.fleet}', fleet.current)

    @property
    def reduce_per_battle(self):
        if self.map_is_2x_book:
            return 4
        else:
            return 2

    @property
    def reduce_per_battle_before_entering(self):
        if self.map_is_2x_book:
            return 4
        elif self.config.Campaign_Use2xBook:
            return 4
        else:
            return 2
    
    @property
    def reduce_shipwreck(self):
        return 10

    def _check_reduce(self, battle):
        """Проверяет снижение настроения в результате боёв.

        Returns:
            recovered (datetime): Ожидаемое время восстановления.
            delay (bool): Требуется ли задержка.
        """
        if self.using_public:
            reduce = battle * self.reduce_per_battle_before_entering
            logger.info(f'[Настроение — проверка] Ожидаемое снижение настроения: {reduce}')

            self.update()
            self.record()
            self.show()
            recovered = self.public_fleet.get_recovered(reduce)
            delay = recovered > current_time()
            return recovered, delay

        method = self.config.Fleet_FleetOrder

        if method == 'fleet1_mob_fleet2_boss':
            battle = (battle - 1, 1)
        elif method == 'fleet1_boss_fleet2_mob':
            battle = (1, battle - 1)
        elif method == 'fleet1_all_fleet2_standby':
            battle = (battle, 0)
        elif method == 'fleet1_standby_fleet2_all':
            battle = (0, battle)
        else:
            raise ScriptError(f'Неизвестный порядок флотов: {method}')

        battle = tuple(np.array(battle) * self.reduce_per_battle_before_entering)
        logger.info(f'[Настроение — проверка] Ожидаемое снижение настроения: {battle}')

        self.update()
        self.record()
        self.show()
        recovered = max([f.get_recovered(b) for f, b in zip(self.fleets, battle)])
        delay = recovered > current_time()
        return recovered, delay

    def check_reduce(self, battle):
        """Проверяет настроение перед входом в кампанию.

        Args:
            battle (int): Количество боёв в текущей кампании.

        Raise:
            ScriptEnd: Откладывает текущую задачу во избежание проблем с контролем настроения.
        """
        if not self.is_calculate:
            return

        recovered, delay = self._check_reduce(battle)
        if delay:
            logger.info('[Настроение — задержка] Текущая задача отложена, чтобы избежать проблем с контролем настроения')
            self.config.task_delay(target=recovered)
            raise ScriptEnd('[Настроение — задержка] Контроль настроения')

    def wait(self, fleet_index):
        """Ожидает восстановления настроения указанного флота. Должен вызываться перед входом в любой бой.

        Args:
            fleet_index (int): Номер флота (1 или 2).
        """
        self.update()
        self.record()
        self.show()
        if self.using_public:
            fleet = self.public_fleet
        else:
            fleet = self.fleets[fleet_index - 1]

        recovered = fleet.get_recovered(expected_reduce=self.reduce_per_battle)
        if recovered > current_time():
            logger.hr('Ожидание восстановления настроения')
            if self.using_public:
                logger.info(f'[Настроение — ожидание] Настроение флота в открытом море восстановится до {fleet.limit} к {recovered}')
            else:
                logger.info(f'[Настроение — ожидание] Настроение флота {fleet_index} восстановится до {fleet.limit} к {recovered}')

            while 1:
                if current_time() > recovered:
                    break

                logger.attr('Ожидание до', recovered)
                sleep(60)

    def reduce(self, fleet_index, shipwreck=False):
        """Снижает значение настроения указанного флота. Должен вызываться после завершения боя.
        Сервер игры списывает настроение сразу после загрузки боя.

        Args:
            fleet_index (int): Номер флота (1 или 2).
            shipwreck (bool): Потерпел ли флот крушение (потопление корабля).
        """
        logger.hr('Снижение настроения')
        self.update()

        if self.using_public:
            fleet = self.public_fleet
        else:
            fleet = self.fleets[fleet_index - 1]

        if not shipwreck:
            fleet.current -= self.reduce_per_battle
            self.total_reduced += self.reduce_per_battle
        else:
            fleet.current -= self.reduce_shipwreck
            self.total_reduced += self.reduce_shipwreck
        self.record()
        self.show()

    @cached_property
    def bug_threshold(self):
        """
        Returns:
            int: Порог срабатывания бага настроения.
        """
        return random_normal_distribution_int(55, 105, n=2)

    def bug_threshold_reset(self):
        """Сбрасывает порог после срабатывания бага настроения."""
        del self.__dict__['bug_threshold']

    def triggered_bug(self):
        """Определяет баг расчёта настроения в клиенте Azur Lane.
        При длительной работе клиент не может корректно рассчитать настроение,
        требуется перезапуск клиента игры для его обновления.
        """
        logger.attr('Ошибка настроения', f'{self.total_reduced}/{self.bug_threshold}')
        if self.total_reduced >= self.bug_threshold:
            logger.info('[Настроение — ошибка] Клиент Azur Lane неправильно рассчитал настроение. '
                        'После длительной работы нужно перезапустить игровой клиент, чтобы обновить настроение.')
            self.total_reduced = 0
            self.bug_threshold_reset()
            return True
        else:
            return False
