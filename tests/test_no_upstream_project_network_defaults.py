from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".ps1",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SCAN_ROOTS = (
    ROOT / "assets",
    ROOT / "config",
    ROOT / "deploy",
    ROOT / "dev_tools",
    ROOT / "module",
)
SCAN_FILES = (
    ROOT / "README.md",
    ROOT / "PRIVACY_AND_DISCLAIMER.md",
)


def _iter_text_files():
    for path in SCAN_FILES:
        if path.is_file():
            yield path
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def test_upstream_project_domains_are_not_present_in_personal_runtime_or_templates():
    forbidden = "nanoda" + ".work"
    hits: list[str] = []
    for path in _iter_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if forbidden in text.lower():
            hits.append(str(path.relative_to(ROOT)))

    assert not hits, "Найдены запрещённые upstream project endpoints: " + ", ".join(hits)


def test_network_cleanup_preserves_useful_features_and_removes_only_reviewed_defaults():
    theme = (ROOT / "assets/gui/css/advanced-material-alas.css").read_text(
        encoding="utf-8"
    )
    server_checker = (ROOT / "module/server_checker.py").read_text(encoding="utf-8")
    time_source = (ROOT / "module/config/time_source.py").read_text(encoding="utf-8")
    docker_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    # Пользовательский случайный фон WebUI пока намеренно сохраняется.
    assert 'https://api.yppp.net/api.php' in theme

    # Полезный сервис статуса игровых серверов сохраняется без изменений endpoint.
    assert 'http://sc.shiratama.cn' in server_checker
    assert '/server/get_state' in server_checker
    assert '/server/get_all_state' in server_checker
    assert '/server/list' in server_checker

    # Китайский connectivity probe удалён, функциональность проверки сети сохранена.
    assert 'www.baidu.com' not in server_checker
    assert 'http://www.msftconnecttest.com/connecttest.txt' in server_checker
    assert 'Microsoft Connect Test' in server_checker

    # NTP-механизм и пользовательское переопределение сохранены, China-first defaults удалены.
    assert "AZURPILOT_NTP_SERVERS" in time_source
    assert "AZURPILOT_NTP_DISABLE" in time_source
    assert "time.cloudflare.com" in time_source
    for host in (
        "ntp.ntsc.ac.cn",
        "ntp.aliyun.com",
        "ntp.tencent.com",
        "cn.pool.ntp.org",
    ):
        assert host not in time_source

    # Upstream GitCode mirror не относится к personal/stable и не должен вернуться.
    assert not (ROOT / ".github/workflows/sync2.yml").exists()

    # Мёртвая ссылка на отсутствующий китайский Dockerfile удалена, рабочий Dockerfile сохранён.
    assert "dockerfile: ./deploy/docker/Dockerfile" in docker_compose
    assert "Dockerfile.cn" not in docker_compose
