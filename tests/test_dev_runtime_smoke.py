from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from module.dev_runtime import smoke
from module.dev_runtime.contracts import DevEnvironment, DevResult
from module.dev_runtime.evidence import EvidenceScreenshot, GitSnapshot

_NOW = datetime(2026, 8, 30, 9, 0, 1, tzinfo=UTC)
_STARTED_AT = "2026-08-30T09:00:00+00:00"
_PNG = b"synthetic-png"


def _environment(tmp_path: Path) -> DevEnvironment:
    (tmp_path / "module" / "config" / "argument").mkdir(parents=True)
    (tmp_path / "module" / "config" / "argument" / "args.json").write_text(
        json.dumps(
            {
                "Reward": {
                    "Reward": {"Enable": {"type": "checkbox", "value": False}},
                    "Unsafe": {"path": {"type": "input", "value": "x"}},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ap.json").write_text(
        json.dumps(
            {
                "Reward": {
                    "Reward": {"Enable": False},
                    "Scheduler": {
                        "Enable": False,
                        "Command": "Reward",
                        "NextRun": "2020-01-01 00:00:00",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return DevEnvironment(tmp_path, Path("python"))


def _source() -> GitSnapshot:
    return GitSnapshot("a" * 40, "main", False, False, (), True, None)


class _Backend(smoke.SmokeSupervisorBackend):
    def launch(self, environment: DevEnvironment, smoke_id: str) -> smoke.SmokeSupervisorIdentity:
        return smoke.SmokeSupervisorIdentity(
            pid=1000,
            created_at=1.0,
            executable="python",
            command_line=["python", "-m", "module.dev_runtime.smoke_supervisor", "--smoke-id", smoke_id],
            cwd=str(environment.repository_root),
        )

    @staticmethod
    def matches(environment: DevEnvironment, smoke_id: str, identity: smoke.SmokeSupervisorIdentity) -> bool:
        return True


class _Runtime:
    def __init__(self, *, error: bool = False, visual: bool = False) -> None:
        self.active = False
        self.error = error
        self.visual = visual
        self.stop_calls = 0
        self.screenshot = EvidenceScreenshot(
            DevResult(
                True,
                "DEV_SCREENSHOT_READY",
                "Снимок готов",
                "running",
                "session-1",
                {
                    "screenshot": {
                        "screenshot_id": "shot-1",
                        "timestamp": _STARTED_AT,
                        "mime": "image/png",
                        "width": 1,
                        "height": 1,
                        "byte_size": len(_PNG),
                        "sha256": hashlib.sha256(_PNG).hexdigest(),
                    }
                },
            ),
            _PNG,
            "image/png",
        )

    def plan(self, **_: object) -> DevResult:
        return DevResult(True, "DEV_TASK_PLAN_READY", "План готов", "no_session")

    def start(self, **_: object) -> DevResult:
        self.active = True
        return DevResult(True, "DEV_SESSION_STARTED", "Сессия запущена", "running", "session-1")

    def status(self) -> DevResult:
        return DevResult(
            True,
            "DEV_STATUS",
            "Статус",
            "running_owned" if self.active else "stopped",
            "session-1" if self.active else None,
            {"task_lifecycle": {"phase": "running" if self.active else "clean"}},
        )

    def get_evidence(self, **_: object) -> DevResult:
        return DevResult(
            True,
            "DEV_EVIDENCE_READY",
            "Подтверждающие данные",
            "running" if self.active else "stopped",
            "session-1",
            {
                "evidence_health": {"status": "complete", "reasons": []},
                "lifecycle": {"started_at": _STARTED_AT},
                "current_task": "Reward" if self.active else None,
                "screenshots": {"count": 0, "latest": None},
            },
        )

    def _events(self) -> list[dict[str, object]]:
        events: list[dict[str, object]] = [
            {
                "sequence": 1,
                "timestamp": _STARTED_AT,
                "type": "session_ready",
                "fields": {},
            },
            {
                "sequence": 2,
                "timestamp": _STARTED_AT,
                "type": "task_started",
                "fields": {"task": "Reward"},
            },
        ]
        if self.error:
            events.append(
                {
                    "sequence": 3,
                    "timestamp": _STARTED_AT,
                    "type": "runtime_error",
                    "fields": {"exception_type": "ProductError", "code": "PRODUCT_ERROR"},
                }
            )
        return events

    def get_timeline(self, **_: object) -> DevResult:
        return DevResult(
            True,
            "DEV_TIMELINE_READY",
            "Хронология",
            "running" if self.active else "stopped",
            "session-1",
            {"events": self._events(), "more": False, "next_after_sequence": 3},
        )

    def get_logs(self, **_: object) -> DevResult:
        return DevResult(
            True,
            "DEV_LOGS_READY",
            "Журнал",
            "running" if self.active else "stopped",
            "session-1",
            {
                "items": [{"text": "Тест Smoke Harness", "truncated": False}],
                "next_cursor": None,
                "more": False,
                "truncated": False,
                "health": {"status": "complete", "reasons": []},
            },
        )

    def stop(self, **_: object) -> DevResult:
        self.stop_calls += 1
        self.active = False
        return DevResult(True, "DEV_SESSION_STOPPED", "Сессия остановлена", "stopped", "session-1", {"cleanup_confirmed": True})

    def cleanup(self) -> DevResult:
        return DevResult(True, "DEV_CLEANUP_CONFIRMED", "Очистка подтверждена", "stopped", "session-1")

    def get_screenshot(self) -> EvidenceScreenshot:
        return self.screenshot

    def get_historical_screenshot(self, **_: object) -> EvidenceScreenshot:
        return self.screenshot

    def port_probe(self, *_: object) -> bool:
        return False


@pytest.fixture
def clean_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "capture_git_snapshot", lambda _: _source())


def _manager(tmp_path: Path, runtime: _Runtime) -> smoke.SmokeRunManager:
    return smoke.SmokeRunManager(
        _environment(tmp_path),
        runtime_factory=lambda: runtime,
        supervisor_backend=_Backend(),
        now=lambda: _NOW,
    )


def _spec(**kwargs: object) -> smoke.SmokeSpec:
    values: dict[str, object] = {
        "name": "stage5-test",
        "objective": "Проверить Smoke Harness",
        "timeout_seconds": 30.0,
        "session": smoke.SmokeSessionSpec(root_tasks=["Reward"]),
    }
    values.update(kwargs)
    return smoke.SmokeSpec(**values)


def test_smoke_spec_is_strict_canonical_and_rejects_forbidden_paths() -> None:
    spec = _spec(
        assertions=[
            smoke.EventOccurredAssertion(
                assertion_id="ready",
                capability_id="event_occurred",
                event_type="session_ready",
            )
        ]
    )
    assert spec.canonical_json() == spec.canonical_json()
    assert len(spec.spec_hash()) == 64
    reordered = _spec(
        assertions=[
            smoke.EventOccurredAssertion(
                assertion_id="ready",
                capability_id="event_occurred",
                event_type="session_ready",
            )
        ],
        setup=smoke.SmokeSetupSpec(config_overrides=[]),
    )
    assert reordered.spec_hash() == spec.spec_hash()
    with pytest.raises(ValueError):
        smoke.SmokeSpec.model_validate({**spec.canonical_dict(), "unexpected": True}, strict=True)
    with pytest.raises(ValueError):
        smoke.SmokeConfigOverride(path="Reward.Scheduler.Enable", value=True)
    with pytest.raises(ValueError):
        smoke.SmokeConfigOverride(path="Reward.Unsafe.path", value="x")


def test_capability_registry_evaluates_negative_assertion_only_after_window() -> None:
    registry = smoke.SmokeCapabilityRegistry()
    assertion = smoke.EventNotOccurredAssertion(
        assertion_id="no-error",
        capability_id="event_not_occurred",
        event_type="runtime_error",
        observation_window_seconds=2.0,
    )
    context = smoke.SmokeObservationContext(
        timeline=(),
        logs=(),
        evidence_health="complete",
        runtime_state="running",
        task_policy_state="active",
        current_task=None,
        config_values={},
        restored_paths=frozenset(),
        port_listening=True,
        elapsed_seconds=1.0,
        completed=False,
        session_id="session-1",
        structured_errors=(),
        screenshot_metadata=(),
        log_available=True,
        log_truncated=False,
    )
    assert registry.evaluate(assertion, context).status is smoke.SmokeAssertionStatus.PENDING
    completed = replace(context, elapsed_seconds=2.0, completed=True)
    assert registry.evaluate(assertion, completed).status is smoke.SmokeAssertionStatus.PASS


def test_smoke_run_passes_and_restores_declared_override(tmp_path: Path, clean_source: None) -> None:
    runtime = _Runtime()
    manager = _manager(tmp_path, runtime)
    spec = _spec(
        setup=smoke.SmokeSetupSpec(
            config_overrides=[smoke.SmokeConfigOverride(path="Reward.Reward.Enable", value=True)]
        ),
        assertions=[
            smoke.EventOccurredAssertion(
                assertion_id="ready",
                capability_id="event_occurred",
                event_type="session_ready",
            )
        ],
    )
    started = manager.start_smoke(spec)
    assert started.ok is True
    smoke_id = started.details["smoke_id"]
    manager._run_supervisor(smoke_id)
    result = manager.store.load_result(smoke_id)
    assert result is not None
    assert result.outcome is smoke.SmokeOutcome.PASS
    assert runtime.stop_calls == 1
    profile = json.loads((tmp_path / "config" / "ap.json").read_text(encoding="utf-8"))
    assert profile["Reward"]["Reward"]["Enable"] is False
    assert result.cleanup.confirmed is True


def test_runtime_error_cannot_be_silently_ignored(tmp_path: Path, clean_source: None) -> None:
    manager = _manager(tmp_path, _Runtime(error=True))
    started = manager.start_smoke(
        _spec(
            assertions=[
                smoke.EventOccurredAssertion(
                    assertion_id="ready",
                    capability_id="event_occurred",
                    event_type="session_ready",
                )
            ]
        )
    )
    manager._run_supervisor(started.details["smoke_id"])
    result = manager.store.load_result(started.details["smoke_id"])
    assert result is not None
    assert result.outcome is smoke.SmokeOutcome.PRODUCT_FAILED
    assert result.primary_failure is not None
    assert result.primary_failure.code == "DEV_SMOKE_UNEXPECTED_RUNTIME_ERROR"


def test_visual_evaluation_is_pending_only_after_cleanup_and_submit_is_immutable(tmp_path: Path, clean_source: None) -> None:
    manager = _manager(tmp_path, _Runtime(visual=True))
    started = manager.start_smoke(
        _spec(
            visual_assertions=[
                smoke.SmokeVisualAssertion(
                    assertion_id="visual",
                    capability_id="external_visual",
                    rubric="Проверить целевой экран по замороженной рубрике",
                    capture_condition=smoke.VisualCaptureCondition(kind="event", event_type="session_ready"),
                )
            ]
        )
    )
    smoke_id = started.details["smoke_id"]
    manager._run_supervisor(smoke_id)
    pending = manager.store.load(smoke_id)
    assert pending.state is smoke.SmokeState.AWAITING_EXTERNAL_EVALUATION
    assert pending.cleanup.confirmed is True
    evaluation = manager.get_smoke_evaluation(smoke_id)
    assert evaluation.image == _PNG
    submitted = manager.submit_smoke_evaluation(smoke_id, "visual", "pass", "Экран соответствует замороженной рубрике")
    assert submitted.ok is True
    final = manager.store.load_result(smoke_id)
    assert final is not None
    assert final.outcome is smoke.SmokeOutcome.PASS
    duplicate = manager.submit_smoke_evaluation(smoke_id, "visual", "pass", "Повтор")
    assert duplicate.ok is False


def test_smoke_state_store_persists_and_rejects_immutable_changes(tmp_path: Path, clean_source: None) -> None:
    environment = _environment(tmp_path)
    source = smoke._source_snapshot(_source())
    spec = _spec()
    now = _NOW.isoformat()
    first = smoke.SmokeStateStore(environment)
    first.create(spec, source, created_at=now, deadline_at=now, smoke_id="persisted")
    second = smoke.SmokeStateStore(environment)
    assert second.load_spec("persisted").spec_hash() == spec.spec_hash()
    second.update("persisted", {"state": smoke.SmokeState.PREPARING})
    with pytest.raises(smoke.SmokeStoreError) as error:
        second.update("persisted", {"spec_hash": "c" * 64})
    assert error.value.code == "DEV_SMOKE_STATE_IMMUTABLE"


def test_source_drift_invalidates_run_before_runtime_start(tmp_path: Path, clean_source: None, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _Runtime()
    manager = _manager(tmp_path, runtime)
    started = manager.start_smoke(_spec())
    assert started.ok is True

    monkeypatch.setattr(smoke, "capture_git_snapshot", lambda _: replace(_source(), head="b" * 40))
    smoke_id = started.details["smoke_id"]
    manager._run_supervisor(smoke_id)

    result = manager.store.load_result(smoke_id)
    assert result is not None
    assert result.outcome is smoke.SmokeOutcome.INVALIDATED
    assert result.code == "INVALIDATED_SOURCE_DRIFT"
    assert result.cleanup.attempted is False
    assert runtime.stop_calls == 0


def test_cancel_request_finishes_with_confirmed_cleanup(tmp_path: Path, clean_source: None) -> None:
    runtime = _Runtime()
    manager = _manager(tmp_path, runtime)
    started = manager.start_smoke(_spec())
    smoke_id = started.details["smoke_id"]
    manager.store.request_cancel(smoke_id, _STARTED_AT)

    manager._run_supervisor(smoke_id)

    result = manager.store.load_result(smoke_id)
    assert result is not None
    assert result.outcome is smoke.SmokeOutcome.CANCELLED
    assert result.cleanup.confirmed is True
    assert result.cleanup.port_free is True
    assert runtime.stop_calls == 1
    assert manager.cancel_smoke(smoke_id).code == "DEV_SMOKE_ALREADY_FINISHED"


def test_cancel_smoke_returns_request_for_live_supervisor(tmp_path: Path, clean_source: None) -> None:
    manager = _manager(tmp_path, _Runtime())
    started = manager.start_smoke(_spec())

    response = manager.cancel_smoke(started.details["smoke_id"])

    assert response.ok is True
    assert response.code == "DEV_SMOKE_CANCEL_REQUESTED"


def test_timeout_finishes_with_timeout_outcome_and_cleanup(tmp_path: Path, clean_source: None) -> None:
    runtime = _Runtime()
    environment = _environment(tmp_path)
    clock = [_NOW]
    manager = smoke.SmokeRunManager(
        environment,
        runtime_factory=lambda: runtime,
        supervisor_backend=_Backend(),
        now=lambda: clock[0],
    )
    started = manager.start_smoke(_spec(timeout_seconds=1.0))
    clock[0] = _NOW + timedelta(seconds=2)

    manager._run_supervisor(started.details["smoke_id"])

    result = manager.store.load_result(started.details["smoke_id"])
    assert result is not None
    assert result.outcome is smoke.SmokeOutcome.TIMEOUT
    assert result.primary_failure is not None
    assert result.primary_failure.code == "DEV_SMOKE_TIMEOUT"
    assert result.cleanup.confirmed is True


def test_active_run_conflict_is_fail_closed(tmp_path: Path, clean_source: None) -> None:
    environment = _environment(tmp_path)
    first_runtime = _Runtime()
    second_runtime = _Runtime()
    first = smoke.SmokeRunManager(environment, runtime_factory=lambda: first_runtime, supervisor_backend=_Backend(), now=lambda: _NOW)
    second = smoke.SmokeRunManager(environment, runtime_factory=lambda: second_runtime, supervisor_backend=_Backend(), now=lambda: _NOW)

    started = first.start_smoke(_spec())
    conflict = second.start_smoke(_spec())

    assert started.ok is True
    assert conflict.ok is False
    assert conflict.code == "DEV_SMOKE_ACTIVE_CONFLICT"


def test_config_registry_rejects_wrong_type_and_unknown_path(tmp_path: Path, clean_source: None) -> None:
    manager = _manager(tmp_path, _Runtime())
    wrong_type = manager.validate_smoke(
        _spec(setup=smoke.SmokeSetupSpec(config_overrides=[smoke.SmokeConfigOverride(path="Reward.Reward.Enable", value="да")]))
    )
    unknown_path = manager.validate_smoke(
        _spec(setup=smoke.SmokeSetupSpec(config_overrides=[smoke.SmokeConfigOverride(path="Reward.Reward.Missing", value=True)]))
    )

    assert wrong_type.ok is False
    assert wrong_type.details["issues"][0]["code"] == "DEV_SMOKE_CONFIG_VALUE_INVALID"
    assert unknown_path.ok is False
    assert unknown_path.details["issues"][0]["code"] == "DEV_SMOKE_CONFIG_PATH_UNSUPPORTED"


def test_capability_registry_rejects_duplicate_registration() -> None:
    registry = smoke.SmokeCapabilityRegistry()
    descriptor = smoke.SmokeCapabilityDescriptor(
        capability_id="future_capability",
        kind="assertion",
        config_schema=smoke.SmokeCapabilitySchema(fields=[]),
        evidence_source="runtime_state",
        deterministic=True,
        external=False,
        available=True,
        description="Проверка будущей capability",
    )
    evaluator = lambda _assertion, _context: smoke.CapabilityEvaluation(
        smoke.SmokeAssertionStatus.PASS,
        "runtime_state",
        "Проверка пройдена",
        (),
    )
    registry.register(descriptor, evaluator)
    with pytest.raises(smoke.SmokeStoreError) as error:
        registry.register(descriptor, evaluator)
    assert error.value.code == "DEV_SMOKE_CAPABILITY_CONFLICT"
    assert "future_capability" in {item.capability_id for item in registry.descriptors()}
