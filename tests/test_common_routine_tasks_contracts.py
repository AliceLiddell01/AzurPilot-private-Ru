from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from module.commission.commission import RewardCommission
from module.commission.project import Commission
from module.dorm.dorm import RewardDorm
from module.research.selector import ResearchSelector
from module.tactical.tactical_class import Book


class TestResearchResourceContracts:
    @staticmethod
    def selector(**overrides):
        selector = object.__new__(ResearchSelector)
        defaults = {
            "Research_UseCube": "always_use",
            "Research_UseCoin": "always_use",
            "Research_UsePart": "always_use",
            "Research_AllowGenreT": True,
            "SERVER": "en",
        }
        defaults.update(overrides)
        selector.config = SimpleNamespace(**defaults)
        selector.storage_has_boxes = True
        return selector

    @staticmethod
    def project(**overrides):
        defaults = {
            "valid": True,
            "duration": "2",
            "need_cube": False,
            "need_coin": False,
            "need_part": False,
            "genre": "Q",
            "task": "",
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_resource_do_not_use_modes_still_reject_consuming_projects(self):
        selector = self.selector(Research_UseCube="do_not_use")
        assert selector._research_check(self.project(need_cube=True)) is False

        selector = self.selector(Research_UseCoin="do_not_use")
        assert selector._research_check(self.project(need_coin=True)) is False

        selector = self.selector(Research_UsePart="do_not_use")
        assert selector._research_check(self.project(need_part=True)) is False

    def test_genre_b_and_disabled_genre_t_remain_rejected(self):
        selector = self.selector()
        assert selector._research_check(self.project(genre="B")) is False

        selector = self.selector(Research_AllowGenreT=False)
        assert selector._research_check(self.project(genre="T")) is False


class TestDormAndTacticalContracts:
    def test_dorm_delay_math_is_unchanged(self):
        dorm = object.__new__(RewardDorm)
        dorm.config = SimpleNamespace(Scheduler_SuccessInterval=321)

        assert dorm.cal_dorm_delay(0) == 321
        assert dorm.cal_dorm_delay(1) == 1000
        assert dorm.cal_dorm_delay(6) == 278
        assert dorm.cal_dorm_delay(99) == 321

    def test_tactical_book_machine_identity_is_unchanged(self):
        book = object.__new__(Book)
        book.genre_str = "Red"
        book.tier_str = "T3"
        book.exp = True
        assert str(book) == "Red_T3_Exp"

        book.genre_str = "Blue"
        book.tier_str = "T2"
        book.exp = False
        assert str(book) == "Blue_T2"

        assert Book.exp_tier == {0: 0, 1: 100, 2: 300, 3: 800, 4: 1500}


class TestCommissionContracts:
    def test_commission_time_parser_keeps_duration_semantics(self):
        commission = object.__new__(Commission)
        commission.valid = True
        assert commission.parse_time("01:30:00") == timedelta(hours=1, minutes=30)
        assert commission.valid is True

        commission.valid = True
        assert commission.parse_time("invalid") is None
        assert commission.valid is False

    def test_major_commission_setting_still_controls_selection(self):
        handler = object.__new__(RewardCommission)
        commission = SimpleNamespace(valid=True, status="pending", category_str="major")

        handler.config = SimpleNamespace(Commission_DoMajorCommission=False)
        assert handler._commission_check(commission) is False

        handler.config = SimpleNamespace(Commission_DoMajorCommission=True)
        assert handler._commission_check(commission) is True

    def test_t_research_commission_counter_still_decrements_and_triggers_research(self):
        handler = object.__new__(RewardCommission)
        handler.config = SimpleNamespace(
            cross_get=Mock(return_value=2),
            cross_set=Mock(),
            task_call=Mock(),
        )

        handler._handle_research_genre_t_update(1)
        handler.config.cross_set.assert_called_once_with(
            "Research.Research.RemainingCommissions", 1
        )
        handler.config.task_call.assert_not_called()

        handler.config.cross_get.return_value = 2
        handler.config.cross_set.reset_mock()
        handler._handle_research_genre_t_update(2)
        handler.config.cross_set.assert_called_once_with(
            "Research.Research.RemainingCommissions", 0
        )
        handler.config.task_call.assert_called_once_with("Research")
