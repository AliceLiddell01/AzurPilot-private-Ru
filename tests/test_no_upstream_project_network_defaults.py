from __future__ import annotations

import shutil
import subprocess
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
    ROOT / ".github",
    ROOT / "assets",
    ROOT / "config",
    ROOT / "deploy",
    ROOT / "dev_tools",
    ROOT / "module",
    ROOT / "scripts",
    ROOT / "submodule",
    ROOT / "tests",
    ROOT / "tools",
    ROOT / "webapp",
)
SCAN_FILES = (
    ROOT / "README.md",
    ROOT / "PRIVACY_AND_DISCLAIMER.md",
    ROOT / "alas.py",
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
    issue_labeler = (ROOT / ".github/workflows/ai-issue-labeler.yml").read_text(
        encoding="utf-8"
    )
    docker_publish = (ROOT / ".github/workflows/docker-publish.yml").read_text(
        encoding="utf-8"
    )
    developer_tools = (ROOT / "module/webui/app_developer_tools.py").read_text(
        encoding="utf-8"
    )
    docker_deploy_path = ROOT / "deploy/docker/deploy-image.sh"
    docker_deploy = docker_deploy_path.read_text(encoding="utf-8")
    maa_argument = (
        ROOT / "submodule/AlasMaaBridge/module/config/argument/argument.yaml"
    ).read_text(encoding="utf-8")
    maa_handler = (
        ROOT / "submodule/AlasMaaBridge/module/handler/handler.py"
    ).read_text(encoding="utf-8")
    maa_updater_path = ROOT / "submodule/AlasMaaBridge/module/asst/updater.py"
    maa_updater = maa_updater_path.read_text(encoding="utf-8")

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

    # AI labeler остаётся доступен, но запускается только вручную и не навязывает провайдера/модель.
    assert "workflow_dispatch:" in issue_labeler
    assert "required: true" in issue_labeler
    assert "github.event.issue" not in issue_labeler
    assert "- opened" not in issue_labeler
    assert "- edited" not in issue_labeler
    assert "AI_BASE_URL: ${{ vars.AI_LABELER_BASE_URL }}" in issue_labeler
    assert "AI_MODEL: ${{ vars.AI_LABELER_MODEL }}" in issue_labeler
    assert "AI_API_KEY: ${{ secrets.AI_LABELER_API_KEY }}" in issue_labeler
    for token in (
        "api.openai.com",
        "gpt-4.1-mini",
        "api.deepseek.com",
        "deepseek-v4-flash",
        "OPENAI_API_KEY",
    ):
        assert token not in issue_labeler

    # Docker publish остаётся, но публикует image в GHCR самого форка без DockerHub secrets.
    assert "REGISTRY: ghcr.io" in docker_publish
    assert "IMAGE_NAME: ${{ github.repository }}" in docker_publish
    assert "username: ${{ github.actor }}" in docker_publish
    assert "password: ${{ secrets.GITHUB_TOKEN }}" in docker_publish
    assert "DOCKERHUB_USERNAME" not in docker_publish
    assert "DOCKERHUB_TOKEN" not in docker_publish
    assert "hajiming/azurlaneautoscript" not in docker_publish

    # Экран удалённого доступа остаётся, но больше не рекламирует upstream provider.
    assert 't("Gui.Remote.ConfigureHint")' in developer_tools
    assert "app.azurlane.cloud" not in developer_tools

    # Docker helper сохраняет развёртывание, но больше не зависит от китайских repo/image/mirror/IP endpoints.
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(docker_deploy_path)], check=True)
    assert "https://github.com/AliceLiddell01/AzurPilot-private-Ru.git" in docker_deploy
    assert 'BRANCH="${BRANCH:-personal/stable}"' in docker_deploy
    assert "https://download.docker.com/linux/" in docker_deploy
    assert 'IMAGE="${IMAGE:-azurpilot-private-ru:local}"' in docker_deploy
    assert 'docker_cmd build --pull -t "${IMAGE}"' in docker_deploy
    assert 'merge --ff-only "origin/${BRANCH}"' in docker_deploy
    assert "https://ifconfig.me/ip" in docker_deploy
    for token in (
        "gitcode.com",
        "aliyuncs.com",
        "mirrors.aliyun.com",
        "mirrors.tuna.tsinghua.edu.cn",
        "4.ipw.cn",
        "myip.ipip.net",
    ):
        assert token not in docker_deploy

    # MAA updater сохраняет обновление, но использует только официальный GitHub API и release assets.
    compile(maa_updater, str(maa_updater_path), "exec")
    assert "https://api.github.com/" in maa_updater
    assert "browser_download_url" in maa_updater
    for host in (
        "api.kgithub.com",
        "ota.maa.plus",
        "download.fastgit.org",
    ):
        assert host not in maa_updater

    # Penguin/YiTuLiu — реальные opt-in возможности MAA, поэтому их не вырезаем как «китайский мусор».
    assert "ReportToPenguin: false" in maa_argument
    assert "ReportToYiTuLiu: false" in maa_argument
    assert '"report_to_penguin": self.config.MaaRecord_ReportToPenguin' in maa_handler
    assert '"report_to_yituliu": self.config.MaaRecord_ReportToYiTuLiu' in maa_handler

def test_global_only_fork_has_no_cn_uncensored_tool():
    token = "AzurLane" + "Uncensored"
    hits: list[str] = []
    for path in _iter_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if token in text:
            hits.append(str(path.relative_to(ROOT)))

    assert not (ROOT / "module/daemon/uncensored.py").exists()
    assert not hits, "CN-only uncensored feature returned: " + ", ".join(hits)
