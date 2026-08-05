#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from tools.global_en_shared import SHARED

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "tools/global_en_migrate_impl.py"

source = IMPL.read_text(encoding="utf-8")
start = source.index("SHARED=(")
end = source.index("\nFOREIGN_ROOTS=", start)
source = source[:start] + f"SHARED={SHARED!r}" + source[end:]

old_cleanup = """    (ROOT/".github/workflows/global-en-migration.yml").unlink()
    Path(__file__).unlink()
    run("git","diff","--check")"""
new_cleanup = """    for relative in (
        ".github/workflows/global-en-migration.yml",
        "tools/global_en_migrate.py",
        "tools/global_en_migrate_impl.py",
        "tools/global_en_shared.py",
    ):
        (ROOT / relative).unlink()
    run("git","diff","--check")"""
if source.count(old_cleanup) != 1:
    raise RuntimeError("Migration implementation cleanup contract drifted")
source = source.replace(old_cleanup, new_cleanup)

namespace = {
    "__name__": "__main__",
    "__file__": str(IMPL),
}
exec(compile(source, str(IMPL), "exec"), namespace)
