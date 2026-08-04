from __future__ import annotations

from pathlib import Path

from dev_tools import stage8b_apply_review_fixes as fixes

ROOT = Path(__file__).resolve().parents[1]


def patch_manual_model_version() -> None:
    path = ROOT / "module/config/config.py"
    source = path.read_text(encoding="utf-8")
    old = '''    def ocr_model_version(self, name: str) -> str:
        if self.ocr_backend == 'ncnn':
            return 'ncnn'

        if name == 'azur_lane':
            return self.Optimization_OcrModelVersionEnglish
        elif name == 'cn':
            return self.Optimization_OcrModelVersionChinese
        elif name in ['azur_lane_jp', 'jp']:
            return self.Optimization_OcrModelVersionJapanese
        elif name == 'tw':
            return self.Optimization_OcrModelVersionTraditionalChinese
        else:
            return 'auto'
'''
    new = '''    def ocr_model_version(self, name: str) -> str:
        if name != "azur_lane":
            raise ValueError(f"Неподдерживаемая OCR-модель: {name}")
        if self.ocr_backend == "ncnn":
            return "ncnn"
        return self.Optimization_OcrModelVersionEnglish
'''
    if source.count(old) != 1:
        raise RuntimeError("Unexpected ocr_model_version implementation")
    path.write_text(source.replace(old, new), encoding="utf-8")


fixes.patch_manual_model_version = patch_manual_model_version
fixes.main()
