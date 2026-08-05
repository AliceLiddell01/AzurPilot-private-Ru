from pathlib import Path


workflow = Path('.github/workflows/ci.yml')
text = workflow.read_text(encoding='utf-8')
start_marker = '          uv run --locked python -m unittest -v \\\n'
end_marker = '          uv run --locked pytest -q tests/test_opsi_data_logger_hardening.py\n'
start = text.find(start_marker)
end = text.find(end_marker, start)
if start < 0 or end < 0:
    raise SystemExit('ci.yml: explicit Python test registry was not found')
end += len(end_marker)
replacement = '          uv run --with pytest==9.1.1 python -m pytest -q tests\n'
workflow.write_text(text[:start] + replacement + text[end:], encoding='utf-8')


docs = Path('docs/ci.md')
doc_text = docs.read_text(encoding='utf-8')
old = '- продуктовые regression-тесты WebUI, конфигурации, устройства, OCR и локальных инструментов;\n'
new = '- автоматическое обнаружение всех `tests/test_*.py` через закреплённый `pytest 9.1.1`;\n'
if doc_text.count(old) != 1:
    raise SystemExit('docs/ci.md: Python test description drifted')
doc_text = doc_text.replace(old, new)
old_registry = 'Точный набор продуктовых тестов зафиксирован в job `Python`. Он намеренно не включает реальное устройство, эмулятор или игровой аккаунт.\n'
new_registry = 'Job `Python` не содержит ручного реестра модулей: `pytest` автоматически собирает весь каталог `tests/`. Тесты, которым требуется реальное устройство, эмулятор или игровой аккаунт, должны проверять только локальный контракт либо оставаться в `tools/acceptance/`.\n'
if doc_text.count(old_registry) != 1:
    raise SystemExit('docs/ci.md: manual registry paragraph drifted')
docs.write_text(doc_text.replace(old_registry, new_registry), encoding='utf-8')
