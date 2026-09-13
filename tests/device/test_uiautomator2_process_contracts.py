from __future__ import annotations

import shlex
from types import SimpleNamespace
from unittest.mock import Mock

from module.device.method.droidcast import DroidCast, build_droidcast_raw_argv
from module.device.method.uiautomator_2 import Uiautomator2, _parse_ps_output


def test_ps_parser_preserves_thread_count_name_and_full_command_line():
    output = (
        "PID PPID NLWP NAME ARGS\n"
        "101 1 4 app_process app_process / ink.mol.droidcast_raw.Main\n"
    )

    processes = _parse_ps_output(output)

    assert len(processes) == 1
    process = processes[0]
    assert process.pid == 101
    assert process.ppid == 1
    assert process.thread_count == 4
    assert process.name == "app_process"
    assert process.cmdline == "app_process / ink.mol.droidcast_raw.Main"


def test_local_process_listing_batches_cmdline_reads_when_ps_has_no_args():
    responses = {
        ("ps", "-A", "-o", "PID,PPID,NAME,CMDLINE"): "unsupported\n",
        ("ps", "-A"): (
            "USER PID PPID VSZ RSS WCHAN ADDR S NAME\n"
            "u0_a1 202 1 100 20 0 0 S app_process\n"
            "u0_a1 203 1 100 20 0 0 S app_process\n"
        ),
    }
    calls = []

    def adb_shell(command, **kwargs):
        command = tuple(map(str, command))
        calls.append((command, kwargs))
        if command[:2] == ("sh", "-c"):
            assert "/proc/$p/cmdline" in command[2]
            assert "202" in command[2]
            assert "203" in command[2]
            return (
                "202|app_process\x00/\x00com.rayworks.droidcast.Main\n"
                "203|app_process\x00/\x00ink.mol.droidcast_raw.Main\n"
            )
        return responses[command]

    device = SimpleNamespace(is_over_http=False, adb_shell=adb_shell)
    processes = Uiautomator2.proc_list_uiautomator2.__wrapped__(device)

    assert [process.pid for process in processes] == [202, 203]
    assert [process.thread_count for process in processes] == [None, None]
    assert [process.cmdline for process in processes] == [
        "app_process / com.rayworks.droidcast.Main",
        "app_process / ink.mol.droidcast_raw.Main",
    ]
    assert sum(command[:2] == ("sh", "-c") for command, _ in calls) == 1
    assert not any(command[0] == "cat" for command, _ in calls)


def test_local_process_listing_reads_each_missing_cmdline_when_batch_read_fails():
    calls = []

    def adb_shell(command, **kwargs):
        command = tuple(map(str, command))
        calls.append(command)
        if command == ("ps", "-A", "-o", "PID,PPID,NAME,CMDLINE"):
            return "unsupported\n"
        if command == ("ps", "-A"):
            return (
                "USER PID PPID VSZ RSS WCHAN ADDR S NAME\n"
                "u0_a1 202 1 100 20 0 0 S app_process\n"
                "u0_a1 203 1 100 20 0 0 S app_process\n"
            )
        if command[:2] == ("sh", "-c"):
            raise RuntimeError("batch shell is unavailable")
        if command == ("cat", "/proc/202/cmdline"):
            return "app_process\x00/\x00com.rayworks.droidcast.Main\x00"
        if command == ("cat", "/proc/203/cmdline"):
            return "app_process\x00/\x00ink.mol.droidcast_raw.Main\x00"
        raise AssertionError(command)

    device = SimpleNamespace(is_over_http=False, adb_shell=adb_shell)
    processes = Uiautomator2.proc_list_uiautomator2.__wrapped__(device)

    assert [process.cmdline for process in processes] == [
        "app_process / com.rayworks.droidcast.Main",
        "app_process / ink.mol.droidcast_raw.Main",
    ]
    assert any(command[:2] == ("sh", "-c") for command in calls)
    assert calls.count(("cat", "/proc/202/cmdline")) == 1
    assert calls.count(("cat", "/proc/203/cmdline")) == 1


def test_local_process_listing_falls_back_only_for_missing_batch_entries():
    calls = []

    def adb_shell(command, **kwargs):
        command = tuple(map(str, command))
        calls.append(command)
        if command == ("ps", "-A", "-o", "PID,PPID,NAME,CMDLINE"):
            return "unsupported\n"
        if command == ("ps", "-A"):
            return (
                "USER PID PPID VSZ RSS WCHAN ADDR S NAME\n"
                "u0_a1 202 1 100 20 0 0 S app_process\n"
                "u0_a1 203 1 100 20 0 0 S app_process\n"
            )
        if command[:2] == ("sh", "-c"):
            return "202|app_process\x00/\x00com.rayworks.droidcast.Main\n"
        if command == ("cat", "/proc/203/cmdline"):
            return "app_process\x00/\x00ink.mol.droidcast_raw.Main\x00"
        raise AssertionError(command)

    device = SimpleNamespace(is_over_http=False, adb_shell=adb_shell)
    processes = Uiautomator2.proc_list_uiautomator2.__wrapped__(device)

    assert [process.cmdline for process in processes] == [
        "app_process / com.rayworks.droidcast.Main",
        "app_process / ink.mol.droidcast_raw.Main",
    ]
    assert calls.count(("cat", "/proc/203/cmdline")) == 1
    assert not any(command == ("cat", "/proc/202/cmdline") for command in calls)


def test_http_process_listing_keeps_string_cmdline_as_one_value():
    response = Mock()
    response.json.return_value = [{
        "pid": 304,
        "ppid": 1,
        "threadCount": 7,
        "cmdline": "app_process / ink.mol.droidcast_raw.Main",
        "name": "app_process",
    }]
    response.raise_for_status.return_value = None
    http = SimpleNamespace(get=Mock(return_value=response))
    device = SimpleNamespace(is_over_http=True, u2=SimpleNamespace(http=http))

    processes = Uiautomator2.proc_list_uiautomator2.__wrapped__(device)

    assert processes[0].thread_count == 7
    assert processes[0].name == "app_process"
    assert processes[0].cmdline == "app_process / ink.mol.droidcast_raw.Main"


def test_droidcast_process_matching_uses_preserved_command_line():
    responses = {
        ("ps", "-A", "-o", "PID,PPID,NAME,CMDLINE"): (
            "PID PPID NAME CMDLINE\n"
            "303 1 app_process app_process / com.torther.droidcasts.Main\n"
        ),
    }

    def adb_shell(command, **kwargs):
        return responses[tuple(map(str, command))]

    device = object.__new__(DroidCast)
    device.is_over_http = False
    device.adb_shell = adb_shell

    processes = list(DroidCast._iter_droidcast_proc(device))

    assert [process.pid for process in processes] == [303]


def test_background_runner_owns_shell_redirection_and_quotes_argv():
    adb_shell = Mock(return_value="404\n")
    device = SimpleNamespace(is_over_http=False, adb_shell=adb_shell)

    response = Uiautomator2.u2_shell_background.__wrapped__(device, [
        "app_process",
        "argument with spaces",
        "a>b",
    ])

    command = adb_shell.call_args.args[0]
    script = command[-1]
    assert shlex.split(script.split(" >/dev/null 2>&1 & echo $!", 1)[0]) == [
        "app_process",
        "argument with spaces",
        "a>b",
    ]
    assert response.success is True
    assert response.pid == 404


def test_background_runner_marks_invalid_pid_as_failure():
    adb_shell = Mock(return_value="not-a-pid\n")
    device = SimpleNamespace(is_over_http=False, adb_shell=adb_shell)

    response = Uiautomator2.u2_shell_background.__wrapped__(device, ["echo", "ok"])

    assert response.success is False
    assert response.pid == 0


def test_http_background_runner_sends_quoted_argv_and_validates_pid():
    response = Mock()
    response.json.return_value = {
        "success": True,
        "pid": "405",
        "description": "started",
    }
    response.raise_for_status.return_value = None
    http = SimpleNamespace(post=Mock(return_value=response))
    device = SimpleNamespace(is_over_http=True, u2=SimpleNamespace(http=http))

    result = Uiautomator2.u2_shell_background.__wrapped__(device, ["echo", "a>b"])

    assert result.success is True
    assert result.pid == 405
    assert http.post.call_args.args == ("/shell/background",)
    assert http.post.call_args.kwargs["data"]["command"] == shlex.join(["echo", "a>b"])


def test_droidcast_raw_argv_contains_no_shell_redirection_tokens():
    argv = build_droidcast_raw_argv("/data/local/tmp/DroidCast_raw.apk")

    assert argv == [
        "CLASSPATH=/data/local/tmp/DroidCast_raw.apk",
        "app_process",
        "/",
        "ink.mol.droidcast_raw.Main",
    ]
    assert ">" not in argv
    assert "/dev/null" not in argv
