"""Однократная коррекция контракта Stage 4 audit для отчёта Stage 5."""

from pathlib import Path


def patch_auditor() -> None:
    path = Path("dev_tools/russianization_audit.py")
    source = path.read_text(encoding="utf-8")
    result_marker = '''RESULT_FILENAMES = (
    "summary.json",
    "ui_strings.json",
    "first_party_logs.json",
    "asset_manifest.json",
    "locale_dependency_map.json",
    "terminology.json",
    "technical_allowlist.json",
    "asset_decisions.json",
    "en_global_required.json",
    "stage4_report.md",
    "deploy_language_migration.md",
    "stage5_9_test_matrix.md",
)
'''
    allowed_block = result_marker + '''ALLOWED_EXTRA_RESULT_FILENAMES = (
    "stage5_report.md",
)
'''
    if "ALLOWED_EXTRA_RESULT_FILENAMES" not in source:
        if result_marker not in source:
            raise SystemExit("RESULT_FILENAMES block not found")
        source = source.replace(result_marker, allowed_block, 1)

    old_check = '''        unexpected = sorted(
            path.name for path in self.output_dir.glob("*")
            if path.is_file() and path.name not in RESULT_FILENAMES
        ) if self.output_dir.exists() else []
'''
    new_check = '''        allowed_result_files = set(RESULT_FILENAMES) | set(ALLOWED_EXTRA_RESULT_FILENAMES)
        unexpected = sorted(
            path.name for path in self.output_dir.glob("*")
            if path.is_file() and path.name not in allowed_result_files
        ) if self.output_dir.exists() else []
'''
    if "path.name not in allowed_result_files" not in source:
        if old_check not in source:
            raise SystemExit("Unexpected-file audit check not found")
        source = source.replace(old_check, new_check, 1)
    path.write_text(source, encoding="utf-8")


def patch_tests() -> None:
    path = Path("tests/test_russianization_audit.py")
    source = path.read_text(encoding="utf-8")
    old_import = "from dev_tools.russianization_audit import AuditEngine, RESULT_FILENAMES, is_excluded\n"
    new_import = '''from dev_tools.russianization_audit import (
    ALLOWED_EXTRA_RESULT_FILENAMES,
    AuditEngine,
    RESULT_FILENAMES,
    is_excluded,
)
'''
    if "ALLOWED_EXTRA_RESULT_FILENAMES," not in source:
        if old_import not in source:
            raise SystemExit("Audit test import not found")
        source = source.replace(old_import, new_import, 1)

    marker = "    def test_machine_outputs_have_required_structure(self) -> None:\n"
    regression = '''    def test_stage5_report_is_an_allowed_non_baseline_artifact(self) -> None:
        engine = self._engine()
        engine.write()
        for filename in ALLOWED_EXTRA_RESULT_FILENAMES:
            (self.output / filename).write_text("Stage 5 report\\n", encoding="utf-8")
        self.assertEqual(engine.check(), [])

'''
    if "def test_stage5_report_is_an_allowed_non_baseline_artifact" not in source:
        if marker not in source:
            raise SystemExit("Audit test insertion marker not found")
        source = source.replace(marker, regression + marker, 1)
    path.write_text(source, encoding="utf-8")


def main() -> None:
    patch_auditor()
    patch_tests()


if __name__ == "__main__":
    main()
