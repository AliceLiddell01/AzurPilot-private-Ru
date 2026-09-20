"""Lock primitive, который реально импортирует MCP HTTP supervisor."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Self

from .mcp_errors import ToolingError
from .mcp_filesystem import path_has_link
from .result import ResultCode


class FileLock:
    """Неблокирующий advisory lock с monotonic timeout."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream = None
        self._locked = False

    def acquire(self, timeout_seconds: float = 0.0) -> bool:
        if timeout_seconds < 0 or timeout_seconds > 24 * 60 * 60:
            raise ValueError("timeout_seconds должен быть bounded")
        if self._locked:
            return True
        if path_has_link(self.path):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Lock path содержит symlink или reparse point.",
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0)
            if self.path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
        except OSError:
            stream.close()
            raise
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._stream = stream
                self._locked = True
                return True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    stream.close()
                    return False
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._locked = False

    def __enter__(self) -> Self:
        if not self.acquire(timeout_seconds=0):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT, "Операция уже выполняется."
            )
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


__all__ = ["FileLock"]
