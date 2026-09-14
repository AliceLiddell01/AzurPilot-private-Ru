from __future__ import annotations

import types
import unittest
from unittest import mock

from module.device.platform.mumu_process_control import force_stop_mumu_instance


class FakeProcess:
    def __init__(self, pid, name, cmdline, *, children=None):
        self.pid = pid
        self._name = name
        self._cmdline = list(cmdline)
        self._children = list(children or [])
        self.killed = False

    def name(self):
        return self._name

    def cmdline(self):
        return list(self._cmdline)

    def children(self, recursive=False):
        return list(self._children)

    def kill(self):
        self.killed = True


class MuMuProcessControlFailureTests(unittest.TestCase):
    def test_persisting_target_after_kill_is_failure(self):
        instance = types.SimpleNamespace(
            name='MuMuPlayerGlobal-15.0-1',
            MuMuPlayer12_id=1,
        )
        root = FakeProcess(
            50260,
            'MuMuNxDevice.exe',
            ['MuMuNxDevice.exe', '-v', '1', '--vm', instance.name],
        )

        with (
            mock.patch(
                'module.device.platform.mumu_process_control.psutil.process_iter',
                return_value=[root],
            ),
            mock.patch(
                'module.device.platform.mumu_process_control.psutil.wait_procs',
                return_value=([], [root]),
            ),
        ):
            result = force_stop_mumu_instance(instance, timeout=0)

        self.assertFalse(result)
        self.assertTrue(root.killed)


if __name__ == '__main__':
    unittest.main()
