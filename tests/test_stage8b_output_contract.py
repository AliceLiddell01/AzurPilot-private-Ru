from __future__ import annotations

import ast
import unittest

from dev_tools.stage8b_output_contract import _normalize_translation_literals
from dev_tools.stage8b_semantic_policy import IMMUTABLE_STAGE8B_BASE_SHA


class Stage8BOutputContractTests(unittest.TestCase):
    def test_message_normalization_preserves_control_flow(self) -> None:
        left = ast.parse("def f(x):\n    if x:\n        logger.info('old')\n        return 1\n").body[0]
        right = ast.parse("def f(x):\n    if x:\n        logger.info('новое')\n        return 1\n").body[0]
        changed = ast.parse("def f(x):\n    if not x:\n        logger.info('новое')\n        return 1\n").body[0]
        self.assertEqual(
            _normalize_translation_literals(left),
            _normalize_translation_literals(right),
        )
        self.assertNotEqual(
            _normalize_translation_literals(left),
            _normalize_translation_literals(changed),
        )

    def test_normalization_ignores_docstrings_and_rating_labels_only(self) -> None:
        left = ast.parse(
            "def f(x):\n    'old docs'\n    if x < 5:\n        return 'Fast', 'green'\n"
        ).body[0]
        right = ast.parse(
            "def f(x):\n    'новая документация'\n    if x < 5:\n        return 'Быстро', 'green'\n"
        ).body[0]
        changed_style = ast.parse(
            "def f(x):\n    if x < 5:\n        return 'Быстро', 'red'\n"
        ).body[0]
        self.assertEqual(
            _normalize_translation_literals(left),
            _normalize_translation_literals(right),
        )
        self.assertNotEqual(
            _normalize_translation_literals(left),
            _normalize_translation_literals(changed_style),
        )

    def test_immutable_base_is_exact_sha(self) -> None:
        self.assertRegex(IMMUTABLE_STAGE8B_BASE_SHA, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
