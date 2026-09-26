"""Обработчик анимации поиска врагов.

Обрабатывает анимацию поиска врагов (разведки), появляющуюся после перемещения по карте.
Когда флот перемещается по карте, игра воспроизводит анимацию обнаружения врагов;
данный модуль отслеживает появление и исчезновение этой анимации, гарантируя продолжение сценария только после её завершения.

Наследуется от InfoHandler, используется совместно с AutoSearchHandler.
"""

from module.base.decorator import del_cached_property
from module.base.timer import Timer
from module.exception import CampaignEnd
from module.handler.assets import *
from module.handler.info_handler import InfoHandler
from module.logger import logger
from module.map.assets import *
from module.ui.assets import CAMPAIGN_CHECK, EVENT_CHECK, SP_CHECK


class EnemySearchingHandler(InfoHandler):
    """Обработчик анимации поиска врагов.

    Определяет появление и исчезновение анимации поиска (разведки) врагов на карте,
    а также обрабатывает сопутствующие ситуации (завершение этапа, срочные заказы, сюжетные окна и т. д.).

    Вызывается после действий на карте, дожидаясь завершения анимации поиска врагов перед следующими действиями.

    Attributes:
        MAP_ENEMY_SEARCHING_OVERLAY_TRANSPARENCY_THRESHOLD (float):
            Порог прозрачности красного перекрытия, выше которого анимация считается активной. Обычное значение: (0.70, 0.80).
        MAP_ENEMY_SEARCHING_TIMEOUT_SECOND (int):
            Тайм-аут ожидания анимации поиска (в секундах).
        in_stage_timer (Timer): Таймер проверки нахождения на странице этапа для защиты от ложных срабатываний.
        stage_entrance: Идентификатор входа на этап.
        map_is_100_percent_clear (bool): Зачищена ли карта на 100%, переопределяется в fast_forward.py.
    """
    MAP_ENEMY_SEARCHING_OVERLAY_TRANSPARENCY_THRESHOLD = 0.5  # Обычное значение: (0.70, 0.80)
    MAP_ENEMY_SEARCHING_TIMEOUT_SECOND = 5
    in_stage_timer = Timer(0.5, count=2)
    stage_entrance = None

    map_is_100_percent_clear = False  # Будет переопределено в fast_forward.py

    def enemy_searching_color_initial(self):
        """Инициализирует базовые цветовые значения для анимации поиска врагов.

        Подклассы могут переопределять этот метод для загрузки цветовых данных из текущего снимка перед проверкой.
        """
        pass

    def enemy_searching_appear(self):
        """Проверяет появление анимации поиска врагов.

        Определяет отображение анимации поиска на экране с помощью сопоставления шаблона и анализа яркости.

        Returns:
            bool: Появилась ли анимация поиска.
        """
        if not self.is_in_map():
            return False

        if MAP_ENEMY_SEARCHING.match_luma(self.device.image, offset=(5, 5)):
            return True

        return False

    def handle_enemy_flashing(self):
        """Ожидает исчезновения анимации мигания врагов.

        После окончания анимации поиска врагов их значки на карте кратковременно мигают.
        Метод ожидает завершения мигания через фиксированную задержку.
        """
        self.device.sleep(1.2)

    def handle_in_stage(self):
        """Определяет и обрабатывает возврат на страницу выбора этапа.

        После завершения боя или зачистки карты игра возвращается на страницу выбора этапа.
        Метод использует таймер для предотвращения ложных срабатываний во время переходов между экранами.

        Returns:
            bool: Всегда возвращает False (штатная ситуация).

        Raises:
            CampaignEnd: Выбрасывается после подтверждения возврата на страницу этапа, завершая текущую кампанию.
        """
        if self.is_in_stage():
            if self.in_stage_timer.reached():
                logger.info('[Обработчик — поиск] Возврат на страницу этапа')
                self.ensure_no_info_bar(timeout=1.2)
                raise CampaignEnd('[Обработчик — поиск] Возврат на страницу этапа')
            else:
                return False
        else:
            if self.appear(MAP_PREPARATION, offset=(20, 20)) or self.appear(FLEET_PREPARATION, offset=(20, 50)):
                self.device.click(MAP_PREPARATION_CANCEL)
            self.in_stage_timer.reset()
            return False

    def is_in_stage_page(self):
        """Проверяет, находится ли экран на странице выбора этапа (кампания/событие/SP).

        Returns:
            bool: Находится ли на странице выбора этапа.
        """
        for check in [CAMPAIGN_CHECK, EVENT_CHECK, SP_CHECK]:
            if self.appear(check, offset=(20, 20)):
                return True
        return False

    def is_stage_page_has_entrance(self):
        """Проверяет наличие входов на этапы, то есть полную загрузку страницы выбора этапа.

        Определяет статус загрузки страницы через извлечение изображений названий этапов по OCR.

        Returns:
            bool: Видны ли входы на этапы (страница загружена полностью).
        """
        # campaign_extract_name_image находится в CampaignOcr
        try:
            if hasattr(self, 'campaign_extract_name_image'):
                del_cached_property(self, '_stage_image')
                del_cached_property(self, '_stage_image_gray')
                if not len(self.campaign_extract_name_image(self.device.image)):
                    return False
        except IndexError:
            return False

        return True

    def is_in_stage(self):
        """Проверяет полный возврат на страницу выбора этапа.

        Объединяет проверку типа экрана и видимость входов на этапы.

        Returns:
            bool: Произошёл ли полный возврат на страницу этапа.
        """
        if not self.is_in_stage_page():
            return False
        if not self.is_stage_page_has_entrance():
            return False
        return True

    def is_in_map(self):
        """Проверяет, находится ли экран в интерфейсе карты.

        Returns:
            bool: Находится ли на карте.
        """
        return self.appear(IN_MAP)

    def is_event_animation(self):
        """
        Проверяет, проигрывается ли анимация события (например, после победы над врагом).

        Returns:
            bool: Проигрывается ли анимация.
        """
        return False

    def handle_auto_search_exit(self, drop=None) -> bool:
        """
        Метод-заглушка, переопределяемый в AutoSearchHandler.
        AutoSearchHandler наследует EnemySearchingHandler,
        однако handle_in_map_with_enemy_searching() вызывает handle_auto_search_exit() для обработки непредвиденных ситуаций.
        """
        return False

    def handle_in_map_with_enemy_searching(self, drop=None):
        """
        Обрабатывает ситуацию появления анимации поиска врагов на карте.

        Args:
            drop (DropImage): Объект фиксации дропа.

        Returns:
            bool: Была ли выполнена обработка.
        """
        if not self.is_in_map():
            return False

        timeout = Timer(self.MAP_ENEMY_SEARCHING_TIMEOUT_SECOND)
        appeared = False
        while 1:
            self.device.screenshot()
            if self.is_event_animation():
                continue
            if self.is_in_map():
                timeout.start()
            else:
                timeout.reset()

            # Этап мог уже завершиться, хотя здесь ожидается анимация поиска противника
            if self.handle_in_stage():
                return True
            # immediately enter submarine combat in W16
            if hasattr(self, 'is_combat_loading') and self.is_combat_loading():
                logger.warning('[Обработчик — поиск] При входе на карту появился экран загрузки боя')
                break
            if self.handle_auto_search_exit(drop=drop):
                timeout.limit = 10
                timeout.reset()
                continue

            # Обработка всплывающих окон
            if self.handle_vote_popup():
                timeout.limit = 10
                timeout.reset()
                continue
            if self.handle_story_skip():
                self.ensure_no_story()
                timeout.limit = 10
                timeout.reset()
            if self.handle_guild_popup_cancel():
                timeout.limit = 10
                timeout.reset()
                continue
            if self.handle_urgent_commission(drop=drop):
                timeout.limit = 10
                timeout.reset()
                continue

            # Условие завершения
            if self.enemy_searching_appear():
                appeared = True
            else:
                if appeared:
                    self.handle_enemy_flashing()
                    self.device.sleep(0.3)
                    self.device.screenshot()
                    logger.info('[Обработчик — поиск] Появилась анимация поиска противника')
                    break
                self.enemy_searching_color_initial()
            if timeout.reached():
                logger.info('[Обработчик — поиск] Истекло время ожидания анимации поиска противника')
                break

        return True

    def handle_in_map_no_enemy_searching(self, drop=None):
        """
        Обрабатывает ситуацию, когда анимация поиска врагов на карте не появилась.

        Args:
            drop (DropImage): Объект фиксации дропа.

        Returns:
            bool: Была ли выполнена обработка.
        """
        if not self.is_in_map():
            return False

        timeout = Timer(1, count=2).start()
        while 1:
            self.device.screenshot()

            if not self.is_in_map():
                timeout.reset()

            # Этап мог уже завершиться, хотя здесь ожидается анимация поиска противника
            if self.handle_in_stage():
                return True
            if self.handle_auto_search_exit(drop=drop):
                timeout.reset()
                continue

            # Обработка всплывающих окон
            if self.handle_vote_popup():
                timeout.reset()
                continue
            if self.handle_story_skip():
                self.ensure_no_story()
                timeout.reset()
            if self.handle_guild_popup_cancel():
                timeout.reset()
                continue
            if self.handle_urgent_commission(drop=drop):
                timeout.reset()
                continue

            # Условие завершения
            if timeout.reached():
                logger.info('[Обработчик — поиск] На карте не появилась анимация поиска противника')
                break

        return True
