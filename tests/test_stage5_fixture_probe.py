from __future__ import annotations

import unittest
from pathlib import Path

import imageio.v2 as imageio

from module.ocr.ocr import Ocr


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "game_settings"


class Stage5FixtureProbeTests(unittest.TestCase):
    def test_probe_bottom_fixture_text(self) -> None:
        areas = [
            (175, y, 1207, min(y + 54, 690))
            for y in range(90, 666, 24)
        ]
        lines: list[str] = []

        for name in (
            "options_traversal_bottom.png",
            "options_traversal_bottom_retry.png",
        ):
            image = imageio.imread(FIXTURE_DIR / name)
            image = image[:, :, :3] if image.ndim == 3 else image
            ocr = Ocr(
                areas,
                letter=(255, 255, 255),
                threshold=180,
                name=f"STAGE5_PROBE_{name}",
            )
            ocr.SHOW_LOG = False
            result = ocr.ocr(image)
            lines.append(f"--- {name} ---")
            for area, text in zip(areas, result, strict=True):
                normalized = str(text).strip()
                if normalized:
                    lines.append(f"y={area[1]:03d}: {normalized}")

        self.fail("\n".join(lines))


if __name__ == "__main__":
    unittest.main()
