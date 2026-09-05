from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.application import (
    GameApplicationState,
    GameControlService,
    GameLoginResult,
    GameLoginState,
    GameRuntimeRestartResult,
    OperationFailedError,
    OwnershipAmbiguousError,
    PostconditionFailedError,
    PreconditionFailedError,
    RuntimeState,
)
from module.application.errors import GameRuntimePhaseError
from module.application.legacy_game_adapters import LegacyGameApplicationAdapter
from module.application.ports import RuntimeSnapshot


class _Instances:
    def list_instance_names(self) -> tuple[str, ...]:
        return ("alas", "neighbor")

    def read_instance_status(self, name: str) -> RuntimeSnapshot:
        return RuntimeSnapshot(running=True, state_code=RuntimeState.RUNNING)


class _Config:
    def read_config(self, instance: str, task: str | None = None) -> dict[str, object]:
        return {}

    def read_scheduler_queue(
        self,
        instance: str,
        schedulable_tasks: tuple[str, ...],
    ) -> tuple[object, ...]:
        return ()


class _Lifecycle:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def is_running(self, instance: str) -> bool:
        self.calls.append("status")
        return True

    def start_instance(self, instance: str) -> bool:
        self.calls.append("start")
        return True

    def stop_instance(self, instance: str) -> bool:
        self.calls.append("stop")
        return True


class _Adb:
    def restart_adb(self, instance: str | None) -> bool:
        return True


class _Emulator:
    def __init__(self, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def restart_emulator(self, instance: str) -> bool:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class _Application:
    def __init__(
        self,
        states: list[GameApplicationState],
        *,
        start_result: bool = True,
        start_error: Exception | None = None,
        login_state: GameLoginState | None = None,
    ) -> None:
        self.states = states
        self.start_result = start_result
        self.start_error = start_error
        self.read_calls = 0
        self.start_calls = 0
        self.login_state = login_state or GameLoginState(True, True, True, True, True)
        self.login_calls = 0
        self.login_timeout: float | None = None

    def read_state(self, instance: str) -> GameApplicationState:
        index = min(self.read_calls, len(self.states) - 1)
        self.read_calls += 1
        return self.states[index]

    def start_game(self, instance: str) -> bool:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    def login_to_main(
        self,
        instance: str,
        *,
        timeout_seconds: float,
    ) -> GameLoginState:
        self.login_calls += 1
        self.login_timeout = timeout_seconds
        return self.login_state


def _service(
    tmp_path: Path,
    emulator: _Emulator,
    application: _Application,
    *,
    game_start_timeout_seconds: float = 0.0,
    game_start_retry_interval_seconds: float = 0.0,
    game_start_max_attempts: int = 1,
) -> GameControlService:
    return GameControlService(
        instance_reader=_Instances(),
        config_schema=SimpleNamespace(),
        config_writer=_Config(),
        scheduler_tasks=SimpleNamespace(),
        lifecycle=_Lifecycle(),
        emulator=emulator,
        adb=_Adb(),
        application=application,
        config_reader=_Config(),
        mutation_lock_root=tmp_path,
        game_start_timeout_seconds=game_start_timeout_seconds,
        game_start_retry_interval_seconds=game_start_retry_interval_seconds,
        game_start_max_attempts=game_start_max_attempts,
    )


def _not_ready_game() -> GameApplicationState:
    return GameApplicationState(True, False, False)


def _ready_game() -> GameApplicationState:
    return GameApplicationState(True, True, True)


def test_runtime_restart_starts_game_and_confirms_foreground(tmp_path: Path) -> None:
    emulator = _Emulator()
    application = _Application([_not_ready_game(), _ready_game()])

    result = _service(tmp_path, emulator, application).restart_runtime("alas")

    assert result == GameRuntimeRestartResult("alas", True, True, True, True)
    assert emulator.calls == 1
    assert application.start_calls == 1


def test_runtime_restart_does_not_start_profile_or_scheduler(tmp_path: Path) -> None:
    lifecycle = _Lifecycle()
    application = _Application([_not_ready_game(), _ready_game()])
    service = GameControlService(
        instance_reader=_Instances(),
        config_schema=SimpleNamespace(),
        config_writer=_Config(),
        scheduler_tasks=SimpleNamespace(),
        lifecycle=lifecycle,
        emulator=_Emulator(),
        adb=_Adb(),
        application=application,
        config_reader=_Config(),
        mutation_lock_root=tmp_path,
        game_start_timeout_seconds=0.0,
        game_start_retry_interval_seconds=0.0,
        game_start_max_attempts=1,
    )

    service.restart_runtime("alas")

    assert lifecycle.calls == []


def test_runtime_restart_does_not_start_already_foreground_game(tmp_path: Path) -> None:
    application = _Application([_ready_game()])

    result = _service(tmp_path, _Emulator(), application).restart_runtime("alas")

    assert result.game_foreground is True
    assert application.start_calls == 0


def test_runtime_restart_rejects_missing_foreground_postcondition(
    tmp_path: Path,
) -> None:
    application = _Application([_not_ready_game()])

    with pytest.raises(GameRuntimePhaseError) as failure:
        _service(tmp_path, _Emulator(), application).restart_runtime("alas")

    assert failure.value.phase == "game_start"
    assert isinstance(failure.value.cause, PostconditionFailedError)
    assert application.start_calls == 1


def test_runtime_restart_accepts_authoritative_state_after_false_start_result(
    tmp_path: Path,
) -> None:
    application = _Application(
        [_not_ready_game(), _ready_game()],
        start_result=False,
    )

    result = _service(tmp_path, _Emulator(), application).restart_runtime("alas")

    assert result.game_running is True
    assert application.start_calls == 1


def test_runtime_restart_reports_game_start_exception_without_retry(
    tmp_path: Path,
) -> None:
    application = _Application(
        [_not_ready_game()],
        start_error=OperationFailedError("ошибка запуска"),
    )

    with pytest.raises(GameRuntimePhaseError) as failure:
        _service(tmp_path, _Emulator(), application).restart_runtime("alas")

    assert failure.value.phase == "game_start"
    assert isinstance(failure.value.cause, OperationFailedError)
    assert application.start_calls == 1


def test_runtime_restart_failure_never_starts_game(tmp_path: Path) -> None:
    emulator = _Emulator(result=False)
    application = _Application([_not_ready_game()])

    with pytest.raises(GameRuntimePhaseError) as failure:
        _service(tmp_path, emulator, application).restart_runtime("alas")

    assert failure.value.phase == "emulator_restart"
    assert application.start_calls == 0


def test_runtime_restart_requires_adb_ready_before_app_start(tmp_path: Path) -> None:
    application = _Application([GameApplicationState(False, None, None)])

    with pytest.raises(GameRuntimePhaseError) as failure:
        _service(tmp_path, _Emulator(), application).restart_runtime("alas")

    assert failure.value.phase == "game_start"
    assert isinstance(failure.value.cause, PreconditionFailedError)
    assert application.start_calls == 0


def test_runtime_restart_waits_for_adb_readiness_before_app_start(
    tmp_path: Path,
) -> None:
    application = _Application(
        [
            GameApplicationState(False, None, None),
            _not_ready_game(),
            _ready_game(),
        ]
    )

    result = _service(
        tmp_path,
        _Emulator(),
        application,
        game_start_timeout_seconds=1.0,
        game_start_max_attempts=3,
    ).restart_runtime("alas")

    assert result.game_foreground is True
    assert application.read_calls == 3
    assert application.start_calls == 1


def test_runtime_restart_requires_running_and_foreground_not_only_foreground(
    tmp_path: Path,
) -> None:
    application = _Application([GameApplicationState(True, False, True)])

    with pytest.raises(GameRuntimePhaseError) as failure:
        _service(tmp_path, _Emulator(), application).restart_runtime("alas")

    assert isinstance(failure.value.cause, PostconditionFailedError)
    assert application.start_calls == 1


def test_login_runtime_uses_application_login_without_scheduler_or_emulator(
    tmp_path: Path,
) -> None:
    lifecycle = _Lifecycle()
    emulator = _Emulator()
    application = _Application([_ready_game()])
    service = GameControlService(
        instance_reader=_Instances(),
        config_schema=SimpleNamespace(),
        config_writer=_Config(),
        scheduler_tasks=SimpleNamespace(),
        lifecycle=lifecycle,
        emulator=emulator,
        adb=_Adb(),
        application=application,
        config_reader=_Config(),
        mutation_lock_root=tmp_path,
        game_login_timeout_seconds=7.5,
    )

    result = service.login_runtime("alas")

    assert result == GameLoginResult("alas", True, True, True, True, True, True)
    assert application.login_calls == 1
    assert application.login_timeout == 7.5
    assert application.start_calls == 0
    assert emulator.calls == 0
    assert lifecycle.calls == []


def test_login_runtime_requires_authoritative_main_postcondition(tmp_path: Path) -> None:
    application = _Application(
        [_ready_game()],
        login_state=GameLoginState(True, True, True, False, False),
    )

    with pytest.raises(GameRuntimePhaseError) as failure:
        _service(tmp_path, _Emulator(), application).login_runtime("alas")

    assert failure.value.phase == "login"
    assert isinstance(failure.value.cause, PostconditionFailedError)
    assert application.login_calls == 1


@dataclass
class _Device:
    serial: str
    state: str = "device"
    foreground: str = "com.android.launcher"
    pid_output: str = ""
    start_calls: int = 0

    def get_state(self) -> str:
        return self.state


class _Client:
    def __init__(self, devices: list[_Device]) -> None:
        self.devices = devices

    def device_list(self) -> list[_Device]:
        return list(self.devices)


class _AppControl:
    def __init__(self, device: _Device, package: str) -> None:
        self.device = device
        self.package = package

    def app_current_adb(self) -> str:
        return self.device.foreground

    def adb_shell(self, command: object, **kwargs: object) -> str:
        assert command == ["pidof", self.package]
        return self.device.pid_output

    def app_start_adb(self) -> bool:
        self.device.start_calls += 1
        self.device.foreground = self.package
        self.device.pid_output = "42"
        return True


def _application_adapter(
    client: _Client,
    *,
    package: object = "org.example.game",
    target_serial: str | None = "target",
    aliases: tuple[str, ...] = (),
    device_factory=None,
    login_handler_factory=None,
    ui_factory=None,
) -> LegacyGameApplicationAdapter:
    config = SimpleNamespace(Emulator_PackageName=package)
    return LegacyGameApplicationAdapter(
        config_factory=lambda instance: config,
        adb_client_factory=lambda: client,
        app_control_factory=lambda _config, device, _serial, app_package, _client: _AppControl(
            device,
            app_package,
        ),
        device_factory=device_factory,
        login_handler_factory=login_handler_factory,
        ui_factory=ui_factory,
        target_serial_provider=lambda instance: target_serial,
        target_serial_aliases_provider=lambda serial: aliases,
    )


def test_application_adapter_uses_configured_package_and_exact_target() -> None:
    target = _Device("target")
    neighbor = _Device("neighbor")
    adapter = _application_adapter(_Client([target, neighbor]), package="org.example.game")

    assert adapter.read_state("alas") == GameApplicationState(True, False, False)
    assert adapter.start_game("alas") is True
    assert target.start_calls == 1
    assert target.foreground == "org.example.game"
    assert neighbor.start_calls == 0
    assert adapter.read_state("alas") == GameApplicationState(True, True, True)


@pytest.mark.parametrize("package", [None, "", "auto", "not a package"])
def test_application_adapter_rejects_missing_or_invalid_package(package: object) -> None:
    target = _Device("target")
    adapter = _application_adapter(_Client([target]), package=package)

    with pytest.raises(PreconditionFailedError):
        adapter.start_game("alas")

    assert target.start_calls == 0


def test_application_adapter_fails_closed_for_ambiguous_auto_target() -> None:
    first = _Device("first")
    second = _Device("second")
    adapter = _application_adapter(
        _Client([first, second]),
        target_serial=None,
    )

    with pytest.raises(OwnershipAmbiguousError):
        adapter.start_game("alas")

    assert first.start_calls == 0
    assert second.start_calls == 0


def test_application_adapter_revalidates_target_before_start() -> None:
    target = _Device("target")
    replacement = _Device("replacement")
    client = _Client([target])
    adapter = _application_adapter(client)
    assert adapter.read_state("alas").adb_ready is True

    client.devices[:] = [replacement]
    with pytest.raises(OwnershipAmbiguousError):
        adapter.start_game("alas")

    assert target.start_calls == 0
    assert replacement.start_calls == 0


def test_application_adapter_preserves_handover_failure_step_and_cause() -> None:
    target = _Device("target")

    def fail_device(_config: object) -> object:
        raise RuntimeError("synthetic device failure")

    adapter = _application_adapter(
        _Client([target]),
        device_factory=fail_device,
    )

    with pytest.raises(OperationFailedError) as failure:
        adapter.return_to_main("alas")

    assert failure.value.handover_step == "device"
    assert failure.value.cause_type == "RuntimeError"
    assert not hasattr(failure.value, "cause_message")


class _LoginDevice:
    def __init__(self, running: bool) -> None:
        self.running = running
        self.screenshot_calls = 0
        self.release_calls = 0

    def app_is_running(self) -> bool:
        return self.running

    def screenshot(self) -> None:
        self.screenshot_calls += 1

    def release_resource(self) -> None:
        self.release_calls += 1


class _LoginHandler:
    def __init__(self, target: _Device, package: str) -> None:
        self.target = target
        self.package = package
        self.calls: list[tuple[str, float]] = []

    def handle_app_login(self, *, timeout_seconds: float) -> None:
        self.calls.append(("handle", timeout_seconds))
        self.target.foreground = self.package
        self.target.pid_output = "42"

    def app_start(self, *, timeout_seconds: float) -> None:
        self.calls.append(("start", timeout_seconds))
        self.target.foreground = self.package
        self.target.pid_output = "42"


class _LoginUI:
    def __init__(self, main: bool) -> None:
        self.main = main
        self.calls = 0

    def is_in_main(self) -> bool:
        self.calls += 1
        return self.main


@pytest.mark.parametrize(
    ("running", "expected_method"),
    ((True, "handle"), (False, "start")),
)
def test_application_adapter_reuses_login_handler_and_confirms_main_ui(
    running: bool,
    expected_method: str,
) -> None:
    package = "org.example.game"
    target = _Device("target")
    device = _LoginDevice(running)
    handler = _LoginHandler(target, package)
    ui = _LoginUI(main=True)
    adapter = _application_adapter(
        _Client([target]),
        package=package,
        device_factory=lambda config: device,
        login_handler_factory=lambda config, current_device: handler,
        ui_factory=lambda config, current_device: ui,
    )

    result = adapter.login_to_main("alas", timeout_seconds=3.5)

    assert result == GameLoginState(True, True, True, True, True)
    assert handler.calls == [(expected_method, 3.5)]
    assert device.screenshot_calls == 1
    assert device.release_calls == 1
    assert ui.calls == 1


def test_application_adapter_rejects_login_without_authoritative_main_ui() -> None:
    target = _Device("target")
    device = _LoginDevice(True)
    handler = _LoginHandler(target, "org.example.game")
    adapter = _application_adapter(
        _Client([target]),
        package="org.example.game",
        device_factory=lambda config: device,
        login_handler_factory=lambda config, current_device: handler,
        ui_factory=lambda config, current_device: _LoginUI(main=False),
    )

    with pytest.raises(PostconditionFailedError, match="главный экран"):
        adapter.login_to_main("alas", timeout_seconds=3.5)

    assert device.release_calls == 1


def test_application_adapter_resolves_fresh_alias_without_touching_neighbor() -> None:
    target = _Device("127.0.0.1:16416")
    neighbor = _Device("127.0.0.1:16417")
    adapter = _application_adapter(
        _Client([target, neighbor]),
        target_serial="configured-target",
        aliases=("127.0.0.1:16416",),
    )

    assert adapter.start_game("alas") is True
    assert target.start_calls == 1
    assert neighbor.start_calls == 0


def test_application_adapter_rejects_non_ready_target_before_start() -> None:
    target = _Device("target", state="offline")
    adapter = _application_adapter(_Client([target]))

    with pytest.raises(OwnershipAmbiguousError):
        adapter.start_game("alas")

    assert target.start_calls == 0


def test_runtime_result_models_reject_unverified_success() -> None:
    with pytest.raises(ValueError):
        GameRuntimeRestartResult("alas", False, True, True, True)
    with pytest.raises(TypeError):
        GameApplicationState(True, "yes", True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GameLoginResult("alas", True, True, True, True, False, True)
