from pathlib import Path

from module.llm import _read_log_tail


def test_read_log_tail_keeps_only_requested_lines(tmp_path: Path):
    path = tmp_path / "error.log"
    path.write_text(
        "".join(f"line-{index:04d}\n" for index in range(500)),
        encoding="utf-8",
    )

    result = _read_log_tail(path, max_bytes=64 * 1024, max_lines=200)

    lines = result.splitlines()
    assert len(lines) == 200
    assert lines[0] == "line-0300"
    assert lines[-1] == "line-0499"


def test_read_log_tail_bounds_single_large_record(tmp_path: Path):
    path = tmp_path / "large.log"
    path.write_bytes(b"x" * (256 * 1024))

    result = _read_log_tail(path, max_bytes=64 * 1024, max_lines=200)

    assert len(result.encode("utf-8")) <= 64 * 1024
