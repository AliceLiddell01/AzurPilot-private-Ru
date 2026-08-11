from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Только уже проверенные и сознательно удалённые endpoints. Полезные/opt-in сервисы
# (например yppp random background, ServerChecker, MAA reporting и Wiki refresh)
# сюда намеренно не входят.
FORBIDDEN = (
    "nanoda" + ".work",
    "api." + "gitcode.com",
    "gitcode" + ".com/ddl2/AzurLaneAutoScript",
    "tool." + "appetizer.io/openatx",
    "app." + "pywebio.online",
    "app." + "azurlane.cloud",
    "api." + "kgithub.com",
    "ota." + "maa.plus",
    "download." + "fastgit.org",
    "api." + "xiaomimimo.com",
    "platform." + "xiaomimimo.com",
    "mirrors." + "aliyun.com/docker-ce",
    "mirrors.tuna." + "tsinghua.edu.cn/docker-ce",
    "4." + "ipw.cn",
    "myip." + "ipip.net",
)


def _tracked_text_files():
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        path = ROOT / raw_path.decode("utf-8", errors="surrogateescape")
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                prefix = stream.read(8192)
        except OSError:
            continue
        if b"\0" in prefix:
            continue
        yield path


def test_reviewed_external_endpoints_do_not_return_anywhere_in_repository():
    hits: dict[str, list[str]] = {}
    for path in _tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        relative = str(path.relative_to(ROOT))
        for endpoint in FORBIDDEN:
            if endpoint.lower() in text:
                hits.setdefault(endpoint, []).append(relative)

    assert not hits, "Проверенные удалённые endpoints вернулись в репозиторий: " + repr(hits)
