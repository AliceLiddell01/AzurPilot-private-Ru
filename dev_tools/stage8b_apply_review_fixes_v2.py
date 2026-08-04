from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dev_tools import stage8b_apply_review_fixes as fixes


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


def patch_owned_translations() -> None:
    replacements = {
        "module/campaign/campaign_ocr.py": {
            "logger.warning(f'[战役-OCR] 未知的关卡名称: {name}')": (
                "logger.warning(f'[Кампания — OCR] Неизвестное имя этапа: {name}')"
            ),
            "logger.warning('[战役] 数字与文本之间未找到间隔。')": (
                "logger.warning('[Кампания — OCR] Не найден интервал между номером этапа и текстом.')"
            ),
        },
        "module/os/sea_miles_ocr.py": {
            'logger.warning(f"[大世界-里程] 异常的海域里程: {result}")': (
                'logger.warning(f"[Operation Siren — OCR] Недопустимое значение Sea Miles: {result}")'
            ),
        },
        "module/device/device.py": {
            "logger.info('[设备-基准测试] 运行OCR设备基准测试')": (
                "logger.info('[Устройство — OCR benchmark] Проверка доступных OCR-устройств')"
            ),
        },
    }
    for relative, mapping in replacements.items():
        path = ROOT / relative
        source = path.read_text(encoding="utf-8")
        for old, new in mapping.items():
            count = source.count(old)
            if count < 1:
                raise RuntimeError(f"Translation owner string missing: {relative}: {old}")
            source = source.replace(old, new)
        path.write_text(source, encoding="utf-8")


fixes.patch_manual_model_version = patch_manual_model_version
fixes.patch_owned_translations = patch_owned_translations
fixes.main()
