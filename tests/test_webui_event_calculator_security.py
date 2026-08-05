
from __future__ import annotations

import unittest

from module.webui.event_calculator import build_event_calculator_js


class WebUiEventCalculatorSecurityTests(unittest.TestCase):
    def test_external_names_are_escaped_before_table_rendering(self) -> None:
        data = {
            "event_name": "<img src=x onerror=alert(1)>",
            "shop_items": [{"name": "<script>alert(1)</script>", "quantity": 1}],
            "daily": [],
            "extra": [],
            "stages": [],
        }

        script = build_event_calculator_js("fixture", data, {})

        self.assertIn("function escapeHtml(value)", script)
        self.assertEqual(script.count("${escapeHtml(item.name)}"), 3)
        self.assertIn("event-name", script)
        self.assertIn(".textContent", script)
        self.assertNotIn("<td>${item.name", script)


if __name__ == "__main__":
    unittest.main()
