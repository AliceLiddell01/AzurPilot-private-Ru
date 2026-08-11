from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import module.webui.event_calculator as calculator


class EventCalculatorNetworkOptInTests(unittest.TestCase):
    def test_default_load_never_contacts_external_wiki_without_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = str(Path(tmpdir) / "event.json")
            with (
                patch.object(calculator, "CACHE_FILE", cache_file),
                patch.object(calculator.requests, "get") as request,
            ):
                data = calculator.load_event_calculator()

        request.assert_not_called()
        self.assertTrue(data["needs_refresh"])
        self.assertFalse(data["from_cache"])
        self.assertIn("явно выполнить внешний запрос", data["error"])

    def test_default_load_uses_valid_local_cache_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "event.json"
            cache_file.write_text(
                json.dumps(
                    {
                        "cache_version": calculator.CACHE_VERSION,
                        "shop_items": [{"name": "Cached", "price": 1, "quantity": 1}],
                        "stages": [{"name": "D3", "points": 180}],
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(calculator, "CACHE_FILE", str(cache_file)),
                patch.object(calculator.requests, "get") as request,
            ):
                data = calculator.load_event_calculator()

        request.assert_not_called()
        self.assertTrue(data["from_cache"])
        self.assertEqual(data["shop_items"][0]["name"], "Cached")

    def test_force_refresh_is_the_only_path_that_contacts_wiki(self) -> None:
        parsed = {
            "event_name": "Synthetic",
            "end_date": "2026-08-31",
            "shop_items": [{"name": "Item", "price": 100, "quantity": 1}],
            "daily": [],
            "extra": [],
            "stages": [{"name": "D3", "points": 180}],
            "source_url": calculator.WIKI_RAW_URL,
            "updated_at": "2026-08-11 00:00:00",
            "shop_total": 100,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = str(Path(tmpdir) / "event.json")
            with (
                patch.object(calculator, "CACHE_FILE", cache_file),
                patch.object(calculator.requests, "get") as request,
                patch.object(calculator, "parse_event_calculator", return_value=parsed),
            ):
                request.return_value.text = "synthetic wiki text"
                request.return_value.raise_for_status.return_value = None
                data = calculator.load_event_calculator(force_refresh=True)

        request.assert_called_once_with(calculator.WIKI_RAW_URL, timeout=10)
        self.assertFalse(data["from_cache"])
        self.assertEqual(data["event_name"], "Synthetic")


if __name__ == "__main__":
    unittest.main()
