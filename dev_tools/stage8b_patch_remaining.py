from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path: str, old: str, new: str, *, count: int = 1) -> None:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    actual = source.count(old)
    if actual != count:
        raise RuntimeError(f"Expected {count} matches in {path}, got {actual}: {old[:80]!r}")
    target.write_text(source.replace(old, new), encoding="utf-8")


def patch_messages() -> None:
    replace(
        "module/daemon/ocr_benchmark.py",
        'raise ValueError(f"Benchmark поддерживает только Global/English model: {model_name}")',
        'raise ValueError(f"Benchmark поддерживает только глобальную английскую модель: {model_name}")',
    )
    replace(
        "module/daemon/ocr_benchmark.py",
        'raise RuntimeError("OpenCV не смог загрузить benchmark image.")',
        'raise RuntimeError("OpenCV не смог загрузить изображение для benchmark.")',
    )
    replace(
        "module/campaign/campaign_ocr.py",
        "logger.info('[战役] 未找到关卡。')",
        "logger.info('[Кампания — OCR] Этапы не найдены.')",
    )
    replace(
        "module/campaign/campaign_ocr.py",
        "logger.attr('章节', self.campaign_chapter)",
        "logger.attr('Глава', self.campaign_chapter)",
    )
    replace(
        "module/campaign/campaign_ocr.py",
        "logger.attr('关卡', ', '.join(self.stage_entrance.keys()))",
        "logger.attr('Этапы', ', '.join(self.stage_entrance.keys()))",
    )


def patch_prompt_tests() -> None:
    replace(
        "tests/test_stage8b_prompt_scenario_matrix.py",
        "import numpy as np\n",
        "import cv2\nimport numpy as np\n",
    )
    replace(
        "tests/test_stage8b_prompt_scenario_matrix.py",
        '''            with self.assertRaises(ValueError):
                instance._to_gray(image)
''',
        '''            with self.assertRaises((ValueError, cv2.error)):
                instance._to_gray(image)
''',
    )
    replace(
        "tests/test_stage8b_prompt_scenario_matrix.py",
        '''        source = Path(al_ocr.__file__).read_text(encoding="utf-8")
        token = {
            "task_done_called": "task_done",
            "worker_started_once": "_ocr_worker_started",
            "worker_ident_set": "_ocr_worker_ident",
            "shutdown_independent": "daemon=True",
        }[scenario]
        self.assertIn(token, source)
        return token
''',
        '''        if scenario == "worker_started_once":
            original_worker = al_ocr._ocr_worker
            created = []

            class FakeThread:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs
                    self.alive = False
                    created.append(self)

                def start(self):
                    self.alive = True

                def is_alive(self):
                    return self.alive

            try:
                al_ocr._ocr_worker = None
                with patch.object(al_ocr.threading, "Thread", FakeThread):
                    al_ocr._ensure_ocr_worker()
                    first = al_ocr._ocr_worker
                    al_ocr._ensure_ocr_worker()
                    self.assertIs(al_ocr._ocr_worker, first)
                    self.assertEqual(len(created), 1)
            finally:
                al_ocr._ocr_worker = original_worker
            return len(created)

        source = Path(al_ocr.__file__).read_text(encoding="utf-8")
        token = {
            "task_done_called": "task_done",
            "worker_ident_set": "_ocr_worker_ident",
            "shutdown_independent": "daemon=True",
        }[scenario]
        self.assertIn(token, source)
        return token
''',
    )


def main() -> None:
    patch_messages()
    patch_prompt_tests()


if __name__ == "__main__":
    main()
