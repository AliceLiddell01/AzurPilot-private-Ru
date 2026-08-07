from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class TestCommonGameMisc01RuntimeMessages:
    def test_representative_runtime_messages_are_russian(self):
        expected = {
            "module/azur_stats/image/auto_search_reward.py": (
                "[AzurStats — награды автопоиска]",
            ),
            "module/coalition/coalition.py": (
                "[Коалиция] Для события",
                "Не заполнены аргументы Coalition.",
            ),
            "module/exercise/combat.py": (
                "[Учения — бой] Подготовка к бою",
                "[Учения — бой] Выполнение боя",
            ),
            "module/gacha/gacha_reward.py": (
                "[Строительство — подготовка]",
                "'wishing_well' недоступен",
            ),
            "module/game_setting/player_prefs.py": (
                "[GameSettings] Рекомендуемые локальные настройки игры уже применены",
                "ADB не получил права root",
            ),
            "module/guild/logistics.py": (
                "[Гильдия — логистика] Предметы обмена:",
            ),
            "module/meowfficer/buy.py": (
                "[Комофицер — покупка] Сегодня уже куплено",
                "[Комофицер — избыток]",
            ),
            "module/meta_reward/meta_reward.py": (
                "[META — награды] Получение наград META завершено",
                "Получение синхронизации META",
            ),
            "module/minigame/minigame.py": (
                "[Мини-игра] Запуск",
            ),
            "module/private_quarters/private_quarters.py": (
                "[Личные покои] Конфигурация задачи:",
            ),
            "module/raid/raid.py": (
                "Неизвестное имя рейда:",
                "[Рейд — PT]",
            ),
            "module/shipyard/shipyard_reward.py": (
                "[Верфь — стоимость]",
            ),
            "module/sos/sos.py": (
                "[SOS] Неизвестная глава SOS:",
                "[SOS] Выбор сигнала главы",
            ),
            "module/war_archives/war_archives.py": (
                "Архивы сейчас недоступны",
                "[Архивы] Выбран архив, область",
            ),
        }

        for path, messages in expected.items():
            text = source(path)
            for message in messages:
                assert message in text, (path, message)


class TestCommonGameMisc01TechnicalContracts:
    def test_game_settings_keeps_safe_player_prefs_and_adb_contracts(self):
        text = source("module/game_setting/player_prefs.py")
        required = (
            "STORY_SPEED_VALUE = 9",
            "'QUICK_CHANGE_EQUIP': 0",
            "'world_sub_auto_call': 0",
            "'/data/user/0/{self.package}/shared_prefs'",
            "['root']",
            "['unroot']",
            "'uid=0(root)'",
            "['exec-out', 'cat', remote]",
            "['exec-in', 'sh', '-c', f'cat > {remote}']",
            "['mv', temporary, prefs]",
            "['chown', f'{metadata.uid}:{metadata.gid}', remote]",
            "['chmod', metadata.mode, remote]",
            "['chcon', metadata.context, remote]",
        )
        for token in required:
            assert token in text, token

    def test_gacha_resource_decisions_are_preserved(self):
        text = source("module/gacha/gacha_reward.py")
        required = (
            "gold_cost = 600",
            "cube_cost = 1",
            "gold_cost = 1500",
            "cube_cost = 2",
            "if actual_pool == \"event\" and self.config.Gacha_UseTicket:",
            "if self.config.Gacha_Amount > self.build_ticket_count:",
            "self.build_coin_count -= gold_total",
            "self.build_cube_count -= cube_total",
            "if self.config.Gacha_UseDrill:",
        )
        for token in required:
            assert token in text, token

    def test_guild_resource_and_retry_contracts_are_preserved(self):
        logistics = source("module/guild/logistics.py")
        for token in (
            "GUILD_SUPPLY_MAX_RETRY = 2",
            "GUILD_EXCHANGE_BUG_RETRY = 5",
            "if GUILD_EXCHANGE_LIMIT.ocr(self.device.image) <= 0:",
            "selected = EXCHANGE_FILTER.apply(items, func=lambda item: item.enough)",
            "self.device.click(button)",
        ):
            assert token in logistics, token

        operations = source("module/guild/operations.py")
        for token in (
            "threshold = total * self.config.GuildOperation_JoinThreshold",
            "if current <= threshold:",
            "if self.config.GuildOperation_BossFleetRecommend:",
            "if self.config.GuildOperation_AttackBoss:",
        ):
            assert token in operations, token

    def test_raid_ticket_and_fleet_contracts_are_preserved(self):
        raid = source("module/raid/raid.py")
        for token in (
            "if self.config.Raid_UseTicket:",
            "self.device.click(TICKET_USE_CONFIRM)",
            "self.device.click(TICKET_USE_CANCEL)",
            "Submarine_Fleet=1",
            "Submarine_Mode='every_combat'",
            "return self.config.Campaign_Event == 'raid_20240328'",
        ):
            assert token in raid, token

        scuttle = source("module/raid/scuttle.py")
        for token in (
            "scanner = ShipScanner(level=(1, 31), fleet=0, status='free')",
            "ship = self.get_common_rarity_ship(index='vanguard')",
            "ship = self.get_common_rarity_ship(index='main')",
            "min(ship, key=lambda s: (s.level, -s.emotion)).button",
        ):
            assert token in scuttle, token


class TestCommonGameMisc01ResourceSensitiveContracts:
    def test_meowfficer_buy_and_enhance_limits_are_preserved(self):
        buy = source("module/meowfficer/buy.py")
        for token in (
            "BUY_MAX = 15",
            "BUY_PRIZE = 1500",
            "overflow_count = -(-(coins - overflow_coins) // BUY_PRIZE)",
            "count = min(overflow_count, today_left)",
            "free = 1 if remain == total else 0",
        ):
            assert token in buy, token

        enhance = source("module/meowfficer/enhance.py")
        for token in (
            "if self.config.MeowfficerTrain_MaxFeedLevel < 1:",
            "elif self.config.MeowfficerTrain_MaxFeedLevel > 30:",
            "if index >= 10:",
            "if not (1 <= self.config.MeowfficerTrain_EnhanceIndex <= 12):",
            "if coins < 1000:",
            "if self._meow_get_level() >= 30:",
        ):
            assert token in enhance, token

        train = source("module/meowfficer/train.py")
        for token in (
            "for i, j in ((0, 2), (1, 1)):",
            "if common_sum > 20:",
            "self.meow_queue(ascending=False)",
        ):
            assert token in train, token

    def test_private_quarters_purchase_thresholds_are_preserved(self):
        shop = source("module/private_quarters/shop.py")
        for token in (
            "if 24000 > self._currency:",
            "if 210 > self.gems:",
            "FILTER.apply(items, self.shop_check_item)",
        ):
            assert token in shop, token

        clerk = source("module/private_quarters/clerk.py")
        for token in (
            "PRIVATE_QUARTERS_SHOP_AMOUNT_MAX",
            "PRIVATE_QUARTERS_SHOP_CONFIRM_AMOUNT",
            "for _ in range(12):",
        ):
            assert token in clerk, token

    def test_shipyard_price_and_index_contracts_are_preserved(self):
        reward = source("module/shipyard/shipyard_reward.py")
        for token in (
            "(1, 2):               0",
            "(3, 4):               150",
            "return 1500",
            "(3, 4, 5, 6):         600",
            "return 6000",
            "if (total + cost) > self._coin_count:",
        ):
            assert token in reward, token

        ui = source("module/shipyard/ui.py")
        for token in (
            "if series == 1:",
            "return index + 42",
            "if series not in range(1, 10):",
        ):
            assert token in ui, token
