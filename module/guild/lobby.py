"""Обработчик холла гильдии, отвечающий за сбор отчётов, вход и проверку информации об участниках.

Определяет доступные действия по цветовому распознаванию красных точек уведомлений и статусов кнопок.
"""

import numpy as np

from module.base.button import Button
from module.base.timer import Timer
from module.base.utils import area_offset, color_similarity_2d
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3
from module.guild.assets import *
from module.guild.base import GuildBase
from module.logger import logger
from module.map_detection.utils import Points
from module.ui.assets import GUILD_CHECK


class GuildLobby(GuildBase):
    def guild_lobby_get_report(self):
        """
        Получение кнопки входа в отчёты гильдии.

        Returns:
            Button: Кнопка перехода к отчётам гильдии, либо None при её отсутствии.
        """
        # Ищем красный цвет в области GUILD_REPORT_AVAILABLE
        image = color_similarity_2d(self.image_crop(GUILD_REPORT_AVAILABLE, copy=False), color=(255, 8, 8))
        points = np.array(np.where(image > 221)).T[:, ::-1]
        if len(points):
            # Координаты центра красной точки
            points = Points(points).group(threshold=40) + GUILD_REPORT_AVAILABLE.area[:2]
            # Смещаемся к центру значка отчёта
            area = area_offset((-51, -45, -13, 0), offset=points[0])
            return Button(area=area, color=(255, 255, 255), button=area, name='GUILD_REPORT')
        else:
            return None

    def _guild_lobby_collect(self, skip_first_screenshot=True):
        """
        Сбор наград за отчёты в холле гильдии.

        Если награды за отчёты доступны, выполняет их получение. Если уже на странице page_guild,
        но не в холле, операция завершится по тайм-ауту и награда будет собрана при следующем запуске.
        Награды накапливаются в очереди, поэтому не требуют немедленного сбора.

        Pages:
            in: Любая страница
            out: Любая страница
        """
        confirm_timer = Timer(1.5, count=3).start()
        click_timer = Timer(3)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if click_timer.reached() and self.appear(GUILD_CHECK, offset=(20, 20)):
                button = self.guild_lobby_get_report()
                if button is not None:
                    self.device.click(button)
                    click_timer.reset()

            if self.appear_then_click(GUILD_REPORT_CLAIM, threshold=30, interval=3):
                confirm_timer.reset()
                continue

            if self.appear_then_click(GET_ITEMS_1, offset=(30, 30), interval=2):
                confirm_timer.reset()
                continue

            if self.appear_then_click(GET_ITEMS_2, offset=(30, 30), interval=2):
                confirm_timer.reset()
                continue

            if self.appear_then_click(GET_ITEMS_3, offset=(30, 30), interval=2):
                confirm_timer.reset()
                continue

            if self.appear(GUILD_REPORT_CLAIMED, threshold=30, interval=3):
                self.device.click(GUILD_REPORT_CLOSE)
                confirm_timer.reset()
                continue

            # Завершение
            if self.appear(GUILD_CHECK, offset=(20, 20)):
                if confirm_timer.reached():
                    break
            else:
                confirm_timer.reset()

    def guild_lobby(self):
        """
        Выполнение всех операций в холле гильдии.

        Pages:
            in: GUILD_LOBBY
            out: GUILD_LOBBY
        """
        logger.hr('Зал гильдии', level=1)
        self._guild_lobby_collect()
        logger.info('[Гильдия — зал] Сбор наград в зале гильдии завершён')
