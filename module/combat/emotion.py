"""Политика и application-интеграция per-ship morale."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from time import sleep
from uuid import uuid4

from module.application.fleet_mapping import physical_fleet_index
from module.application.morale import (
    MoraleEventKind,
    MoraleKnowledge,
    MoraleService,
    RecordMoraleEvent,
    morale_ready_at,
)
from module.base.decorator import cached_property
from module.base.utils import random_normal_distribution_int
from module.config.time_source import now as current_time
from module.dock_inventory.model import IdentityStatus
from module.exception import RequestHumanTakeover, ScriptEnd, ScriptError
from module.formation.model import FleetSelection
from module.logger import logger


DIC_LIMIT = {
    "keep_exp_bonus": 120,
    "prevent_green_face": 40,
    "prevent_yellow_face": 30,
    "prevent_red_face": 2,
}

# Сохранено как policy metadata для старых импортов; числового состояния в config нет.
DIC_RECOVER = {
    "not_in_dormitory": 20,
    "dormitory_floor_1": 40,
    "dormitory_floor_2": 50,
}
DIC_RECOVER_MAX = {
    "not_in_dormitory": 119,
    "dormitory_floor_1": 150,
    "dormitory_floor_2": 150,
}


class FleetEmotion:
    """Policy view одной logical роли без собственного morale state."""

    def __init__(self, config, fleet):
        self.config = config
        self.fleet = fleet

    @property
    def control(self):
        logical = self.fleet if self.fleet in (1, 2) else 1
        return getattr(self.config, f"Emotion_Fleet{logical}Control")

    @property
    def limit(self):
        try:
            return DIC_LIMIT[self.control]
        except KeyError as exc:
            raise ScriptError(f"Неизвестная policy настроения: {self.control}") from exc


class Emotion:
    """Исполнитель morale policy поверх единого application ledger.

    `FleetEmotion` здесь содержит только policy. Baseline, projection и события
    хранятся исключительно через `MoraleService`, а не в legacy config keys.
    """

    def __init__(self, config, *, morale_service: MoraleService | None = None):
        self.config = config
        self.fleet_1 = FleetEmotion(self.config, fleet=1)
        self.fleet_2 = FleetEmotion(self.config, fleet=2)
        self.fleets = [self.fleet_1, self.fleet_2]
        self.public_fleet = self.fleet_1
        self.using_public = self._handle_public()
        self.morale_service = morale_service
        self.map_is_2x_book = False
        self.total_reduced = 0
        self._active_event_key: str | None = None
        self._event_session_key = uuid4().hex[:16]

    def _handle_public(self) -> bool:
        """Сохранить старую task policy, не создавая общий числовой pool."""

        if not getattr(self.config, "PublicEmotion_Enable", False):
            return False
        tasks = getattr(self.config, "PublicEmotion_Tasks", None)
        if not isinstance(tasks, str) or not tasks.strip():
            return False
        task = getattr(getattr(self.config, "task", None), "command", "")
        return task in {item.strip() for item in tasks.split(",") if item.strip()}

    @property
    def is_calculate(self):
        return "calculate" in getattr(self.config, "Emotion_Mode", "nothing")

    @property
    def is_ignore(self):
        return "ignore" in getattr(self.config, "Emotion_Mode", "nothing")

    def _service(self) -> MoraleService:
        if self.morale_service is None:
            from module.persistence.runtime import build_runtime_morale_service

            self.morale_service = build_runtime_morale_service(require_ready=False)
        return self.morale_service

    def _physical_fleet(self, logical_fleet_index: int) -> int:
        try:
            return physical_fleet_index(self.config, logical_fleet_index)
        except (TypeError, ValueError) as exc:
            raise RequestHumanTakeover(
                f"Не удалось доказать physical Fleet для logical роли {logical_fleet_index}."
            ) from exc

    def _instance(self) -> str:
        instance = getattr(self.config, "config_name", None)
        if not isinstance(instance, str) or not instance.strip():
            raise RequestHumanTakeover("Не удалось определить app instance для morale event.")
        return instance

    def update(self):
        """Совместимый no-op: projection вычисляется application service на read."""

    def record(self):
        """Совместимый no-op: numeric morale больше не записывается в config."""

    def show(self):
        """Совместимый no-op без раскрытия устаревшего локального состояния."""

    @property
    def reduce_per_battle(self):
        return 4 if self.map_is_2x_book else 2

    @property
    def reduce_per_battle_before_entering(self):
        if self.map_is_2x_book or getattr(self.config, "Campaign_Use2xBook", False):
            return 4
        return 2

    @property
    def reduce_shipwreck(self):
        return 10

    @staticmethod
    def _order_counts(battle: int, order: str) -> tuple[int, int]:
        if type(battle) is not int or battle < 1:
            raise ValueError("battle должен быть положительным int")
        if order == "fleet1_mob_fleet2_boss":
            return battle - 1, 1
        if order == "fleet1_boss_fleet2_mob":
            return 1, battle - 1
        if order == "fleet1_all_fleet2_standby":
            return battle, 0
        if order == "fleet1_standby_fleet2_all":
            return 0, battle
        raise ScriptError(f"Неизвестный порядок флотов: {order}")

    def _policy(self, logical_fleet_index: int) -> FleetEmotion:
        return self.fleets[logical_fleet_index - 1]

    @staticmethod
    def _target(policy: FleetEmotion, expected_reduce: int, ceiling: Decimal) -> Decimal:
        target = Decimal(policy.limit + expected_reduce)
        # Для outside-Dorm ceiling=119 старая keep-exp policy ограничивала
        # ожидаемое списание до 29, иначе задача никогда не могла стать ready.
        if policy.control == "keep_exp_bonus" and expected_reduce >= 29:
            target = min(target, ceiling)
        return min(target, ceiling)

    def _check_reduce(self, battle):
        counts = self._order_counts(battle, self.config.Fleet_FleetOrder)
        costs = tuple(
            count * self.reduce_per_battle_before_entering for count in counts
        )
        logical_indices = tuple(index for index, cost in enumerate(costs, 1) if cost)
        if not logical_indices:
            return current_time(), False
        physical = tuple(self._physical_fleet(index) for index in logical_indices)
        service = self._service()
        now = service.now()
        state = service.state(
            self._instance(),
            FleetSelection.several(*physical),
            at=now,
        )
        fleet_by_physical = {
            fleet_state.fleet_index: fleet_state for fleet_state in state.fleets
        }
        ready: list = []
        for logical, cost, physical_index in zip(
            logical_indices,
            (costs[index - 1] for index in logical_indices),
            physical,
            strict=True,
        ):
            fleet_state = fleet_by_physical[physical_index]
            policy = self._policy(logical)
            for slot in fleet_state.slots:
                if (
                    not slot.occupied
                    or slot.identity_status is not IdentityStatus.MATCHED
                ):
                    continue
                if slot.knowledge is MoraleKnowledge.UNKNOWN:
                    logger.warning(
                        f"[Настроение — проверка] Мораль слота физического Fleet {fleet_state.fleet_index} неизвестна; ETA не вычисляется"
                    )
                    continue
                if slot.recovery is None:
                    continue
                target = self._target(policy, cost, slot.recovery.recovery_ceiling)
                recovered = morale_ready_at(slot, target=target, at=now)
                if recovered is not None:
                    ready.append(recovered)
        recovered = max(ready, default=now)
        return recovered, recovered > now

    def check_reduce(self, battle):
        """Перед входом в campaign проверить известные per-ship projections."""

        if not self.is_calculate:
            return
        recovered, delay = self._check_reduce(battle)
        if delay:
            logger.info(
                "[Настроение — задержка] Текущая задача отложена до доказанного recovery"
            )
            self.config.task_delay(target=recovered)
            raise ScriptEnd("[Настроение — задержка] Контроль настроения")

    def wait(self, fleet_index):
        """Дождаться порога для всех известных ships физического флота."""

        if type(fleet_index) is not int or fleet_index not in (1, 2):
            raise ValueError("fleet_index должен быть logical индексом 1 или 2")
        physical = self._physical_fleet(fleet_index)
        service = self._service()
        now = service.now()
        state = service.state(self._instance(), FleetSelection.one(physical), at=now)
        fleet_state = state.fleets[0]
        policy = self._policy(fleet_index)
        ready: list = []
        for slot in fleet_state.slots:
            if (
                not slot.occupied
                or slot.identity_status is not IdentityStatus.MATCHED
            ):
                continue
            if slot.knowledge is MoraleKnowledge.UNKNOWN:
                logger.warning(
                    f"[Настроение — ожидание] Мораль physical Fleet {physical} неизвестна; ожидание пропущено"
                )
                continue
            if slot.recovery is not None:
                target = self._target(
                    policy,
                    self.reduce_per_battle,
                    slot.recovery.recovery_ceiling,
                )
                recovered = morale_ready_at(slot, target=target, at=now)
                if recovered is not None:
                    ready.append(recovered)
        recovered = max(ready, default=now)
        while current_time() < recovered:
            logger.attr("Ожидание до", recovered)
            sleep(60)

    def begin_event(self, event_key: str) -> None:
        if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 80:
            raise ValueError("event_key должен быть непустой строкой длиной до 80 символов")
        # Scheduler.NextRun отличает одинаковый battle index в следующих запусках
        # задачи, а digest не раскрывает raw scheduler data в observation key.
        run_token = (
            f"{getattr(self.config, 'Scheduler_NextRun', 'unknown')}"
            f":{self._event_session_key}"
        )
        run_digest = hashlib.sha256(run_token.encode("utf-8")).hexdigest()[:16]
        if len(event_key) > 67:
            event_digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:16]
            event_key = f"{event_key[:48]}:{event_digest}"
        self._active_event_key = f"{event_key}:run:{run_digest}"

    def _event_key(self, logical_fleet_index: int, kind: MoraleEventKind) -> str:
        if self._active_event_key:
            return f"{self._active_event_key}:{kind.value}"
        campaign = getattr(self.config, "Campaign_Name", "unknown")
        battle = getattr(self, "battle_count", 0)
        run = getattr(self.config, "Scheduler_NextRun", "unknown")
        return (
            f"{self._instance()}:{campaign}:{battle}:{run}:"
            f"{logical_fleet_index}:{kind.value}"
        )[:96]

    def record_warning(self, fleet_index: int, *, event_key: str | None = None):
        physical = self._physical_fleet(fleet_index)
        key = event_key or self._event_key(fleet_index, MoraleEventKind.WARNING)
        return self._service().record_warning(
            self._instance(),
            fleet_index=physical,
            event_key=key,
        )

    def reduce(self, fleet_index, shipwreck=False):
        """Зафиксировать ровно одно battle или shipwreck event."""

        kind = MoraleEventKind.SHIPWRECK if shipwreck else MoraleEventKind.BATTLE
        cost = self.reduce_shipwreck if shipwreck else self.reduce_per_battle
        physical = self._physical_fleet(fleet_index)
        result = self._service().apply_event(
            self._instance(),
            RecordMoraleEvent(
                fleet_index=physical,
                kind=kind,
                cost=Decimal(cost),
                source=f"combat:{kind.value}",
                event_key=self._event_key(fleet_index, kind),
            ),
        )
        if result.applied:
            self.total_reduced += cost
        logger.info(
            f"[Настроение — event] {kind.value}: physical Fleet {physical}, "
            f"слотов применено {result.applied_slots}, пропущено {result.skipped_slots}"
        )
        return result

    @cached_property
    def bug_threshold(self):
        return random_normal_distribution_int(55, 105, n=2)

    def bug_threshold_reset(self):
        del self.__dict__["bug_threshold"]

    def triggered_bug(self):
        logger.attr("Ошибка настроения", f"{self.total_reduced}/{self.bug_threshold}")
        if self.total_reduced >= self.bug_threshold:
            logger.info(
                "[Настроение — ошибка] Клиент Azur Lane неправильно рассчитал настроение; игровой клиент будет перезапущен"
            )
            self.total_reduced = 0
            self.bug_threshold_reset()
            return True
        return False


__all__ = (
    "DIC_LIMIT",
    "DIC_RECOVER",
    "DIC_RECOVER_MAX",
    "Emotion",
    "FleetEmotion",
)
