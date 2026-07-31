from pathlib import Path

path = Path('dev_tools/russianization_audit.py')
source = path.read_text(encoding='utf-8')
old_loop = '        for token in candidates:\n'
new_loop = '        for token in sorted(candidates):\n'
old_tail = '''        generated = [ref for ref in refs if any(token in ref["path"].lower() for token in ("generated", "button_extract", "config_updater"))]
        tests = [ref for ref in refs if ref["path"].startswith("tests/")]
        static = [ref for ref in refs if ref not in generated and ref not in tests]
        return static, dynamic, generated, tests
'''
new_tail = '''        refs.sort(key=lambda item: (item["path"], item["line"], item["match"]))
        dynamic.sort(key=lambda item: (item["path"], item["line"], item["code"]))
        generated = [ref for ref in refs if any(token in ref["path"].lower() for token in ("generated", "button_extract", "config_updater"))]
        tests = [ref for ref in refs if ref["path"].startswith("tests/")]
        static = [ref for ref in refs if ref not in generated and ref not in tests]
        return static, dynamic, generated, tests
'''
if old_loop not in source:
    raise SystemExit('Expected candidate loop was not found.')
if old_tail not in source:
    raise SystemExit('Expected reference classification block was not found.')
source = source.replace(old_loop, new_loop, 1)
source = source.replace(old_tail, new_tail, 1)
path.write_text(source, encoding='utf-8')
