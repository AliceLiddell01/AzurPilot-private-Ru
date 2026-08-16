from __future__ import annotations

import multiprocessing
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import module.webui.app_event_planner as planner_module
import module.webui.event_shop_priority as priority_module
from module.webui.app_event_planner import EventPlannerMixin
from module.webui.event_source import (
    empty_event_user_state,
    event_user_state_write_lock,
    load_event_user_state,
    mutate_event_user_state,
    save_event_user_state,
)


def _transaction_worker(
    instance: str,
    root: str,
    legacy_root: str,
    row_id: str,
    value: int,
    ready,
    entered,
    release,
) -> None:
    ready.set()

    def mutation(state):
        entered.set()
        if release is not None and not release.wait(15):
            raise TimeoutError("Не получен сигнал продолжения транзакции")
        updated = dict(state)
        selections = dict(state.get("shop_selections", {}))
        selections[row_id] = value
        updated["shop_selections"] = selections
        return updated

    if not mutate_event_user_state(
        instance,
        mutation,
        root=Path(root),
        legacy_root=Path(legacy_root),
    ):
        raise RuntimeError("Транзакция пользовательского состояния не выполнена")


def test_user_state_transaction_serializes_processes_without_lost_update(tmp_path):
    instance = "race-profile"
    root = tmp_path / "state"
    legacy_root = tmp_path / "legacy"
    initial = empty_event_user_state()
    initial["source_event_id"] = "en:test"
    initial["shop_selections"] = {"A": 3, "B": 5}
    save_event_user_state(instance, initial, root=root)

    context = multiprocessing.get_context("spawn")
    first_ready = context.Event()
    first_entered = context.Event()
    second_ready = context.Event()
    second_entered = context.Event()
    release_first = context.Event()

    first = context.Process(
        target=_transaction_worker,
        args=(
            instance,
            str(root),
            str(legacy_root),
            "A",
            0,
            first_ready,
            first_entered,
            release_first,
        ),
    )
    second = context.Process(
        target=_transaction_worker,
        args=(
            instance,
            str(root),
            str(legacy_root),
            "B",
            2,
            second_ready,
            second_entered,
            None,
        ),
    )

    first.start()
    try:
        assert first_ready.wait(15)
        assert first_entered.wait(15)
        second.start()
        assert second_ready.wait(15)
        assert not second_entered.wait(1.0)
        release_first.set()
        assert second_entered.wait(15)
        first.join(20)
        second.join(20)
        assert first.exitcode == 0
        assert second.exitcode == 0
    finally:
        release_first.set()
        for process in (first, second):
            if process.pid is not None and process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(5)

    final = load_event_user_state(instance, root=root, legacy_root=legacy_root)
    assert final["shop_selections"] == {"A": 0, "B": 2}


def test_clear_selected_target_preserves_unrelated_latest_selection(monkeypatch):
    initial = empty_event_user_state()
    initial["source_event_id"] = "en:test"
    initial["shop_selections"] = {"A": 3, "B": 9}
    persisted = {}

    def fake_mutate(instance, mutation):
        assert instance == "profile"
        updated = mutation(deepcopy(initial))
        if updated is None:
            return False
        persisted.update(updated)
        return True

    monkeypatch.setattr(priority_module, "mutate_event_user_state", fake_mutate)
    config = SimpleNamespace(config_name="profile")

    assert priority_module._clear_selected_target(config, "en:test", "A", 3) is True
    assert persisted["shop_selections"] == {"A": 0, "B": 9}


def test_clear_selected_target_rejects_parallel_same_row_change(monkeypatch):
    initial = empty_event_user_state()
    initial["source_event_id"] = "en:test"
    initial["shop_selections"] = {"A": 4, "B": 9}
    saved = []

    def fake_mutate(instance, mutation):
        assert instance == "profile"
        updated = mutation(deepcopy(initial))
        if updated is None:
            return False
        saved.append(updated)
        return True

    monkeypatch.setattr(priority_module, "mutate_event_user_state", fake_mutate)
    config = SimpleNamespace(config_name="profile")

    assert priority_module._clear_selected_target(config, "en:test", "A", 3) is False
    assert saved == []


def test_user_state_public_io_is_reentrant_inside_write_lock(tmp_path):
    instance = "nested-profile"
    root = tmp_path / "state"
    initial = empty_event_user_state()
    initial["shop_selections"] = {"A": 1}
    save_event_user_state(instance, initial, root=root)
    done = Event()
    errors = []

    def nested_io():
        try:
            with event_user_state_write_lock(instance, root):
                state = load_event_user_state(
                    instance, root=root, legacy_root=tmp_path / "legacy"
                )
                state["shop_selections"]["A"] = 2
                save_event_user_state(instance, state, root=root)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    worker = Thread(target=nested_io, daemon=True)
    worker.start()
    assert done.wait(3), "Вложенная блокировка user-state самозаблокировалась"
    assert errors == []
    final = load_event_user_state(instance, root=root, legacy_root=tmp_path / "legacy")
    assert final["shop_selections"] == {"A": 2}


def test_event_plan_mutation_blocks_runtime_until_fresh_write(monkeypatch, tmp_path):
    instance = "planner-race"
    root = tmp_path / "state"
    legacy_root = tmp_path / "legacy"
    initial = empty_event_user_state()
    initial["source_event_id"] = "en:test"
    initial["shop_selections"] = {"A": 3, "B": 5}
    save_event_user_state(instance, initial, root=root)

    plan_read = Event()
    allow_plan_write = Event()
    runtime_entered = Event()
    ui_result = []

    class PlannerProbe(EventPlannerMixin):
        def __init__(self):
            self.alas_name = instance

        def _event_plan(self):
            plan_read.set()
            assert allow_plan_write.wait(3)
            return {
                "event": {"id": "en:test"},
                "shop_items": [
                    {"id": "A", "selected": 3},
                    {"id": "B", "selected": 5},
                ],
            }

    real_mutate = mutate_event_user_state
    monkeypatch.setattr(planner_module, "is_demo_mode", lambda: False)
    monkeypatch.setattr(
        planner_module,
        "event_user_state_write_lock",
        lambda profile: event_user_state_write_lock(profile, root),
    )
    monkeypatch.setattr(
        planner_module,
        "mutate_event_user_state",
        lambda profile, mutation: real_mutate(
            profile, mutation, root=root, legacy_root=legacy_root
        ),
    )

    planner = PlannerProbe()

    def ui_mutation():
        ui_result.append(
            planner._event_plan_mutate(
                lambda plan: plan["shop_items"][0].update(selected=2), ""
            )
        )

    def runtime_mutation():
        def update(state):
            runtime_entered.set()
            updated = dict(state)
            selections = dict(state["shop_selections"])
            selections["B"] = 9
            updated["shop_selections"] = selections
            return updated

        real_mutate(instance, update, root=root, legacy_root=legacy_root)

    ui_thread = Thread(target=ui_mutation)
    runtime_thread = Thread(target=runtime_mutation)
    ui_thread.start()
    assert plan_read.wait(3)
    runtime_thread.start()
    assert not runtime_entered.wait(0.2), (
        "Runtime вошёл между чтением плана и записью WebUI"
    )
    allow_plan_write.set()
    ui_thread.join(3)
    runtime_thread.join(3)
    assert not ui_thread.is_alive()
    assert not runtime_thread.is_alive()
    assert ui_result == [True]

    final = load_event_user_state(instance, root=root, legacy_root=legacy_root)
    assert final["shop_selections"] == {"A": 2, "B": 9}
