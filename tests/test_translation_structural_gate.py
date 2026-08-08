from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dev_tools.translation_structural_gate import run_gate, verify_source_pair


def assert_passes(base: str, head: str) -> None:
    assert verify_source_pair(base, head, "module/example.py") == []


def assert_blocked(base: str, head: str) -> None:
    assert verify_source_pair(base, head, "module/example.py")


@pytest.mark.parametrize(
    ("base", "head"),
    [
        ('logger.info("Starting battle")\n', 'logger.info("Начало боя")\n'),
        (
            'logger.info(f"Enemy: {enemy}")\n',
            'logger.info(f"Противник: {enemy}")\n',
        ),
        (
            'logger.info("""Battle started\nFleet ready""")\n',
            'logger.info("""Бой начат\nФлот готов""")\n',
        ),
        ('logger.attr("Enemy", enemy)\n', 'logger.attr("Противник", enemy)\n'),
        (
            'raise RuntimeError("Battle failed")\n',
            'raise RuntimeError("Бой не выполнен")\n',
        ),
        (
            'logger.info("Enemy {name!r}: {hp:.1f}".format(name=name, hp=hp))\n',
            'logger.info("Противник {name!r}: {hp:.1f}".format(name=name, hp=hp))\n',
        ),
        (
            'logger.info("Enemy %s: %.1f" % (enemy, hp))\n',
            'logger.info("Противник %s: %.1f" % (enemy, hp))\n',
        ),
        (
            'logger.info("Enemy %s: %.1f", enemy, hp)\n',
            'logger.info("Противник %s: %.1f", enemy, hp)\n',
        ),
    ],
)
def test_operator_prose_changes_pass(base: str, head: str) -> None:
    assert_passes(base, head)


@pytest.mark.parametrize(
    ("base", "head"),
    [
        ("pass\n", "def added():\n    pass\n"),
        ("def removed():\n    pass\n", "pass\n"),
        ("def f(a=1):\n    pass\n", "def f(a, b=1):\n    pass\n"),
        ("def f(a=1):\n    pass\n", "def f(a=2):\n    pass\n"),
        ("pass\n", "async def added():\n    pass\n"),
        ("async def removed():\n    pass\n", "pass\n"),
        ("pass\n", "class Added:\n    pass\n"),
        ("class Removed:\n    pass\n", "pass\n"),
        ("@old\ndef f():\n    pass\n", "@new\ndef f():\n    pass\n"),
        ("pass\n", "import os\n"),
        ("import os\n", "pass\n"),
        ("from pkg import one\n", "from pkg import two\n"),
        ("if ready:\n    run()\n", "run()\n"),
        ("run()\n", "if ready:\n    run()\n"),
        ("if ready:\n    run()\n", "if done:\n    run()\n"),
        ("for item in items:\n    run(item)\n", "run(items)\n"),
        ("run(items)\n", "for item in items:\n    run(item)\n"),
        ("for item in items:\n    run(item)\n", "for other in values:\n    run(other)\n"),
        ("while ready:\n    run()\n", "run()\n"),
        ("run()\n", "while ready:\n    run()\n"),
        (
            "try:\n    run()\nexcept ValueError:\n    recover()\n",
            "run()\n",
        ),
        (
            "run()\n",
            "try:\n    run()\nexcept ValueError:\n    recover()\n",
        ),
        (
            "try:\n    run()\nexcept ValueError:\n    recover()\n",
            "try:\n    run()\nexcept TypeError:\n    recover()\n",
        ),
        ("with lock:\n    run()\n", "run()\n"),
        ("run()\n", "with lock:\n    run()\n"),
        (
            'match state:\n    case "battle":\n        run()\n',
            "run()\n",
        ),
        (
            "run()\n",
            'match state:\n    case "battle":\n        run()\n',
        ),
        ("def f():\n    pass\n", "def f():\n    return 1\n"),
        ("def f():\n    return 1\n", "def f():\n    pass\n"),
        ("def f():\n    return 1\n", "def f():\n    return 2\n"),
        ("def f():\n    pass\n", "def f():\n    raise ValueError\n"),
        ("def f():\n    raise ValueError\n", "def f():\n    pass\n"),
        ("raise ValueError('failed')\n", "raise TypeError('ошибка')\n"),
        ("for x in xs:\n    pass\n", "for x in xs:\n    break\n"),
        ("for x in xs:\n    break\n", "for x in xs:\n    pass\n"),
        ("for x in xs:\n    pass\n", "for x in xs:\n    continue\n"),
        ("for x in xs:\n    continue\n", "for x in xs:\n    pass\n"),
        ("logger.info('Start')\n", "other.info('Старт')\n"),
        ("call(one, two)\n", "call(two, one)\n"),
        ("call(one=1, two=2)\n", "call(two=2, one=1)\n"),
        ("target = source\n", "other = source\n"),
        ("target = source\n", "target = other\n"),
        ("target = 1\n", "target = 2\n"),
        ("target = one + two\n", "target = one - two\n"),
        ("result = one == two\n", "result = one != two\n"),
        ("result = item.value\n", "result = item.other\n"),
        ('result = data["boss"]\n', 'result = data["босс"]\n'),
        (
            'logger.info(f"Enemy: {enemy}")\n',
            'logger.info(f"Противник: {boss}")\n',
        ),
        (
            'logger.info(f"Enemy: {enemy!r}")\n',
            'logger.info(f"Противник: {enemy!s}")\n',
        ),
        (
            'logger.info(f"HP: {hp:.1f}")\n',
            'logger.info(f"ОЗ: {hp:.0f}")\n',
        ),
        ('"""Module docs."""\n', '"""Документация."""\n'),
        ('if state == "battle":\n    run()\n', 'if state == "бой":\n    run()\n'),
        ('state = "running"\n', 'state = "выполняется"\n'),
        ('mapping["boss"]\n', 'mapping["босс"]\n'),
        (
            'if mode in {"normal", "boss"}:\n    run()\n',
            'if mode in {"normal", "босс"}:\n    run()\n',
        ),
        (
            'match state:\n    case "battle":\n        run()\n',
            'match state:\n    case "бой":\n        run()\n',
        ),
        ('getattr(item, "state")\n', 'getattr(item, "состояние")\n'),
        ('setattr(item, "state", value)\n', 'setattr(item, "состояние", value)\n'),
        ('hasattr(item, "state")\n', 'hasattr(item, "состояние")\n'),
        ('regex.match("battle", value)\n', 'regex.match("бой", value)\n'),
        ('Button("BATTLE_START")\n', 'Button("НАЧАТЬ_БОЙ")\n'),
        ('ocr.match("BATTLE")\n', 'ocr.match("БОЙ")\n'),
        ('open("battle.txt")\n', 'open("бой.txt")\n'),
        ('run_command("adb shell")\n', 'run_command("adb оболочка")\n'),
        ('payload = {"state": "running"}\n', 'payload = {"состояние": "running"}\n'),
        ('unknown("battle")\n', 'unknown("бой")\n'),
        ('def f(label="Battle"):\n    pass\n', 'def f(label="Бой"):\n    pass\n'),
        ('logger.info("Enemy {}".format(enemy))\n', 'logger.info("Противник {0}".format(enemy))\n'),
        ('logger.info("Enemy %s" % enemy)\n', 'logger.info("Противник %r" % enemy)\n'),
        ('logger.info("Enemy %s", enemy)\n', 'logger.info("Противник %d", enemy)\n'),
        ('logger.info("{}".format("value"))\n', 'logger.info("{}".format(r"value"))\n'),
        ('# Battle\nlogger.info("Start")\n', '# Бой\nlogger.info("Старт")\n'),
        ('logger.info("A\\nB")\n', 'logger.info("А B")\n'),
        ('logger.info("A\\tB")\n', 'logger.info("А B")\n'),
        ('logger.info("Start")\n', 'logger.warning("Старт")\n'),
    ],
)
def test_structural_or_machine_changes_fail(base: str, head: str) -> None:
    assert_blocked(base, head)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Translation Gate Test")
    _git(tmp_path, "config", "user.email", "translation-gate@example.invalid")
    return tmp_path


@pytest.mark.parametrize("operation", ["add", "delete", "rename", "copy"])
def test_production_file_topology_changes_fail(repository: Path, operation: str) -> None:
    module = repository / "module"
    module.mkdir()
    original = module / "original.py"
    original.write_text("pass\n", encoding="utf-8")
    base = _commit(repository, "base")

    if operation == "add":
        (module / "added.py").write_text("pass\n", encoding="utf-8")
    elif operation == "delete":
        original.unlink()
    elif operation == "rename":
        original.rename(module / "renamed.py")
    else:
        (module / "copied.py").write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
    head = _commit(repository, operation)

    blockers = run_gate(repository, base, head)
    assert blockers
    if operation == "copy":
        assert any("production file copied:" in blocker for blocker in blockers)


@pytest.mark.parametrize(
    "protected_path",
    [
        ".github/workflows/ci.yml",
        "dev_tools/translation_structural_gate.py",
        "tests/test_translation_structural_gate.py",
    ],
)
def test_protected_path_changes_fail(repository: Path, protected_path: str) -> None:
    path = repository / protected_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("base\n", encoding="utf-8")
    base = _commit(repository, "base")
    path.write_text("head\n", encoding="utf-8")
    head = _commit(repository, "head")

    assert run_gate(repository, base, head)


def test_syntax_error_fails() -> None:
    assert_blocked("logger.info('Start')\n", "logger.info(\n")
