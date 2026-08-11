from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>`]+")
STUN_TURN_RE = re.compile(r"(?i)\b(?:stun|turn|turns):[A-Za-z0-9._-]+(?::\d+)?")
BARE_HOST_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(?:[A-Za-z0-9-]+\.)+"
    r"(?:com|net|org|io|cn|work|dev|app|me|xyz|top|cc|ru|jp|tw|hk|cloud|ai|tv|info|site|online|tech|pro|co|us|uk|de|fr)"
    r"(?::\d+)?"
    r"(?![A-Za-z0-9_])"
)


def _tracked_files() -> list[Path]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / p.decode("utf-8") for p in raw.split(b"\0") if p]


def _host_from_url(value: str) -> str | None:
    cleaned = value.rstrip(".,);]}>")
    try:
        return urlsplit(cleaned).hostname
    except ValueError:
        return None


def test_external_host_inventory_for_manual_audit():
    hits: dict[str, set[str]] = defaultdict(set)
    for path in _tracked_files():
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        rel = str(path.relative_to(ROOT)).replace("\\", "/")

        for match in URL_RE.findall(text):
            host = _host_from_url(match)
            if host:
                hits[host.lower()].add(rel)

        for match in STUN_TURN_RE.findall(text):
            host = match.split(":", 1)[1].rsplit(":", 1)[0]
            hits[host.lower()].add(rel)

        for match in BARE_HOST_RE.findall(text):
            host = match.rsplit(":", 1)[0] if match.rsplit(":", 1)[-1].isdigit() else match
            # Reverse-DNS Java/Android package names are not network hosts.
            if host.startswith(("com.", "org.", "net.")):
                continue
            hits[host.lower()].add(rel)

    lines = ["EXTERNAL HOST INVENTORY (temporary audit)"]
    for host in sorted(hits):
        paths = sorted(hits[host])
        sample = ", ".join(paths[:8])
        suffix = f" (+{len(paths) - 8} more)" if len(paths) > 8 else ""
        lines.append(f"{host} | files={len(paths)} | {sample}{suffix}")

    assert False, "\n" + "\n".join(lines)
