from pathlib import Path

path = Path('.github/workflows/ci.yml')
text = path.read_text(encoding='utf-8')
old = '          fetch-depth: 50\n'
new = '          fetch-depth: 100\n'
if text.count(old) != 1:
    raise SystemExit(f'expected one bounded security checkout, found {text.count(old)}')
path.write_text(text.replace(old, new), encoding='utf-8')
