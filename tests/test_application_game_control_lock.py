from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from module.application import resource_lease
from module.application.errors import ResourceBusyError
from module.application.game_control_lock import (
    profile_mutation_lock,
    profile_mutation_lock_path,
)
from module.application.resource_lease import (
    game_runtime_lease,
    game_runtime_lease_path,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _lock_holder(path: Path, *, crash: bool = False) -> subprocess.Popen[str]:
    script = "\n".join(
        (
            "import os",
            "import sys",
            "from pathlib import Path",
            "from module.application.host_lock import application_host_lock",
            "path = Path(sys.argv[1])",
            "with application_host_lock(path, timeout=5):",
            "    print('ready', flush=True)",
            "    " + ("os._exit(17)" if crash else "sys.stdin.read(1)"),
        )
    )
    return subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        cwd=str(_REPOSITORY_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _lease_holder() -> subprocess.Popen[str]:
    script = "\n".join(
        (
            "import sys",
            "from module.application.resource_lease import game_runtime_lease",
            "with game_runtime_lease(timeout=5):",
            "    print('ready', flush=True)",
            "    sys.stdin.read(1)",
        )
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(_REPOSITORY_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_profile_mutation_lock_is_shared_across_processes_and_released(
    tmp_path: Path,
) -> None:
    path = profile_mutation_lock_path("alpha", repository_root=tmp_path)
    holder = _lock_holder(path)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(ResourceBusyError), profile_mutation_lock(
            "alpha", repository_root=tmp_path, timeout=0.1
        ):
            pass
        assert holder.poll() is None
    finally:
        if holder.poll() is None:
            assert holder.stdin is not None
            holder.stdin.write("release")
            holder.stdin.flush()
        holder.wait(timeout=5)

    with profile_mutation_lock("alpha", repository_root=tmp_path, timeout=0.1):
        pass


def test_profile_mutation_lock_is_released_after_owner_crash(tmp_path: Path) -> None:
    path = profile_mutation_lock_path("alpha", repository_root=tmp_path)
    holder = _lock_holder(path, crash=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        assert holder.wait(timeout=5) == 17
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)

    with profile_mutation_lock("alpha", repository_root=tmp_path, timeout=0.1):
        pass


def test_profile_mutation_lock_does_not_serialize_different_profiles(
    tmp_path: Path,
) -> None:
    with (
        profile_mutation_lock("alpha", repository_root=tmp_path, timeout=0.1),
        profile_mutation_lock("beta", repository_root=tmp_path, timeout=0.1),
    ):
        pass


def test_profile_mutation_lock_path_is_stable_and_rejects_empty_profile(
    tmp_path: Path,
) -> None:
    assert profile_mutation_lock_path("alpha", repository_root=tmp_path) == (
        profile_mutation_lock_path("alpha", repository_root=tmp_path)
    )
    with pytest.raises(ValueError):
        profile_mutation_lock_path("", repository_root=tmp_path)


def test_profile_mutation_lock_serializes_checkouts_for_one_adb_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 40000 + (uuid.uuid4().int % 10000)
    monkeypatch.setenv("ADB_SERVER_SOCKET", f"tcp:127.0.0.1:{port}")
    holder = _lease_holder()
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(ResourceBusyError), profile_mutation_lock(
            "alpha", repository_root=tmp_path, timeout=0.1
        ):
            pass
        assert holder.poll() is None
    finally:
        if holder.poll() is None:
            assert holder.stdin is not None
            holder.stdin.write("release")
            holder.stdin.flush()
        holder.wait(timeout=5)


def test_game_runtime_lease_does_not_serialize_different_adb_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_port = 40000 + (uuid.uuid4().int % 10000)
    second_port = 40000 + (uuid.uuid4().int % 10000)
    if second_port == first_port:
        second_port = 50000

    monkeypatch.setenv("ADB_SERVER_SOCKET", f"tcp:127.0.0.1:{first_port}")
    first_path = game_runtime_lease_path()
    with game_runtime_lease(tmp_path, timeout=0.1):
        monkeypatch.setenv("ADB_SERVER_SOCKET", f"tcp:127.0.0.1:{second_port}")
        assert game_runtime_lease_path() != first_path
        with game_runtime_lease(tmp_path, timeout=0.1):
            pass


def test_game_runtime_lease_cleanup_error_does_not_mask_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 40000 + (uuid.uuid4().int % 10000)
    monkeypatch.setenv("ADB_SERVER_SOCKET", f"tcp:127.0.0.1:{port}")
    lease_path = game_runtime_lease_path()
    marker_path = lease_path.with_name(f"{lease_path.stem}.owner.json")
    original_remove = resource_lease.atomic_remove

    def fail_remove(_path: Path) -> None:
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(resource_lease, "atomic_remove", fail_remove)
    try:
        with pytest.raises(ValueError, match="body failure"), game_runtime_lease(
            timeout=0.1
        ):
            raise ValueError("body failure")
    finally:
        if marker_path.exists():
            original_remove(marker_path)


def test_game_runtime_lease_does_not_probe_pid_without_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(resource_lease, "_process_created_at", lambda _pid: None)
    monkeypatch.setattr(
        resource_lease.os,
        "kill",
        lambda *args: kill_calls.append(args),
    )

    assert resource_lease._marker_is_active((12345, 1.0)) is None
    assert kill_calls == []
