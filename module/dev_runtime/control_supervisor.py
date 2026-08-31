"""Фиксированная точка входа независимого Dev Runtime control supervisor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from module.dev_runtime.bounded_io import read_bounded_bytes
from module.dev_runtime.contracts import DevEnvironment
from module.dev_runtime.control import (
    CONTROL_MAX_BYTES,
    DevRuntimeControlOperation,
    RuntimeControlManager,
    _is_reparse_point,
)
from module.dev_runtime.target import DevTarget


def _operation_environment(operation_id: str) -> DevEnvironment:
    """Создать bootstrap environment из persisted binding, а не из registry."""

    repository_root = Path.cwd().resolve()
    operation_path = repository_root / "config" / "state" / "dev-runtime-control" / "operation.json"
    for path in (
        repository_root / "config",
        repository_root / "config" / "state",
        operation_path.parent,
        operation_path,
    ):
        if _is_reparse_point(path):
            raise RuntimeError("control operation path is unsafe")
    payload = json.loads(read_bounded_bytes(operation_path, max_bytes=CONTROL_MAX_BYTES).decode("utf-8"))
    operation = DevRuntimeControlOperation.from_payload(payload)
    if operation.control_id != operation_id:
        raise RuntimeError("control operation id mismatch")
    return DevEnvironment(
        repository_root=repository_root,
        python_executable=Path(sys.executable),
        dev_target=DevTarget(operation.target_profile_name),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dev Runtime control supervisor")
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    try:
        # RuntimeControlManager.execute повторно сверяет этот persisted binding
        # с текущими target marker и критической конфигурацией перед мутацией.
        environment = _operation_environment(args.operation_id)
        result = RuntimeControlManager(environment).execute(args.operation_id)
    except Exception:  # noqa: BLE001
        # Supervisor обязан завершить процесс без traceback/секретов в MCP stdio.
        return 1
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
