from __future__ import annotations

import types
import unittest
from unittest import mock

import psutil

from module.device.platform.mumu_process_control import (
    MuMuInstanceIdentityError,
    find_mumu_instance_roots,
    force_stop_mumu_instance,
    is_mumu_instance_root,
    mumu_instance_owned_processes,
)


class FakeProcess:
    def __init__(self, pid, name, cmdline, *, children=None, kill_error=None, cmdline_error=None):
        self.pid = pid
        self._name = name
        self._cmdline = list(cmdline)
        self._children = list(children or [])
        self._kill_error = kill_error
        self._cmdline_error = cmdline_error
        self.killed = False

    def name(self):
        return self._name

    def cmdline(self):
        if self._cmdline_error is not None:
            raise self._cmdline_error
        return list(self._cmdline)

    def children(self, recursive=False):
        self.last_recursive = recursive
        return list(self._children)

    def kill(self):
        if self._kill_error is not None:
            raise self._kill_error
        self.killed = True


class MuMuProcessControlTests(unittest.TestCase):
    def setUp(self):
        self.instance0 = types.SimpleNamespace(
            name='MuMuPlayerGlobal-15.0-0',
            MuMuPlayer12_id=0,
        )
        self.instance1 = types.SimpleNamespace(
            name='MuMuPlayerGlobal-15.0-1',
            MuMuPlayer12_id=1,
        )

    @staticmethod
    def root(instance, pid, *, children=None):
        return FakeProcess(
            pid,
            'MuMuNxDevice.exe',
            [
                r'C:\Program Files\Netease\MuMuPlayer\nx_device\15.0\shell\MuMuNxDevice.exe',
                '-v',
                str(instance.MuMuPlayer12_id),
                '--vm',
                instance.name,
            ],
            children=children,
        )

    def test_root_requires_both_instance_id_and_name(self):
        correct = self.root(self.instance1, 100)
        wrong_id = FakeProcess(
            101,
            'MuMuNxDevice.exe',
            ['MuMuNxDevice.exe', '-v', '0', '--vm', self.instance1.name],
        )
        wrong_name = FakeProcess(
            102,
            'MuMuNxDevice.exe',
            ['MuMuNxDevice.exe', '-v', '1', '--vm', self.instance0.name],
        )
        shared = FakeProcess(103, 'MuMuNxMain.exe', ['MuMuNxMain.exe'])

        self.assertTrue(is_mumu_instance_root(correct, self.instance1))
        self.assertFalse(is_mumu_instance_root(wrong_id, self.instance1))
        self.assertFalse(is_mumu_instance_root(wrong_name, self.instance1))
        self.assertFalse(is_mumu_instance_root(shared, self.instance1))

    def test_candidate_root_cmdline_access_denied_fails_closed(self):
        denied = FakeProcess(
            104,
            'MuMuNxDevice.exe',
            [],
            cmdline_error=psutil.AccessDenied(pid=104),
        )

        with self.assertRaises(MuMuInstanceIdentityError):
            find_mumu_instance_roots(self.instance1, [denied])

    def test_ambiguous_root_fails_closed(self):
        roots = [self.root(self.instance1, 100), self.root(self.instance1, 101)]
        with self.assertRaises(MuMuInstanceIdentityError):
            find_mumu_instance_roots(self.instance1, roots)

    @mock.patch('module.device.platform.mumu_process_control.psutil.wait_procs')
    @mock.patch('module.device.platform.mumu_process_control.psutil.process_iter')
    def test_wrong_instance_and_shared_processes_are_never_killed(self, process_iter, wait_procs):
        child0 = FakeProcess(201, 'crashpad_handler.exe', ['crashpad_handler.exe'])
        root0 = self.root(self.instance0, 200, children=[child0])
        root1 = self.root(self.instance1, 300)
        shared_main = FakeProcess(400, 'MuMuNxMain.exe', ['MuMuNxMain.exe'])
        shared_service = FakeProcess(401, 'MuMuNxSVC.exe', ['MuMuNxSVC.exe', '-Embedding'])

        snapshots = [
            [root0, root1, shared_main, shared_service],
            [root0, root1, shared_main, shared_service],
            [root0, root1, shared_main, shared_service],
            [root0, root1, shared_main, shared_service],
            [root1, shared_main, shared_service],
        ]
        process_iter.side_effect = lambda: snapshots.pop(0)
        wait_procs.return_value = ([child0, root0], [])

        self.assertTrue(force_stop_mumu_instance(self.instance0, timeout=0))
        self.assertTrue(child0.killed)
        self.assertTrue(root0.killed)
        self.assertFalse(root1.killed)
        self.assertFalse(shared_main.killed)
        self.assertFalse(shared_service.killed)

    @mock.patch('module.device.platform.mumu_process_control.psutil.wait_procs')
    @mock.patch('module.device.platform.mumu_process_control.psutil.process_iter')
    def test_identity_change_before_escalation_uses_fresh_target_set(self, process_iter, wait_procs):
        old_child = FakeProcess(501, 'old-child.exe', ['old-child.exe'])
        old_root = self.root(self.instance1, 500, children=[old_child])
        new_child = FakeProcess(601, 'new-child.exe', ['new-child.exe'])
        new_root = self.root(self.instance1, 600, children=[new_child])
        other_root = self.root(self.instance0, 700)

        snapshots = [
            [old_root, other_root],
            [new_root, other_root],
            [new_root, other_root],
            [new_root, other_root],
            [other_root],
        ]
        process_iter.side_effect = lambda: snapshots.pop(0)
        wait_procs.return_value = ([new_child, new_root], [])

        self.assertTrue(force_stop_mumu_instance(self.instance1, timeout=0))
        self.assertFalse(old_root.killed)
        self.assertFalse(old_child.killed)
        self.assertTrue(new_root.killed)
        self.assertTrue(new_child.killed)
        self.assertTrue(other_root.killed is False)

    @mock.patch('module.device.platform.mumu_process_control.psutil.wait_procs')
    @mock.patch('module.device.platform.mumu_process_control.psutil.process_iter')
    def test_ambiguous_identity_before_escalation_fails_without_kill(self, process_iter, wait_procs):
        root = self.root(self.instance1, 800)
        duplicate = self.root(self.instance1, 801)
        process_iter.side_effect = ([root], [root, duplicate])

        self.assertFalse(force_stop_mumu_instance(self.instance1, timeout=0))
        self.assertFalse(root.killed)
        self.assertFalse(duplicate.killed)
        wait_procs.assert_not_called()

    @mock.patch('module.device.platform.mumu_process_control.psutil.process_iter')
    def test_owned_process_set_is_exact_root_plus_descendants(self, process_iter):
        child = FakeProcess(201, 'crashpad_handler.exe', ['crashpad_handler.exe'])
        grandchild = FakeProcess(202, 'renderer.exe', ['renderer.exe'])
        child._children.append(grandchild)
        root = self.root(self.instance1, 200, children=[child, grandchild])
        shared = FakeProcess(400, 'MuMuNxMain.exe', ['MuMuNxMain.exe'])
        process_iter.return_value = [root, shared]

        targets = mumu_instance_owned_processes(self.instance1)
        self.assertEqual({proc.pid for proc in targets}, {200, 201, 202})
        self.assertNotIn(400, {proc.pid for proc in targets})
        self.assertTrue(root.last_recursive)

    @mock.patch('module.device.platform.mumu_process_control.psutil.process_iter')
    def test_access_denied_is_failure_not_success(self, process_iter):
        denied = FakeProcess(
            200,
            'MuMuNxDevice.exe',
            ['MuMuNxDevice.exe', '-v', '1', '--vm', self.instance1.name],
            kill_error=psutil.AccessDenied(pid=200),
        )
        process_iter.return_value = [denied]

        self.assertFalse(force_stop_mumu_instance(self.instance1, timeout=0))


if __name__ == '__main__':
    unittest.main()
