from pathlib import Path

# Transport-only helper; excluded from the audit fingerprint before generation.
AUDITOR = Path('dev_tools/russianization_audit.py')
source = AUDITOR.read_text(encoding='utf-8')
old = '    RESULTS_RELATIVE.as_posix() + "/",\n}'
new = '    "github/workflows/", "github/stage4_",\n    RESULTS_RELATIVE.as_posix() + "/",\n}'
if old in source:
    source = source.replace(old, new, 1)
elif new not in source:
    raise SystemExit('Expected exclusion block was not found.')
AUDITOR.write_text(source, encoding='utf-8')

TESTS = Path('tests/test_russianization_audit.py')
tests = TESTS.read_text(encoding='utf-8')
old_import = 'from dev_tools.russianization_audit import AuditEngine, RESULT_FILENAMES'
new_import = 'from dev_tools.russianization_audit import AuditEngine, RESULT_FILENAMES, is_excluded'
if old_import in tests:
    tests = tests.replace(old_import, new_import, 1)
elif new_import not in tests:
    raise SystemExit('Expected audit test import was not found.')

marker = '    def test_all_locale_files_and_key_drift_are_detected(self) -> None:\n'
test_block = (
    '    def test_ci_and_stage4_transport_are_excluded(self) -> None:\n'
    '        self.assertTrue(is_excluded(".github/workflows/lint.yml"))\n'
    '        self.assertTrue(is_excluded(".github/stage4_regenerate.py"))\n'
    '        self.assertFalse(is_excluded("module/webui/app.py"))\n\n'
)
if 'def test_ci_and_stage4_transport_are_excluded' not in tests:
    if marker not in tests:
        raise SystemExit('Expected audit test insertion point was not found.')
    tests = tests.replace(marker, test_block + marker, 1)
TESTS.write_text(tests, encoding='utf-8')
