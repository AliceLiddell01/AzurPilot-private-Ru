"""Политика и application-интеграция per-ship morale."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from time import sleep

from module.application.fleet_mapping import physical_fleet_index
from module.application.morale import (
    MORALE_MAX,
    MoraleEventKind,
    MoraleKnowledge,
    MoraleService,
    RecordMoraleEvent,
    morale_ready_at,
)
from module.base.decorator import cached_property
from module.base.utils import random_normal_distribution_int
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
_MORALE_EXECUTION_STORAGE_KEY = "MoraleCombatExecution"


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
        self.morale_service = morale_service
        self.map_is_2x_book = False
        self.total_reduced = 0
        self._active_event_key: str | None = None
        self._active_execution_storage: tuple[str, str, int] | None = None

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
        del ceiling
        effective_reduce = expected_reduce
        # Это отдельная legacy gameplay policy: при большом ожидаемом списании
        # keep-exp учитывает не более 29 пунктов. Она не разрешает снижать
        # пользовательский target до recovery ceiling.
        if policy.control == "keep_exp_bonus" and expected_reduce >= 29:
            effective_reduce = 29
        return Decimal(policy.limit + effective_reduce)

    def _check_reduce(self, battle):
        counts = self._order_counts(battle, self.config.Fleet_FleetOrder)
        costs = tuple(
            count * self.reduce_per_battle_before_entering for count in counts
        )
        logical_indices = tuple(index for index, cost in enumerate(costs, 1) if cost)
        service = self._service()
        now = service.now()
        if not logical_indices:
            return now, False
        physical = tuple(self._physical_fleet(index) for index in logical_indices)
        state = service.state(
            self._instance(),
            FleetSelection.several(*physical),
            at=now,
        )
        self._log_state(state, label="before_map")
        fleet_by_physical = {
            fleet_state.fleet_index: fleet_state for fleet_state in state.fleets
        }
        ready: list = []
        blocked = False
        for logical, cost, physical_index in zip(
            logical_indices,
            (costs[index - 1] for index in logical_indices),
            physical,
            strict=True,
        ):
            fleet_state = fleet_by_physical.get(physical_index)
            if fleet_state is None or fleet_state.formation_observation_id is None:
                logger.warning(
                    f"[Настроение — проверка] Fleet State physical Fleet {physical_index} отсутствует; вход заблокирован"
                )
                blocked = True
                continue
            policy = self._policy(logical)
            for slot in fleet_state.slots:
                if not slot.occupied:
                    continue
                if slot.identity_status is not IdentityStatus.MATCHED:
                    logger.warning(
                        f"[Настроение — проверка] Identity занятого слота физического Fleet {fleet_state.fleet_index} не доказана; вход заблокирован"
                    )
                    blocked = True
                    continue
                if slot.knowledge is MoraleKnowledge.UNKNOWN:
                    logger.warning(
                        f"[Настроение — проверка] Мораль слота физического Fleet {fleet_state.fleet_index} неизвестна; вход заблокирован"
                    )
                    blocked = True
                    continue
                if slot.recovery is None:
                    logger.warning(
                        f"[Настроение — проверка] Recovery слота физического Fleet {fleet_state.fleet_index} не доказан; вход заблокирован"
                    )
                    blocked = True
                    continue
                target = self._target(policy, cost, slot.recovery.recovery_ceiling)
                if target > slot.recovery.recovery_ceiling or target > MORALE_MAX:
                    logger.warning(
                        f"[Настроение — проверка] Target {target} недостижим для слота физического Fleet {fleet_state.fleet_index}; вход заблокирован"
                    )
                    blocked = True
                    continue
                recovered = morale_ready_at(slot, target=target, at=now)
                if recovered is not None:
                    ready.append(recovered)
                else:
                    logger.warning(
                        f"[Настроение — проверка] ETA слота физического Fleet {fleet_state.fleet_index} не доказан; вход заблокирован"
                    )
                    blocked = True
        if blocked:
            return None, True
        recovered = max(ready, default=now)
        return recovered, recovered > now

    @staticmethod
    def _log_state(state, *, label: str) -> None:
        for fleet_state in state.fleets:
            for slot in fleet_state.slots:
                if not slot.occupied:
                    continue
                current = "unknown" if slot.current is None else str(slot.current)
                recovery = (
                    "unknown"
                    if slot.recovery is None
                    else str(slot.recovery.recovery_per_hour)
                )
                logger.info(
                    f"[Настроение — снимок] {label}: physical Fleet "
                    f"{fleet_state.fleet_index}, {slot.side.value}:{slot.position}, "
                    f"morale={current}, recovery={recovery}/hour, "
                    f"location={slot.location.value}, knowledge={slot.knowledge.value}"
                )

    def log_working_fleets(self, label: str) -> None:
        """Записать projection только для физических флотов текущей задачи."""

        from module.application.fleet_mapping import working_fleet_bindings

        physical = tuple(
            binding.physical_fleet_index
            for binding in working_fleet_bindings(self.config)
        )
        service = self._service()
        now = service.now()
        self._log_state(
            service.state(
                self._instance(),
                FleetSelection(physical),
                at=now,
            ),
            label=label,
        )

    def check_reduce(self, battle):
        """Перед входом в campaign проверить известные per-ship projections."""

        if not self.is_calculate:
            return
        recovered, delay = self._check_reduce(battle)
        if recovered is None:
            raise ScriptEnd(
                "[Настроение — задержка] Недостаточно доказательств для безопасного входа в бой"
            )
        if delay:
            logger.info(
                "[Настроение — задержка] Текущая задача отложена до доказанного recovery"
            )
            # Scheduler хранит local-naive datetime, а morale domain — timezone-aware.
            scheduler_target = recovered.astimezone().replace(tzinfo=None)
            self.config.task_delay(target=scheduler_target)
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
        if fleet_state.formation_observation_id is None:
            raise ScriptEnd(
                f"[Настроение — ожидание] Fleet State physical Fleet {physical} отсутствует"
            )
        policy = self._policy(fleet_index)
        ready: list = []
        blocked = False
        for slot in fleet_state.slots:
            if not slot.occupied:
                continue
            if slot.identity_status is not IdentityStatus.MATCHED:
                logger.warning(
                    f"[Настроение — ожидание] Identity занятого слота physical Fleet {physical} не доказана; ожидание заблокировано"
                )
                blocked = True
                continue
            if slot.knowledge is MoraleKnowledge.UNKNOWN:
                logger.warning(
                    f"[Настроение — ожидание] Мораль physical Fleet {physical} неизвестна; ожидание заблокировано"
                )
                blocked = True
                continue
            if slot.recovery is None:
                blocked = True
                continue
            target = self._target(
                policy,
                self.reduce_per_battle,
                slot.recovery.recovery_ceiling,
            )
            if target > slot.recovery.recovery_ceiling or target > MORALE_MAX:
                logger.warning(
                    f"[Настроение — ожидание] Target {target} недостижим для physical Fleet {physical}; ожидание заблокировано"
                )
                blocked = True
                continue
            recovered = morale_ready_at(slot, target=target, at=now)
            if recovered is not None:
                ready.append(recovered)
            else:
                blocked = True
        if blocked:
            raise ScriptEnd(
                f"[Настроение — ожидание] Нет доказательства безопасного morale для physical Fleet {physical}"
            )
        recovered = max(ready, default=now)
        while service.now() < recovered:
            logger.attr("Ожидание до", recovered)
            sleep(60)

    def _execution_storage(self) -> dict[str, object] | None:
        """Вернуть скрытый persisted task Storage для generation morale event."""

        storage = getattr(self.config, "Storage_Storage", None)
        if storage is None:
            # Упрощённые unit-test config doubles исторически не имеют Storage.
            return None
        if not isinstance(storage, dict):
            raise RequestHumanTakeover(
                "Persisted Storage morale event имеет некорректный формат."
            )
        return storage

    def _durable_execution_sequence(
        self,
        execution_id: str,
        run_token: str,
    ) -> int | None:
        storage = self._execution_storage()
        if storage is None:
            self._active_execution_storage = None
            return None

        raw_state = storage.get(_MORALE_EXECUTION_STORAGE_KEY)
        if raw_state is None:
            state: dict[str, object] = {}
        elif isinstance(raw_state, dict):
            state = raw_state
        else:
            raise RequestHumanTakeover(
                "Persisted coordinate morale event имеет некорректный формат."
            )

        sequence = state.get("sequence", 0)
        if type(sequence) is not int or sequence < 0:
            raise RequestHumanTakeover(
                "Persisted sequence morale event имеет некорректное значение."
            )
        applied = state.get("applied", False)
        if type(applied) is not bool:
            raise RequestHumanTakeover(
                "Persisted applied marker morale event имеет некорректное значение."
            )

        run_digest = hashlib.sha256(run_token.encode("utf-8")).hexdigest()[:16]
        caller_digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:16]
        retry = (
            sequence > 0
            and state.get("run") == run_digest
            and state.get("caller") == caller_digest
            and not applied
        )
        current = sequence if retry else sequence + 1
        persisted = {
            "run": run_digest,
            "caller": caller_digest,
            "sequence": current,
            "applied": False,
        }
        updated = dict(storage)
        updated[_MORALE_EXECUTION_STORAGE_KEY] = persisted
        self.config.Storage_Storage = updated
        self._active_execution_storage = (run_digest, caller_digest, current)
        return current

    def _mark_execution_applied(self) -> None:
        marker = self._active_execution_storage
        if marker is None:
            return
        storage = self._execution_storage()
        if storage is None:
            return
        raw_state = storage.get(_MORALE_EXECUTION_STORAGE_KEY)
        if not isinstance(raw_state, dict):
            raise RequestHumanTakeover(
                "Persisted coordinate morale event потерян после применения."
            )
        run_digest, caller_digest, sequence = marker
        if (
            raw_state.get("run") != run_digest
            or raw_state.get("caller") != caller_digest
            or raw_state.get("sequence") != sequence
        ):
            raise RequestHumanTakeover(
                "Persisted coordinate morale event изменён конкурентно."
            )
        if raw_state.get("applied") is True:
            return
        persisted = dict(raw_state)
        persisted["applied"] = True
        updated = dict(storage)
        updated[_MORALE_EXECUTION_STORAGE_KEY] = persisted
        self.config.Storage_Storage = updated

    def begin_event(
        self,
        event_key: str,
        *,
        execution_id: str | None = None,
    ) -> None:
        if not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 80:
            raise ValueError("event_key должен быть непустой строкой длиной до 80 символов")
        if execution_id is None:
            execution_id = event_key
        if (
            not isinstance(execution_id, str)
            or not execution_id.strip()
            or len(execution_id) > 96
        ):
            raise ValueError(
                "execution_id должен быть непустой строкой длиной до 96 символов"
            )
        # Scheduler.NextRun задаёт durable boundary task run. Внутри него hidden
        # Storage хранит generation caller coordinate: retry до фактического
        # apply_event повторяет ключ, а следующий уже применённый бой получает
        # новую generation даже после перезапуска Python-процесса.
        run_token = self._durable_run_token()
        sequence = self._durable_execution_sequence(execution_id, run_token)
        durable_coordinate = execution_id
        if sequence is not None:
            durable_coordinate = f"{execution_id}:generation:{sequence}"
        execution_digest = hashlib.sha256(
            f"{run_token}:{durable_coordinate}".encode("utf-8")
        ).hexdigest()[:16]
        if len(event_key) > 67:
            event_digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:16]
            event_key = f"{event_key[:48]}:{event_digest}"
        self._active_event_key = f"{event_key}:exec:{execution_digest}"

    def _durable_run_token(self) -> str:
        run = getattr(self.config, "Scheduler_NextRun", None)
        if run is None or not str(run).strip():
            raise RequestHumanTakeover(
                "Не удалось определить durable Scheduler.NextRun для morale event."
            )
        task = getattr(getattr(self.config, "task", None), "command", "unknown")
        return f"{task}:{run}"

    def _event_key(
        self,
        logical_fleet_index: int,
        kind: MoraleEventKind,
        *,
        battle: object | None = None,
    ) -> str:
        if self._active_event_key:
            raw = f"{self._active_event_key}:{logical_fleet_index}:{kind.value}"
            if len(raw) <= 96:
                return raw
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
            return f"morale-event:{logical_fleet_index}:{kind.value}:{digest}"
        if battle is None:
            raise RequestHumanTakeover(
                "Для morale event без begin_event не передан battle coordinate."
            )
        campaign = getattr(self.config, "Campaign_Name", "unknown")
        run_digest = hashlib.sha256(
            self._durable_run_token().encode("utf-8")
        ).hexdigest()[:16]
        raw = (
            f"{self._instance()}:{campaign}:{battle}:"
            f"{logical_fleet_index}:{kind.value}:run:{run_digest}"
        )
        if len(raw) <= 96:
            return raw
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"morale-event:{kind.value}:{digest}"

    def record_warning(
        self,
        fleet_index: int,
        *,
        event_key: str | None = None,
        battle: object | None = None,
    ):
        physical = self._physical_fleet(fleet_index)
        key = event_key or self._event_key(
            fleet_index,
            MoraleEventKind.WARNING,
            battle=battle,
        )
        result = self._service().record_warning(
            self._instance(),
            fleet_index=physical,
            event_key=key,
        )
        self._mark_execution_applied()
        return result

    def reduce(
        self,
        fleet_index,
        shipwreck=False,
        *,
        casualty_slot=None,
        battle: object | None = None,
    ):
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
                event_key=self._event_key(fleet_index, kind, battle=battle),
                target_side=(casualty_slot[0] if casualty_slot is not None else None),
                target_position=(
                    casualty_slot[1] if casualty_slot is not None else None
                ),
            ),
        )
        self._mark_execution_applied()
        if result.exact_slots:
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
        self.__dict__.pop("bug_threshold", None)

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
