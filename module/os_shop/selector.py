"""Логика фильтрации и выбора предметов в магазине Operation Siren.

Разбирает категории товаров на основе регулярных выражений и принимает решения о покупке
согласно настроенным пользователем фильтрам. Поддерживает фильтрацию магазина Акаши и предустановки магазина OS.
"""
import re
from typing import List
from module.config.config_generated import GeneratedConfig
from module.os_shop.preset import OS_SHOP
from module.os_shop.item import OSShopItem as Item
from module.base.filter import Filter

# Правило regex-сопоставления названий товаров
FILTER_REGEX = re.compile(
    '^(actionpoint|crystallizedheatresistantsteel|developmentmaterial'
    '|energystoragedevice|geardesignplan|gearpart|logger|metaredbook'
    '|nanoceramicalloy|neuroplasticprostheticarm|ordnancetestingreport'
    '|platerandom|purplecoins|repairpack|supercavitationgenerator|tuningsample'
    '|tuning)'

    '(20|50|100|prototype|specialized|abyssal|obscure|full2|full|triple2|triple|2'
    '|combat|offence|survival)?'

    '(t[1-6])?$',
    flags=re.IGNORECASE)
FILTER_ATTR = ('group', 'sub_genre', 'tier')
FILTER = Filter(FILTER_REGEX, FILTER_ATTR)


class Selector():
    """Базовый селектор товаров магазина.

    Предоставляет предварительную обработку предметов, проверку монет, валидацию количества и фильтрацию.
    """

    def pretreatment(self, items) -> List[Item]:
        """Предварительно обработать список предметов, извлекая информацию о типе из названий.

        С помощью регулярных выражений извлекает атрибуты group, sub_genre и tier предмета.

        Args:
            items: Список предметов для предварительной обработки.

        Returns:
            list[Item]: Список обработанных предметов, содержащий только успешно распознанные.
        """
        _items = []
        for item in items:
            item.group, item.sub_genre, item.tier = None, None, None
            result = re.search(FILTER_REGEX, item.name.lower())
            if result:
                item.group, item.sub_genre, item.tier = [group.lower()
                                                         if group is not None else None
                                                         for group in result.groups()]
                _items.append(item)

        return _items

    def enough_coins_in_akashi(self, item) -> bool:
        """Проверить, достаточно ли монет в магазине Акаши для покупки предмета.

        Args:
            item: Проверяемый предмет.

        Returns:
            bool: True, если монет достаточно, иначе False.
        """
        if item.cost == 'YellowCoins' and item.price <= self._shop_yellow_coins:
            return True
        if item.cost == 'PurpleCoins' and item.price <= self._shop_purple_coins:
            return True

        return False

    def check_cl1_purple_coins(self, item) -> bool:
        """Проверить, разрешена ли покупка фиолетовых монет в режиме CL1.

        Args:
            item: Проверяемый предмет.

        Returns:
            bool: True, если покупка разрешена; False, если это фиолетовые монеты в режиме CL1.
        """
        return not (self.is_cl1_mode_enabled and item.name == 'PurpleCoins')

    def check_item_count(self, item) -> bool:
        """Проверить валидность количества предмета.

        Args:
            item: Проверяемый предмет.

        Returns:
            bool: True, если количество валидно (текущее >= 1, общее >= 1, текущее не превышает общее).
        """
        return item.count >= 1 and item.total_count >= 1 and item.count <= item.total_count

    def items_filter_in_akashi_shop(self, items) -> List[Item]:
        """Отфильтровать доступные для покупки предметы в магазине Акаши.

        Фильтрует предметы согласно режиму CL1 или общей конфигурации, проверяя баланс монет.

        Args:
            items: Список предметов для фильтрации.

        Returns:
            list[Item]: Список доступных для покупки предметов.
        """
        items = self.pretreatment(items)
        if getattr(self, 'is_running_cl1_leveling', False):
            parser = self.config.OpsiHazard1Leveling_Cl1Filter
            if not parser:
                parser = 'ActionPoint'
        else:
            parser = self.config.OpsiGeneral_AkashiShopFilter
            if not parser.strip():
                parser = GeneratedConfig.OpsiGeneral_AkashiShopFilter
        FILTER.load(parser)
        return FILTER.applys(items, funcs=[self.enough_coins_in_akashi])

    def items_filter_in_os_shop(self, items) -> List[Item]:
        """Отфильтровать доступные для покупки предметы в магазине Operation Siren.

        Фильтрует предметы по предустановленному или пользовательскому фильтру,
        проверяя ограничения CL1 для фиолетовых монет и валидность количества.

        Args:
            items: Список предметов для фильтрации.

        Returns:
            list[Item]: Список доступных для покупки предметов.
        """
        items = self.pretreatment(items)
        preset = self.config.OpsiShop_PresetFilter
        parser = ''
        if preset == 'custom':
            parser = self.config.OpsiShop_CustomFilter
            if not parser.strip():
                parser = OS_SHOP[GeneratedConfig.OpsiShop_PresetFilter]
        else:
            parser = OS_SHOP[preset]
        FILTER.load(parser)
        return FILTER.applys(items, funcs=[self.check_cl1_purple_coins, self.check_item_count])
