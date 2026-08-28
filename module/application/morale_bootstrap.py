"""Target-driven bootstrap per-ship morale перед запуском campaign map."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Callable

from module.application.fleet_mapping import working_fleet_bindings
from module.application.morale import (
    MoraleKnowledge,
    MoraleLocation,
    MoraleSlotState,
)
from module.application.morale_reconciliation import TargetedMoraleLookupTarget
from module.dock_inventory.model import IdentityStatus
from module.dorm.morale_composition import build_campaign_morale_context
from module.dorm.morale_lookup import (
    TargetedMoraleLocationHint,
    TargetedMoraleLookupController,
    TargetedMoraleLookupError,
)
from module.dorm.morale_model import (
    DormFloorScanStatus,
    DormMoraleObservation,
    DormMoraleScanResult,
)
from module.formation.model import FleetSelection
from module.logger import logger
from module.ui.page import page_main


class CampaignMoraleBootstrapError(RuntimeError):
    """Детерминированный пробел evidence, который должен остановить только задачу."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        target: TargetedMoraleLookupTarget | None = None,
    ) -> None:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            raise ValueError("code morale bootstrap имеет неверный формат")
        super().__init__(message)
        self.code = code
        self.target = target


@dataclass(frozen=True, slots=True)
class CampaignMoraleBootstrapSummary:
    task: str
    physical_fleets: tuple[int, ...]
    target_count: int
    dorm_exact: int
    targeted_outside: int
    unmatched_unrelated: int
    unresolved_raw: int
    ambiguous_targets: int
    final_exact: int


@dataclass(frozen=True, slots=True)
class _DormTargetFilter:
    scan: DormMoraleScanResult
    exact_matches: int
    unmatched_unrelated: int
    unresolved_raw: int
    ambiguous_targets: int


def _target_key(slot: MoraleSlotState) -> tuple[int, object, int]:
    return slot.fleet_index, slot.side, slot.position


def _target_label(target: TargetedMoraleLookupTarget) -> str:
    return (
        f"{target.canonical_name} "
        f"(Fleet {target.fleet_index}, {target.side.value}:{target.position})"
    )


def _compatible_targets(
    observation: DormMoraleObservation,
    targets: tuple[MoraleSlotState, ...],
) -> tuple[MoraleSlotState, ...]:
    if (
        observation.identity_status is not IdentityStatus.MATCHED
        or observation.canonical_identity is None
    ):
        return ()
    return tuple(
        target
        for target in targets
        if (
            target.canonical_identity == observation.canonical_identity
            and (
                observation.ship_form is None
                or target.ship_form is observation.ship_form
            )
        )
    )


def _filter_scan_for_targets(
    scan: DormMoraleScanResult,
    targets: tuple[MoraleSlotState, ...],
) -> _DormTargetFilter:
    """Оставить только Dorm evidence, которое однозначно относится к target set.

    Campaign bootstrap не является глобальным Dorm inventory scan. Посторонние и
    неразрешённые карточки учитываются в диагностике, а missing targets добираются
    безопасным Search lookup.
    """

    unresolved_raw = 0
    unmatched_unrelated = 0
    compatible_by_card: dict[
        tuple[object, int], tuple[MoraleSlotState, ...]
    ] = {}

    for observation in scan.observations:
        card_key = (observation.floor, observation.ordinal)
        if (
            observation.identity_status is not IdentityStatus.MATCHED
            or observation.canonical_identity is None
        ):
            unresolved_raw += 1
            compatible_by_card[card_key] = ()
            continue
        compatible = _compatible_targets(observation, targets)
        compatible_by_card[card_key] = compatible
        if not compatible:
            unmatched_unrelated += 1

    demand = Counter(
        _target_key(compatible[0])
        for compatible in compatible_by_card.values()
        if len(compatible) == 1
    )
    accepted: set[tuple[object, int]] = set()
    ambiguous_target_keys: set[tuple[int, object, int]] = set()
    for card_key, compatible in compatible_by_card.items():
        if len(compatible) == 1 and demand[_target_key(compatible[0])] == 1:
            accepted.add(card_key)
            continue
        if compatible:
            ambiguous_target_keys.update(_target_key(item) for item in compatible)

    attempts = []
    for attempt in scan.attempts:
        if (
            attempt.status is DormFloorScanStatus.SUCCEEDED
            and attempt.snapshot is not None
        ):
            kept = tuple(
                item
                for item in attempt.snapshot.observations
                if (item.floor, item.ordinal) in accepted
            )
            attempts.append(
                replace(
                    attempt,
                    snapshot=replace(attempt.snapshot, observations=kept),
                )
            )
        else:
            attempts.append(attempt)

    return _DormTargetFilter(
        scan=replace(scan, attempts=tuple(attempts)),
        exact_matches=len(accepted),
        unmatched_unrelated=unmatched_unrelated,
        unresolved_raw=unresolved_raw,
        ambiguous_targets=len(ambiguous_target_keys),
    )


class CampaignMoraleBootstrapper:
    """Довести campaign target set от Dorm evidence до exact bootstrap."""

    def __init__(
        self,
        config,
        device,
        dorm_controller,
        *,
        context_factory: Callable = build_campaign_morale_context,
        lookup_factory: Callable = TargetedMoraleLookupController,
    ) -> None:
        self.config = config
        self.device = device
        self.dorm_controller = dorm_controller
        self._context_factory = context_factory
        self._lookup_factory = lookup_factory

    def _task(self) -> str:
        task = getattr(getattr(self.config, "task", None), "command", None)
        if not isinstance(task, str) or not task.strip():
            raise CampaignMoraleBootstrapError(
                "task_unknown",
                "Нельзя доказать campaign task для morale bootstrap.",
            )
        return task

    @staticmethod
    def _occupied_targets(state) -> tuple[MoraleSlotState, ...]:
        return tuple(
            slot
            for fleet in state.fleets
            for slot in fleet.slots
            if slot.occupied
        )

    @staticmethod
    def _matched_targets(
        targets: tuple[MoraleSlotState, ...],
    ) -> tuple[MoraleSlotState, ...]:
        return tuple(
            slot
            for slot in targets
            if (
                slot.identity_status is IdentityStatus.MATCHED
                and slot.canonical_identity is not None
                and slot.canonical_name is not None
                and slot.ship_form is not None
            )
        )

    @staticmethod
    def _target_from_slot(slot: MoraleSlotState) -> TargetedMoraleLookupTarget:
        if (
            slot.canonical_identity is None
            or slot.canonical_name is None
            or slot.ship_form is None
        ):
            raise CampaignMoraleBootstrapError(
                "formation_identity_unresolved",
                f"Fleet {slot.fleet_index} {slot.side.value}:{slot.position} "
                "не имеет доказанной canonical identity.",
            )
        return TargetedMoraleLookupTarget(
            fleet_index=slot.fleet_index,
            side=slot.side,
            position=slot.position,
            canonical_identity=slot.canonical_identity,
            canonical_name=slot.canonical_name,
            ship_form=slot.ship_form,
        )

    def _return_to_main(self, lookup=None) -> None:
        """Вернуться в Main через доказанный безопасный UI path."""

        if lookup is not None:
            lookup.exit_to_main()
            return
        try:
            self.dorm_controller.close_train()
        except Exception as error:  # только fallback к штатному page graph.
            from module.dorm.morale_controller import DormMoraleControllerError

            if not isinstance(error, DormMoraleControllerError):
                raise
        self.dorm_controller.ui_ensure(page_main)

    def _arm_task_level_failure(self, error: CampaignMoraleBootstrapError) -> None:
        """Отложить только campaign task и подавить внешний Restart request."""

        self.config.task_delay(success=False)
        original_task_call = self.config.task_call

        def guarded_task_call(task, force_call=True):
            if task == "Restart":
                logger.warning(
                    "[Настроение] Restart не назначен: bootstrap завершился "
                    f"детерминированным evidence failure `{error.code}`."
                )
                return False
            return original_task_call(task, force_call=force_call)

        # Guard живёт только на текущем cached config object. После обработки
        # исключения scheduler перечитает конфигурацию, поэтому global policy не меняется.
        self.config.task_call = guarded_task_call

    def _fail_safely(
        self,
        error: CampaignMoraleBootstrapError,
        *,
        lookup=None,
    ) -> None:
        target = _target_label(error.target) if error.target is not None else "нет"
        logger.error_context(
            title="Morale bootstrap не получил полное exact evidence",
            reason=f"stage={error.code}; target={target}; {error}",
            impact="Campaign map не будет запущена; текущая задача будет отложена.",
            action=(
                "Проверьте строки Formation/Dorm/Targeted lookup выше. "
                "Перезапуск Azur Lane для такого evidence failure не требуется."
            ),
            level=40,
        )
        self._return_to_main(lookup)
        self._arm_task_level_failure(error)
        raise error

    def run(
        self,
        scan: DormMoraleScanResult,
    ) -> tuple[DormMoraleScanResult, CampaignMoraleBootstrapSummary]:
        if not isinstance(scan, DormMoraleScanResult):
            raise TypeError("scan должен быть DormMoraleScanResult")

        task = self._task()
        bindings = working_fleet_bindings(self.config, task=task)
        physical_fleets = tuple(item.physical_fleet_index for item in bindings)
        selection = FleetSelection(physical_fleets)
        context = self._context_factory(require_ready=False)
        state_before = context.morale_service.state(
            self.config.config_name,
            selection,
        )
        occupied = self._occupied_targets(state_before)
        matched = self._matched_targets(occupied)
        roles = ", ".join(
            f"{item.role}=logical{item.logical_fleet_index}->Fleet{item.physical_fleet_index}"
            for item in bindings
        )
        target_labels = ", ".join(
            _target_label(self._target_from_slot(item))
            for item in matched
        ) or "нет"
        formation_complete = all(
            fleet.formation_observation_id is not None for fleet in state_before.fleets
        )
        logger.info(
            f"[Настроение] Bootstrap: task={task}; roles={roles}; "
            f"physical_fleets={physical_fleets}; formation_complete={formation_complete}; "
            f"targets={len(occupied)}; identities={target_labels}"
        )

        if not formation_complete:
            self._fail_safely(
                CampaignMoraleBootstrapError(
                    "formation_missing",
                    "Для одного из рабочих физических флотов отсутствует Fleet State.",
                )
            )
        if not occupied:
            self._fail_safely(
                CampaignMoraleBootstrapError(
                    "formation_empty",
                    "В рабочих физических флотах не найдено occupied targets.",
                )
            )
        if len(matched) != len(occupied):
            unresolved = next(item for item in occupied if item not in matched)
            self._fail_safely(
                CampaignMoraleBootstrapError(
                    "formation_identity_unresolved",
                    f"Fleet {unresolved.fleet_index} {unresolved.side.value}:"
                    f"{unresolved.position} не имеет MATCHED identity.",
                )
            )

        filtered = _filter_scan_for_targets(scan, matched)
        floor_status = ", ".join(
            f"{attempt.floor.value}={attempt.status.value}"
            + (f"/{attempt.error_code}" if attempt.error_code else "")
            for attempt in scan.attempts
        )
        logger.info(
            "[Настроение] Dorm: "
            f"{floor_status}; exact_target_matches={filtered.exact_matches}; "
            f"unmatched_unrelated={filtered.unmatched_unrelated}; "
            f"unresolved_raw={filtered.unresolved_raw}; "
            f"ambiguous_targets={filtered.ambiguous_targets}"
        )
        if not scan.complete:
            self._fail_safely(
                CampaignMoraleBootstrapError(
                    "dorm_scan_incomplete",
                    "Dorm 1F/2F scan не дал полного recovery context.",
                )
            )

        reconciliation = context.reconciliation_service.reconcile(
            self.config.config_name,
            selection,
            filtered.scan,
        )
        if reconciliation.stale_fleet_indices:
            self._fail_safely(
                CampaignMoraleBootstrapError(
                    "formation_stale",
                    "Formation continuity изменилась после начала Dorm scan: "
                    f"{reconciliation.stale_fleet_indices}.",
                )
            )

        lookup = None
        targeted_outside = 0
        if reconciliation.lookup_targets:
            try:
                # Для открытия candidate-selection нужен именно raw Train occupant.
                # Отфильтрованный scan намеренно не содержит unrelated Dorm cards.
                self.dorm_controller.open_candidate_selection(scan)
                lookup = self._lookup_factory(self.config, device=self.device)
                for target in reconciliation.lookup_targets:
                    try:
                        observed = lookup.lookup(target)
                    except TargetedMoraleLookupError as exc:
                        logger.warning(
                            "[Настроение] Targeted lookup: "
                            f"ship={target.canonical_name}; query={target.search_query}; "
                            f"identity=not_proven; fleet_badge=unknown; morale=unknown; "
                            f"location=unknown; error={exc.error_code}"
                        )
                        raise CampaignMoraleBootstrapError(
                            f"lookup_{exc.error_code}"[:64],
                            str(exc),
                            target=target,
                        ) from exc
                    logger.info(
                        "[Настроение] Targeted lookup: "
                        f"ship={target.canonical_name}; query={target.search_query}; "
                        f"identity=matched; fleet_badge={observed.fleet_badge}; "
                        f"morale={observed.morale}; location={observed.location_hint.value}; "
                        "error=none"
                    )
                    if observed.location_hint is not TargetedMoraleLocationHint.OUTSIDE_DORM:
                        raise CampaignMoraleBootstrapError(
                            "lookup_dorm_location_requires_recovery",
                            "Search доказал Train/Rest location, но missing target не имеет "
                            "однозначного Dorm recovery evidence; outside semantics запрещены.",
                            target=target,
                        )
                    try:
                        context.reconciliation_service.record_targeted_outside(
                            self.config.config_name,
                            target,
                            dorm_scan_id=reconciliation.dorm_scan_id,
                            morale=observed.morale,
                            observed_at=observed.observed_at,
                        )
                    except (TypeError, ValueError) as exc:
                        raise CampaignMoraleBootstrapError(
                            "lookup_continuity_failed",
                            str(exc),
                            target=target,
                        ) from exc
                    targeted_outside += 1
            except CampaignMoraleBootstrapError as error:
                self._fail_safely(error, lookup=lookup)
            except Exception as exc:
                from module.dorm.morale_controller import DormMoraleControllerError

                if isinstance(exc, DormMoraleControllerError):
                    self._fail_safely(
                        CampaignMoraleBootstrapError(
                            "selection_open_failed",
                            str(exc),
                        ),
                        lookup=lookup,
                    )
                raise

        final_state = context.morale_service.state(
            self.config.config_name,
            selection,
        )
        final_targets = self._occupied_targets(final_state)
        final_exact = tuple(
            slot
            for slot in final_targets
            if (
                slot.identity_status is IdentityStatus.MATCHED
                and slot.knowledge is MoraleKnowledge.EXACT
                and slot.current is not None
                and slot.recovery is not None
                and slot.location is not MoraleLocation.UNKNOWN
                and slot.dorm_scan_id == filtered.scan.id
            )
        )
        projected = tuple(
            slot
            for slot in final_targets
            if slot.knowledge is MoraleKnowledge.PROJECTED
        )
        unknown = tuple(
            slot
            for slot in final_targets
            if slot not in final_exact and slot not in projected
        )
        logger.info(
            "[Настроение] Final bootstrap: "
            f"exact={len(final_exact)}; projected={len(projected)}; "
            f"unknown={len(unknown)}; targets={len(final_targets)}"
        )
        if len(final_exact) != len(final_targets) or len(final_targets) != len(occupied):
            self._fail_safely(
                CampaignMoraleBootstrapError(
                    "final_exact_incomplete",
                    "Не каждый occupied target получил exact current и recovery context "
                    "с provenance текущего bootstrap scan.",
                ),
                lookup=lookup,
            )

        self._return_to_main(lookup)
        summary = CampaignMoraleBootstrapSummary(
            task=task,
            physical_fleets=physical_fleets,
            target_count=len(occupied),
            dorm_exact=filtered.exact_matches,
            targeted_outside=targeted_outside,
            unmatched_unrelated=filtered.unmatched_unrelated,
            unresolved_raw=filtered.unresolved_raw,
            ambiguous_targets=filtered.ambiguous_targets,
            final_exact=len(final_exact),
        )
        logger.info(
            "[Настроение] Bootstrap PASS: "
            f"final_exact={summary.final_exact}/{summary.target_count}; "
            "returned_main=True; dorm_roster_changed=False"
        )
        return filtered.scan, summary


__all__ = (
    "CampaignMoraleBootstrapError",
    "CampaignMoraleBootstrapSummary",
    "CampaignMoraleBootstrapper",
)
