from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from module.dev_runtime import (
    SCHEDULER_RESET_TIME,
    DevEnvironment,
    EvidenceStore,
    DevSession,
    DevSessionManager,
    DevRuntimeMode,
    DevSessionState,
    DevTaskMode,
    DevTaskPhase,
    DevTarget,
    ProcessBackend,
    ProcessIdentity,
    TaskCatalog,
    TaskPlan,
    TaskPolicyStore,
    TaskSandboxError,
    task_sandbox,
)
from module.dev_runtime import manager as manager_module


@pytest.fixture(autouse=True)
def _reset_policy_environment_cache():
    task_sandbox.reset_policy_environment_cache()
    yield
    task_sandbox.reset_policy_environment_cache()


def _profile() -> dict[str, object]:
    return {
        "Alas": {"Emulator": {}, "General": {}, "RuntimeOnly": "service"},
        "Dashboard": {"title": "не task"},
        "RootTask": {
            "Scheduler": {
                "Enable": False,
                "Command": "RootTask",
                "NextRun": SCHEDULER_RESET_TIME,
            },
            "Gameplay": {"preserve": "root"},
        },
        "DependencyTask": {
            "Scheduler": {
                "Enable": True,
                "Command": "DependencyTask",
                "NextRun": "2026-08-29 00:00:00",
            },
            "Gameplay": {"preserve": "dependency"},
        },
        "ExcludedTask": {
            "Scheduler": {
                "Enable": True,
                "Command": "ExcludedTask",
                "NextRun": "2026-08-29 00:00:00",
            },
            "Gameplay": {"preserve": "excluded"},
        },
        "UnrelatedTask": {
            "Scheduler": {
                "Enable": True,
                "Command": "UnrelatedTask",
                "NextRun": "2026-08-29 00:00:00",
            },
            "Gameplay": {"preserve": "unrelated"},
        },
    }


def _environment(tmp_path: Path) -> DevEnvironment:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    (root / "config" / "ap.json").write_text(
        json.dumps(_profile(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget("ap"),
    )


def _write_session(
    environment: DevEnvironment,
    session_id: str = "sandbox-session",
    state: DevSessionState = DevSessionState.RUNNING,
) -> None:
    session = DevSession(
        session_id=session_id,
        state=state,
        repository_root=str(environment.repository_root),
        created_at="2026-08-29T00:00:00+00:00",
        updated_at="2026-08-29T00:00:00+00:00",
    )
    environment.state_file.parent.mkdir(parents=True, exist_ok=True)
    environment.state_file.write_text(
        json.dumps(session.as_dict(), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def test_policy_environment_cache_reloads_replaced_current_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_a = _environment(tmp_path)
    environment_b = DevEnvironment(
        repository_root=environment_a.repository_root,
        python_executable=environment_a.python_executable,
        dev_target=DevTarget("ap"),
    )
    monkeypatch.delenv(task_sandbox.TASK_POLICY_ROOT_ENV, raising=False)
    monkeypatch.setattr(task_sandbox.DevEnvironment, "current", lambda: environment_a)

    first, first_error = task_sandbox._current_policy_environment()

    monkeypatch.setattr(task_sandbox.DevEnvironment, "current", lambda: environment_b)
    second, second_error = task_sandbox._current_policy_environment()

    assert first is environment_a
    assert first_error == ""
    assert second is environment_b
    assert second_error == ""


def test_policy_environment_marker_follows_configured_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    state_dir = config_dir / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "dev-runtime-target.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(task_sandbox.TASK_POLICY_ROOT_ENV, str(tmp_path))

    marker = task_sandbox._policy_environment_marker()

    assert marker == (
        task_sandbox._policy_path_marker(config_dir),
        task_sandbox._policy_path_marker(state_dir),
        task_sandbox._policy_path_marker(state_dir / "dev-runtime-target.json"),
    )


class _Backend:
    def __init__(self) -> None:
        self.alive = False
        self.identity: ProcessIdentity | None = None
        self.launch_count = 0
        self.fail_launch = False
        self.fail_stop = False

    def launch(self, environment: DevEnvironment, session_id: str) -> int:
        if self.fail_launch:
            raise OSError("synthetic launch failure")
        self.launch_count += 1
        self.alive = True
        self.identity = ProcessIdentity(
            pid=42000 + self.launch_count,
            created_at=1000.0 + self.launch_count,
            executable=str(environment.python_executable),
            command_line=tuple(ProcessBackend.expected_command(environment, session_id)),
            cwd=str(environment.repository_root),
        )
        return self.identity.pid

    def capture(self, pid: int) -> ProcessIdentity | None:
        return self.identity if self.alive and self.identity and self.identity.pid == pid else None

    def matches(self, identity: ProcessIdentity) -> bool | None:
        if not self.alive:
            return None
        return identity == self.identity

    def find_by_session(
        self, _environment: DevEnvironment, _session_id: str
    ) -> tuple[ProcessIdentity, ...]:
        return ()

    def is_descendant(self, _child_pid: int, _parent: ProcessIdentity) -> bool:
        return self.alive

    def listens_on(self, _pid: int, _host: str, _port: int) -> bool:
        return self.alive

    def request_stop(self, identity: ProcessIdentity) -> bool:
        if self.fail_stop:
            return False
        if self.matches(identity) is not True:
            return False
        self.alive = False
        return True

    def wait_exit(self, _identity: ProcessIdentity, _timeout: float) -> bool:
        return not self.alive

    def force_stop(self, identity: ProcessIdentity) -> bool:
        return self.request_stop(identity)


def _manager(environment: DevEnvironment, backend: _Backend) -> DevSessionManager:
    manager = DevSessionManager(
        environment,
        process_backend=backend,
        shared_webui=False,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
        readiness_probe=lambda _environment, _identity: (True, "ready"),
        now=lambda: datetime(2026, 8, 29, tzinfo=UTC),
        session_id_factory=lambda: "sandbox-session",
        ready_timeout=0.01,
        stop_timeout=0.01,
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    manager._webui_registry_check = lambda: (True, "registry ready")
    return manager


def test_catalog_is_dynamic_and_excludes_non_task_sections(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    catalog = TaskCatalog.from_path(environment.profile_file, profile_name=environment.profile_name)

    assert catalog.commands == (
        "DependencyTask",
        "ExcludedTask",
        "RootTask",
        "UnrelatedTask",
    )
    assert all(item.section == item.command for item in catalog.tasks)
    assert "Dashboard" not in catalog.commands

    expanded = _profile()
    expanded["FutureTask"] = {
        "Scheduler": {
            "Enable": False,
            "Command": "FutureTask",
            "NextRun": SCHEDULER_RESET_TIME,
        },
        "Gameplay": {"preserve": "future"},
    }
    assert "FutureTask" in TaskCatalog.from_payload(expanded).commands


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda payload: payload["RootTask"]["Scheduler"].update({"Command": "Other"}), "DEV_TASK_COMMAND_CONFLICT"),
        (lambda payload: payload["RootTask"].update({"Scheduler": "broken"}), "DEV_TASK_SCHEDULER_MALFORMED"),
        (lambda payload: payload["RootTask"]["Scheduler"].update({"Command": ""}), "DEV_TASK_COMMAND_EMPTY"),
    ],
)
def test_catalog_rejects_ambiguous_scheduler_contract(
    tmp_path: Path, change, code: str
) -> None:
    payload = _profile()
    change(payload)

    with pytest.raises(TaskSandboxError) as error:
        TaskCatalog.from_payload(payload)

    assert error.value.code == code


def test_plan_is_read_only_and_rejects_unknown_or_conflicting_selectors(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())
    before = environment.profile_file.read_bytes()

    plan = manager.plan(
        root_tasks=["RootTask", "RootTask"],
        excluded_tasks=["ExcludedTask"],
    )
    assert plan.ok is True
    assert plan.details["plan"]["root_tasks"] == ["RootTask"]
    assert environment.profile_file.read_bytes() == before

    conflict = manager.plan(root_tasks=["RootTask"], excluded_tasks=["RootTask"])
    unknown = manager.plan(root_tasks=["MissingTask"])
    unsafe = manager.plan(root_tasks=["../RootTask"])
    assert conflict.code == "DEV_TASK_ROOT_EXCLUDED_CONFLICT"
    assert unknown.code == "DEV_TASK_UNKNOWN_ROOT"
    assert unsafe.code == "DEV_TASK_SELECTOR_UNSAFE"

    multiple = manager.plan(
        root_tasks=["UnrelatedTask", "RootTask"],
        excluded_tasks=["ExcludedTask"],
    )
    assert multiple.ok is True
    assert multiple.details["plan"]["root_tasks"] == ["RootTask", "UnrelatedTask"]


def test_policy_provenance_supports_transitive_and_excluded_dependencies(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    _write_session(environment)
    catalog = TaskCatalog.from_path(environment.profile_file, profile_name=environment.profile_name)
    plan = TaskPlan.from_catalog(
        catalog,
        root_tasks=["RootTask"],
        excluded_tasks=["ExcludedTask"],
    )
    store = TaskPolicyStore(environment)
    store.create(plan, session_id="sandbox-session", timestamp="2026-08-29T00:00:00+00:00")

    first = store.register_dependency(
        session_id="sandbox-session",
        caller="RootTask",
        target="DependencyTask",
        timestamp="2026-08-29T00:00:01+00:00",
    )
    second = store.register_dependency(
        session_id="sandbox-session",
        caller="DependencyTask",
        target="ExcludedTask",
        timestamp="2026-08-29T00:00:02+00:00",
    )
    duplicate = store.register_dependency(
        session_id="sandbox-session",
        caller="RootTask",
        target="DependencyTask",
        timestamp="2026-08-29T00:00:03+00:00",
    )
    unrelated = store.register_dependency(
        session_id="sandbox-session",
        caller="UnrelatedTask",
        target="ExcludedTask",
        timestamp="2026-08-29T00:00:04+00:00",
    )

    assert first.allowed and first.reason == "dependency"
    assert second.allowed and second.reason == "dependency_override"
    assert duplicate.allowed and duplicate.new_dependency is False
    assert unrelated.allowed is False
    policy = store.read()
    assert policy is not None
    assert policy.allowed_tasks == ("RootTask", "DependencyTask", "ExcludedTask")
    assert [item.reason for item in policy.dependencies] == [
        "dependency",
        "dependency_override",
    ]

    cycle = store.register_dependency(
        session_id="sandbox-session",
        caller="ExcludedTask",
        target="RootTask",
        timestamp="2026-08-29T00:00:05+00:00",
    )
    assert cycle.allowed is True
    assert cycle.new_dependency is False
    assert len(store.read().dependencies) == 2


def test_task_aware_start_and_default_stop_reset_only_scheduler_fields(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    original = copy.deepcopy(json.loads(environment.profile_file.read_text(encoding="utf-8")))
    backend = _Backend()
    manager = _manager(environment, backend)

    started = manager.start(root_tasks=["RootTask"], excluded_tasks=["ExcludedTask"])
    running = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    assert started.ok is True
    assert running["RootTask"]["Scheduler"]["Enable"] is True
    expected_local = datetime.fromtimestamp(
        datetime(2026, 8, 29, tzinfo=UTC).timestamp()
    ).replace(microsecond=0)
    assert running["RootTask"]["Scheduler"]["NextRun"] == expected_local.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert running["UnrelatedTask"]["Scheduler"]["Enable"] is False
    assert running["ExcludedTask"]["Scheduler"]["Enable"] is False
    assert running["RootTask"]["Gameplay"] == original["RootTask"]["Gameplay"]
    assert manager.environment.task_policy_file.exists()

    stopped = manager.stop()
    cleaned = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    assert stopped.ok is True
    assert stopped.details["cleanup_confirmed"] is True
    assert not environment.task_policy_file.exists()
    for task in ("RootTask", "DependencyTask", "ExcludedTask", "UnrelatedTask"):
        assert cleaned[task]["Scheduler"] == {
            "Enable": False,
            "Command": task,
            "NextRun": SCHEDULER_RESET_TIME,
        }
        assert cleaned[task]["Gameplay"] == original[task]["Gameplay"]


def test_task_aware_lifecycle_marker_is_clean_after_default_stop(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())

    assert manager.start(root_tasks=["RootTask"]).ok is True
    prepared = DevSession.from_dict(
        json.loads(environment.state_file.read_text(encoding="utf-8"))
    )
    assert prepared.task_mode is DevTaskMode.TASK_AWARE
    assert prepared.task_phase is DevTaskPhase.RUNNING
    assert prepared.task_cleanup_required is True
    assert prepared.task_policy_expected is True

    stopped = manager.stop()
    cleaned = DevSession.from_dict(
        json.loads(environment.state_file.read_text(encoding="utf-8"))
    )
    assert stopped.ok is True
    assert cleaned.task_phase is DevTaskPhase.CLEAN
    assert cleaned.task_cleanup_required is False
    assert cleaned.task_policy_expected is False


def test_missing_policy_before_stop_still_cleans_scheduler_state(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())
    assert manager.start(root_tasks=["RootTask"]).ok is True
    environment.task_policy_file.unlink()

    stopped = manager.stop()
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))

    assert stopped.ok is True
    assert stopped.details["cleanup_confirmed"] is True
    assert stopped.code == "DEV_SESSION_STOPPED"
    assert not environment.task_policy_file.exists()
    assert all(
        item["Scheduler"]["Enable"] is False
        and item["Scheduler"]["NextRun"] == SCHEDULER_RESET_TIME
        for item in payload.values()
        if isinstance(item, dict) and "Scheduler" in item
    )


def test_missing_policy_before_recovery_still_cleans_scheduler_state(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    backend = _Backend()
    manager = _manager(environment, backend)
    assert manager.start(root_tasks=["RootTask"]).ok is True
    environment.task_policy_file.unlink()
    backend.alive = False

    recovered = manager.recover()
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))

    assert recovered.ok is True
    assert recovered.details["cleanup_confirmed"] is True
    assert recovered.code == "DEV_STALE_RECOVERED"
    assert not environment.task_policy_file.exists()
    assert all(
        item["Scheduler"]["Enable"] is False
        and item["Scheduler"]["NextRun"] == SCHEDULER_RESET_TIME
        for item in payload.values()
        if isinstance(item, dict) and "Scheduler" in item
    )


def test_new_start_cleans_stopped_task_marker_without_policy(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    backend = _Backend()
    manager = _manager(environment, backend)
    assert manager.start(root_tasks=["RootTask"]).ok is True
    backend.alive = False
    session = DevSession.from_dict(
        json.loads(environment.state_file.read_text(encoding="utf-8"))
    )
    session.state = DevSessionState.STOPPED
    session.process = None
    manager._write_session(session)
    environment.task_policy_file.unlink()

    next_manager = _manager(environment, _Backend())
    started = next_manager.start(root_tasks=["DependencyTask"])

    assert started.ok is True
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    assert payload["RootTask"]["Scheduler"]["Enable"] is False
    assert payload["DependencyTask"]["Scheduler"]["Enable"] is True
    assert next_manager.stop().ok is True


def test_interrupted_preparation_recovers_from_durable_task_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())

    def interrupt_policy_creation(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        task_sandbox.TaskPolicyStore,
        "create",
        interrupt_policy_creation,
    )
    with pytest.raises(KeyboardInterrupt):
        manager.start(root_tasks=["RootTask"])

    interrupted = DevSession.from_dict(
        json.loads(environment.state_file.read_text(encoding="utf-8"))
    )
    assert interrupted.task_mode is DevTaskMode.TASK_AWARE
    assert interrupted.task_phase is DevTaskPhase.PREPARED
    assert interrupted.task_cleanup_required is True
    assert interrupted.task_policy_expected is True

    recovered = _manager(environment, _Backend()).recover()
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))

    assert recovered.ok is True
    assert recovered.code == "DEV_STALE_RECOVERED"
    assert recovered.code != "DEV_TASK_CLEANUP_NOT_NEEDED"
    assert recovered.details["cleanup_confirmed"] is True
    assert all(
        item["Scheduler"]["Enable"] is False
        and item["Scheduler"]["NextRun"] == SCHEDULER_RESET_TIME
        for item in payload.values()
        if isinstance(item, dict) and "Scheduler" in item
    )


def test_running_task_aware_session_with_missing_policy_is_not_healthy(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())
    assert manager.start(root_tasks=["RootTask"]).ok is True
    environment.task_policy_file.unlink()

    status = manager.status()

    assert status.ok is False
    assert status.code == "DEV_TASK_POLICY_MISSING"
    assert status.state == "running_owned"


def test_stage1_session_keeps_legacy_task_lifecycle_defaults(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())

    started = manager.start()
    session = DevSession.from_dict(
        json.loads(environment.state_file.read_text(encoding="utf-8"))
    )

    assert started.ok is True
    assert session.task_mode is DevTaskMode.NONE
    assert session.task_phase is DevTaskPhase.NONE
    assert session.task_cleanup_required is False
    assert session.task_policy_expected is False
    assert manager.stop().ok is True


def test_legacy_stage1_marker_without_task_fields_remains_compatible(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    legacy = DevSession(
        session_id="legacy-session",
        state=DevSessionState.STOPPED,
        repository_root=str(environment.repository_root),
        created_at="2026-08-29T00:00:00+00:00",
        updated_at="2026-08-29T00:00:00+00:00",
    ).as_dict()
    for field in (
        "runtime_mode",
        "task_mode",
        "task_phase",
        "task_cleanup_required",
        "task_policy_expected",
    ):
        legacy.pop(field)

    restored = DevSession.from_dict(legacy)

    assert restored.task_mode is DevTaskMode.NONE
    assert restored.task_phase is DevTaskPhase.NONE
    assert restored.task_cleanup_required is False
    assert restored.task_policy_expected is False
    assert restored.runtime_mode is DevRuntimeMode.STANDALONE_PROCESS


def test_preflight_blocks_corrupt_task_policy_without_task_session(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())
    environment.task_policy_file.parent.mkdir(parents=True, exist_ok=True)
    environment.task_policy_file.write_text("{broken", encoding="utf-8")

    result = manager.preflight()

    assert result.ok is False
    assert "DEV_TASK_STATE_CORRUPT" in result.details["blockers"]


def test_doctor_blocks_corrupt_task_policy_for_stopped_session(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())
    _write_session(environment, state=DevSessionState.STOPPED)
    environment.task_policy_file.parent.mkdir(parents=True, exist_ok=True)
    environment.task_policy_file.write_text("{broken", encoding="utf-8")

    result = manager.doctor()

    assert result.ok is False
    assert result.details["status"]["code"] == "DEV_TASK_STATE_CORRUPT"
    assert "DEV_TASK_STATE_CORRUPT" in result.details["preflight"]["details"]["blockers"]


def test_absent_policy_without_task_state_remains_normal(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())

    preflight = manager.preflight()
    doctor = manager.doctor()

    assert preflight.ok is True
    assert doctor.ok is True


def test_task_aware_launch_failure_cleans_scheduler_state(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    backend = _Backend()
    backend.fail_launch = True
    manager = _manager(environment, backend)

    result = manager.start(root_tasks=["RootTask"])
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))

    assert result.ok is False
    assert result.code == "DEV_LAUNCH_FAILED"
    assert not environment.task_policy_file.exists()
    assert all(
        item["Scheduler"]["Enable"] is False
        and item["Scheduler"]["NextRun"] == SCHEDULER_RESET_TIME
        for item in payload.values()
        if isinstance(item, dict) and "Scheduler" in item
    )


def test_preserve_is_explicit_and_cleanup_remains_recoverable(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())
    assert manager.start(root_tasks=["RootTask"]).ok

    preserved = manager.stop(preserve_task_state=True)
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    assert preserved.ok is True
    assert preserved.code == "DEV_SESSION_STOPPED_PRESERVED"
    assert preserved.details["cleanup_confirmed"] is False
    assert payload["RootTask"]["Scheduler"]["Enable"] is True
    status = manager.status()
    assert status.ok is False
    assert status.code == "DEV_TASK_STATE_PRESERVED"

    cleanup = manager.cleanup()
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    assert cleanup.ok is True
    assert payload["RootTask"]["Scheduler"]["Enable"] is False
    assert payload["RootTask"]["Scheduler"]["NextRun"] == SCHEDULER_RESET_TIME

    repeated = manager.cleanup()
    assert repeated.ok is True
    assert repeated.details["cleanup_confirmed"] is True


def test_cleanup_does_not_reset_policy_when_marker_has_no_process_but_worker_is_found(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    backend = _Backend()
    backend.launch(environment, "sandbox-session")
    manager = _manager(environment, backend)
    session = DevSession(
        session_id="sandbox-session",
        state=DevSessionState.STARTING,
        repository_root=str(environment.repository_root),
        created_at="2026-08-29T00:00:00+00:00",
        updated_at="2026-08-29T00:00:00+00:00",
    )
    environment.state_file.parent.mkdir(parents=True, exist_ok=True)
    environment.state_file.write_text(
        json.dumps(session.as_dict(), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    catalog = TaskCatalog.from_path(environment.profile_file)
    plan = TaskPlan.from_catalog(catalog, ["RootTask"], [], profile_name=environment.profile_name)
    TaskPolicyStore(environment).create(
        plan,
        session_id="sandbox-session",
        timestamp="2026-08-29T00:00:00+00:00",
    )
    backend.find_by_session = lambda _environment, _session_id: (backend.identity,)

    result = manager.cleanup()

    assert result.ok is False
    assert result.code == "DEV_SESSION_ACTIVE"
    assert TaskPolicyStore(environment).read() is not None
    assert json.loads(environment.profile_file.read_text(encoding="utf-8"))["RootTask"]["Scheduler"]["Enable"] is False


def test_task_aware_readiness_failure_and_stale_recovery_cleanup(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    backend = _Backend()
    manager = _manager(environment, backend)
    manager.readiness_probe = lambda _environment, _identity: (False, "synthetic not ready")

    failed = manager.start(root_tasks=["RootTask"])
    assert failed.ok is False
    assert failed.code == "DEV_READINESS_FAILED"
    assert failed.details["task_cleanup"]["details"]["cleanup_confirmed"] is True
    assert not environment.task_policy_file.exists()
    assert failed.session_id == "sandbox-session"
    store = EvidenceStore.for_session(environment, failed.session_id)
    evidence = store.summary()
    assert evidence["cleanup"]["status"] == "complete"
    assert evidence["lifecycle"]["duration_seconds"] == 0
    timeline = store.timeline_page(limit=100)
    assert "session_stopped" in [event["type"] for event in timeline["events"]]

    backend = _Backend()
    manager = _manager(environment, backend)
    assert manager.start(root_tasks=["RootTask"]).ok
    backend.alive = False
    recovered = manager.recover()
    assert recovered.ok is True
    assert recovered.details["cleanup_confirmed"] is True
    assert not environment.task_policy_file.exists()
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    assert all(
        item["Scheduler"]["Enable"] is False
        and item["Scheduler"]["NextRun"] == SCHEDULER_RESET_TIME
        for item in payload.values()
        if isinstance(item, dict) and "Scheduler" in item
    )


def test_readiness_failure_with_unconfirmed_stop_marks_policy_pending(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    backend = _Backend()
    backend.fail_stop = True
    manager = _manager(environment, backend)
    manager.readiness_probe = lambda _environment, _identity: (False, "synthetic not ready")

    failed = manager.start(root_tasks=["RootTask"])

    assert failed.ok is False
    assert failed.code == "DEV_CLEANUP_FAILED"
    assert failed.details["task_cleanup"]["details"]["cleanup_confirmed"] is False
    pending = TaskPolicyStore(environment).read()
    assert pending is not None
    assert pending.state == "cleanup_pending"
    persisted = manager._read_session()
    assert persisted is not None
    assert persisted.last_code == "DEV_CLEANUP_FAILED"
    evidence = EvidenceStore.for_session(environment, failed.session_id).summary()
    assert evidence["lifecycle"]["stopped_at"] is None
    assert evidence["cleanup"]["status"] == "pending"


def test_cleanup_failure_is_not_reported_as_clean_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())
    assert manager.start(root_tasks=["RootTask"]).ok
    monkeypatch.setattr(
        manager_module,
        "write_profile_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic write failure")),
    )

    stopped = manager.stop()
    assert stopped.ok is False
    assert stopped.code == "DEV_CLEANUP_FAILED"
    assert stopped.state == "failed"
    assert stopped.details["cleanup_confirmed"] is False
    pending = TaskPolicyStore(environment).read()
    assert pending is not None and pending.state == "cleanup_pending"


def test_leftover_policy_is_cleaned_before_next_task_aware_start(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _Backend())
    assert manager.start(root_tasks=["RootTask"]).ok
    preserved = manager.stop(preserve_task_state=True)
    assert preserved.ok is True
    assert json.loads(environment.profile_file.read_text(encoding="utf-8"))["RootTask"]["Scheduler"]["Enable"]

    next_manager = _manager(environment, _Backend())
    started = next_manager.start(root_tasks=["DependencyTask"])
    assert started.ok is True
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    assert payload["RootTask"]["Scheduler"]["Enable"] is False
    assert payload["DependencyTask"]["Scheduler"]["Enable"] is True
    assert next_manager.stop().ok is True


def test_scheduler_filter_blocks_enabled_unrelated_tasks_under_active_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from module.config.config import AzurLaneConfig

    catalog = TaskCatalog.from_payload(_profile(), profile_name="ap")
    plan = TaskPlan.from_catalog(catalog, ["RootTask"], ["ExcludedTask"], profile_name="ap")
    policy = task_sandbox.TaskPolicy(
        session_id="sandbox-session",
        repository_root=str(tmp_path.resolve()),
        profile="ap",
        state="active",
        root_tasks=plan.root_tasks,
        excluded_tasks=plan.excluded_tasks,
        catalog=catalog.commands,
        dependencies=(),
        created_at="2026-08-29T00:00:00+00:00",
        updated_at="2026-08-29T00:00:00+00:00",
    )
    config = object.__new__(AzurLaneConfig)
    scheduler_data = copy.deepcopy(_profile())
    for task in ("RootTask", "UnrelatedTask", "ExcludedTask"):
        scheduler_data[task]["Scheduler"]["NextRun"] = datetime(2020, 1, 1)
    scheduler_data["RootTask"]["Scheduler"]["Enable"] = True
    object.__setattr__(
        config,
        "data",
        {
            "RootTask": scheduler_data["RootTask"],
            "UnrelatedTask": scheduler_data["UnrelatedTask"],
            "ExcludedTask": scheduler_data["ExcludedTask"],
        },
    )
    object.__setattr__(config, "config_name", "ap")
    object.__setattr__(config, "pending_task", [])
    object.__setattr__(config, "waiting_task", [])
    monkeypatch.setattr(AzurLaneConfig, "is_hoarding_task", False)
    monkeypatch.setattr(
        AzurLaneConfig,
        "SCHEDULER_PRIORITY",
        property(lambda _self: "RootTask > UnrelatedTask > ExcludedTask"),
    )
    monkeypatch.setattr(
        task_sandbox,
        "task_policy_context",
        lambda _name: task_sandbox.TaskPolicyContext(True, policy, "DEV_TASK_POLICY_ACTIVE"),
    )

    config.get_next_task()

    assert [item.command for item in config.pending_task] == ["RootTask"]


def test_scheduler_filter_clears_queues_for_invalid_enforced_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from module.config.config import AzurLaneConfig

    config = object.__new__(AzurLaneConfig)
    object.__setattr__(config, "data", {})
    object.__setattr__(config, "config_name", "ap")
    object.__setattr__(config, "pending_task", ["stale pending"])
    object.__setattr__(config, "waiting_task", ["stale waiting"])
    monkeypatch.setattr(
        task_sandbox,
        "task_policy_context",
        lambda _name: task_sandbox.TaskPolicyContext(
            True, None, "DEV_TASK_POLICY_CONTEXT_INCOMPLETE"
        ),
    )

    config.get_next_task()

    assert config.pending_task == []
    assert config.waiting_task == []


def test_scheduler_is_unchanged_without_active_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from module.config.config import AzurLaneConfig

    config = object.__new__(AzurLaneConfig)
    scheduler_data = _profile()
    for task in ("RootTask", "UnrelatedTask"):
        scheduler_data[task]["Scheduler"]["NextRun"] = datetime(2020, 1, 1)
        scheduler_data[task]["Scheduler"]["Enable"] = True
    object.__setattr__(
        config,
        "data",
        {task: scheduler_data[task] for task in ("RootTask", "UnrelatedTask")},
    )
    object.__setattr__(config, "config_name", "ap")
    object.__setattr__(config, "pending_task", [])
    object.__setattr__(config, "waiting_task", [])
    monkeypatch.setattr(AzurLaneConfig, "is_hoarding_task", False)
    monkeypatch.setattr(
        AzurLaneConfig,
        "SCHEDULER_PRIORITY",
        property(lambda _self: "RootTask > UnrelatedTask"),
    )
    monkeypatch.setattr(
        task_sandbox,
        "task_policy_context",
        lambda _name: task_sandbox.TaskPolicyContext(False, None, "DEV_TASK_POLICY_NO_CONTEXT"),
    )

    config.get_next_task()

    assert {item.command for item in config.pending_task} == {"RootTask", "UnrelatedTask"}


def test_task_call_is_unchanged_without_active_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from module.config.config import AzurLaneConfig, Function

    config = object.__new__(AzurLaneConfig)
    object.__setattr__(config, "data", {"RootTask": _profile()["RootTask"], "UnrelatedTask": _profile()["UnrelatedTask"]})
    object.__setattr__(config, "config_name", "ap")
    object.__setattr__(config, "modified", {})
    object.__setattr__(config, "auto_update", False)
    object.__setattr__(config, "task", Function(_profile()["RootTask"]))
    monkeypatch.setattr(
        task_sandbox,
        "_active_policy_context",
        lambda _name: task_sandbox.TaskPolicyContext(False, None, "DEV_TASK_POLICY_NO_CONTEXT"),
    )

    assert config.task_call("UnrelatedTask", force_call=True) is True
    assert config.modified["UnrelatedTask.Scheduler.Enable"] is True


def test_task_call_force_does_not_bypass_policy_and_records_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from module.config.config import AzurLaneConfig, Function

    environment = _environment(tmp_path)
    _write_session(environment)
    catalog = TaskCatalog.from_path(environment.profile_file, profile_name=environment.profile_name)
    plan = TaskPlan.from_catalog(catalog, ["RootTask"], ["ExcludedTask"], profile_name=environment.profile_name)
    store = TaskPolicyStore(environment)
    store.create(plan, session_id="sandbox-session", timestamp="2026-08-29T00:00:00+00:00")
    policy = store.read()
    assert policy is not None
    monkeypatch.setattr(task_sandbox.DevEnvironment, "current", lambda: environment)
    monkeypatch.setattr(
        task_sandbox,
        "_active_policy_context",
        lambda _name: task_sandbox.TaskPolicyContext(True, policy, "DEV_TASK_POLICY_ACTIVE"),
    )

    config = object.__new__(AzurLaneConfig)
    object.__setattr__(
        config,
        "data",
        {
            "RootTask": _profile()["RootTask"],
            "ExcludedTask": _profile()["ExcludedTask"],
        },
    )
    object.__setattr__(config, "config_name", "ap")
    object.__setattr__(config, "modified", {})
    object.__setattr__(config, "auto_update", False)
    object.__setattr__(config, "task", Function(_profile()["RootTask"]))

    assert config.task_call("ExcludedTask", force_call=True) is True
    registered = store.read()
    assert registered is not None
    assert registered.dependencies[0].reason == "dependency_override"

    object.__setattr__(config, "task", Function({"Scheduler": {"Command": "UnrelatedTask"}}))
    object.__setattr__(config, "modified", {})
    assert config.task_call("ExcludedTask", force_call=True) is False
    assert config.modified == {}


def test_task_call_does_not_leave_provenance_when_config_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from module.config.config import AzurLaneConfig, Function

    environment = _environment(tmp_path)
    _write_session(environment)
    catalog = TaskCatalog.from_path(environment.profile_file, profile_name=environment.profile_name)
    plan = TaskPlan.from_catalog(catalog, ["RootTask"], ["ExcludedTask"], profile_name=environment.profile_name)
    store = TaskPolicyStore(environment)
    store.create(plan, session_id="sandbox-session", timestamp="2026-08-29T00:00:00+00:00")
    policy = store.read()
    assert policy is not None
    monkeypatch.setattr(task_sandbox.DevEnvironment, "current", lambda: environment)
    monkeypatch.setattr(
        task_sandbox,
        "_active_policy_context",
        lambda _name: task_sandbox.TaskPolicyContext(True, policy, "DEV_TASK_POLICY_ACTIVE"),
    )

    config = object.__new__(AzurLaneConfig)
    object.__setattr__(
        config,
        "data",
        {"RootTask": _profile()["RootTask"], "ExcludedTask": _profile()["ExcludedTask"]},
    )
    object.__setattr__(config, "config_name", "ap")
    object.__setattr__(config, "modified", {})
    object.__setattr__(config, "auto_update", True)
    object.__setattr__(config, "task", Function(_profile()["RootTask"]))
    object.__setattr__(config, "update", lambda: (_ for _ in ()).throw(OSError("synthetic update failure")))

    with pytest.raises(OSError, match="synthetic update failure"):
        config.task_call("ExcludedTask", force_call=True)

    persisted = store.read()
    assert persisted is not None
    assert persisted.dependencies == ()


def test_task_call_reraises_update_failure_without_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from module.config.config import AzurLaneConfig, Function

    monkeypatch.setattr(
        task_sandbox,
        "_active_policy_context",
        lambda _name: task_sandbox.TaskPolicyContext(False, None, "DEV_TASK_POLICY_NO_CONTEXT"),
    )
    config = object.__new__(AzurLaneConfig)
    object.__setattr__(
        config,
        "data",
        {"RootTask": _profile()["RootTask"]},
    )
    object.__setattr__(config, "config_name", "ap")
    object.__setattr__(config, "modified", {})
    object.__setattr__(config, "auto_update", True)
    object.__setattr__(config, "task", Function(_profile()["RootTask"]))
    object.__setattr__(
        config,
        "update",
        lambda: (_ for _ in ()).throw(OSError("synthetic update failure without dependency")),
    )

    with pytest.raises(OSError, match="without dependency"):
        config.task_call("RootTask", force_call=True)


def test_task_call_preserves_update_failure_when_provenance_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from module.config.config import AzurLaneConfig, Function

    environment = _environment(tmp_path)
    _write_session(environment)
    catalog = TaskCatalog.from_path(environment.profile_file, profile_name=environment.profile_name)
    plan = TaskPlan.from_catalog(catalog, ["RootTask"], ["ExcludedTask"], profile_name=environment.profile_name)
    store = TaskPolicyStore(environment)
    store.create(plan, session_id="sandbox-session", timestamp="2026-08-29T00:00:00+00:00")
    policy = store.read()
    assert policy is not None
    monkeypatch.setattr(task_sandbox.DevEnvironment, "current", lambda: environment)
    monkeypatch.setattr(
        task_sandbox,
        "_active_policy_context",
        lambda _name: task_sandbox.TaskPolicyContext(True, policy, "DEV_TASK_POLICY_ACTIVE"),
    )

    def fail_rollback(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic rollback failure")

    monkeypatch.setattr(task_sandbox, "rollback_task_dependency", fail_rollback)
    config = object.__new__(AzurLaneConfig)
    object.__setattr__(
        config,
        "data",
        {"RootTask": _profile()["RootTask"], "ExcludedTask": _profile()["ExcludedTask"]},
    )
    object.__setattr__(config, "config_name", "ap")
    object.__setattr__(config, "modified", {})
    object.__setattr__(config, "auto_update", True)
    object.__setattr__(config, "task", Function(_profile()["RootTask"]))
    object.__setattr__(
        config,
        "update",
        lambda: (_ for _ in ()).throw(OSError("synthetic update failure")),
    )

    with pytest.raises(OSError, match="synthetic update failure"):
        config.task_call("ExcludedTask", force_call=True)


def test_task_call_does_not_mutate_profile_when_provenance_registration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from module.config.config import AzurLaneConfig, Function

    environment = _environment(tmp_path)
    original_profile = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    _write_session(environment)
    catalog = TaskCatalog.from_path(environment.profile_file, profile_name=environment.profile_name)
    plan = TaskPlan.from_catalog(catalog, ["RootTask"], ["ExcludedTask"], profile_name=environment.profile_name)
    store = TaskPolicyStore(environment)
    store.create(plan, session_id="sandbox-session", timestamp="2026-08-29T00:00:00+00:00")
    policy = store.read()
    assert policy is not None
    monkeypatch.setattr(task_sandbox.DevEnvironment, "current", lambda: environment)
    monkeypatch.setattr(
        task_sandbox,
        "_active_policy_context",
        lambda _name: task_sandbox.TaskPolicyContext(True, policy, "DEV_TASK_POLICY_ACTIVE"),
    )
    monkeypatch.setattr(
        task_sandbox,
        "register_task_dependency",
        lambda *_args, **_kwargs: task_sandbox.TaskAuthorization(
            False, False, "DEV_TASK_POLICY_WRITE_FAILED"
        ),
    )

    config = object.__new__(AzurLaneConfig)
    object.__setattr__(
        config,
        "data",
        {"RootTask": _profile()["RootTask"], "ExcludedTask": _profile()["ExcludedTask"]},
    )
    object.__setattr__(config, "config_name", "ap")
    object.__setattr__(config, "modified", {})
    object.__setattr__(config, "auto_update", True)
    object.__setattr__(config, "task", Function(_profile()["RootTask"]))

    assert config.task_call("ExcludedTask", force_call=True) is False
    assert config.modified == {}
    persisted_profile = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    assert persisted_profile == original_profile
    persisted_policy = store.read()
    assert persisted_policy is not None
    assert persisted_policy.dependencies == ()


def test_policy_context_requires_exact_session_and_is_neutral_for_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    _write_session(environment)
    catalog = TaskCatalog.from_path(environment.profile_file, profile_name=environment.profile_name)
    plan = TaskPlan.from_catalog(catalog, ["RootTask"], [], profile_name=environment.profile_name)
    store = TaskPolicyStore(environment)
    store.create(plan, session_id="sandbox-session", timestamp="2026-08-29T00:00:00+00:00")
    monkeypatch.setattr(task_sandbox.DevEnvironment, "current", lambda: environment)
    monkeypatch.setenv(task_sandbox.TASK_POLICY_SESSION_ENV, "sandbox-session")
    monkeypatch.setenv(task_sandbox.TASK_POLICY_ROOT_ENV, str(environment.repository_root))
    monkeypatch.setenv(task_sandbox.TASK_POLICY_FILE_ENV, str(environment.task_policy_file))

    assert task_sandbox.active_task_policy("ap") is not None
    assert task_sandbox.active_task_policy("alas") is None

    monkeypatch.setenv(task_sandbox.TASK_POLICY_SESSION_ENV, "forged-session")
    assert task_sandbox.active_task_policy("ap") is None

    monkeypatch.setenv(task_sandbox.TASK_POLICY_SESSION_ENV, "sandbox-session")
    environment.task_policy_file.write_text("{broken", encoding="utf-8")
    context = task_sandbox.task_policy_context("ap")
    assert context.enforced is True
    assert context.policy is None
    assert context.code == "DEV_TASK_STATE_CORRUPT"

    monkeypatch.delenv(task_sandbox.TASK_POLICY_FILE_ENV)
    incomplete = task_sandbox.task_policy_context("ap")
    assert incomplete.enforced is True
    assert incomplete.policy is None
    assert incomplete.code == "DEV_TASK_POLICY_CONTEXT_INCOMPLETE"
