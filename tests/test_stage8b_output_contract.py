from __future__ import annotations

import ast
import unittest

from dev_tools.stage8b_output_contract import _normalize_message_literals
from dev_tools.stage8b_semantic_policy import IMMUTABLE_STAGE8B_BASE_SHA


class Stage8BOutputContractTests(unittest.TestCase):
    def test_message_normalization_preserves_control_flow(self) -> None:
        left = ast.parse("def f(x):\n    if x:\n        logger.info('old')\n        return 1\n").body[0]
        right = ast.parse("def f(x):\n    if x:\n        logger.info('новое')\n        return 1\n").body[0]
        changed = ast.parse("def f(x):\n    if not x:\n        logger.info('новое')\n        return 1\n").body[0]
        self.assertEqual(_normalize_message_literals(left), _normalize_message_literals(right))
        self.assertNotEqual(_normalize_message_literals(left), _normalize_message_literals(changed))

    def test_immutable_base_is_exact_sha(self) -> None:
        self.assertRegex(IMMUTABLE_STAGE8B_BASE_SHA, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
