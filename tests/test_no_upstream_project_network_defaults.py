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
    llm_argument = (ROOT / "module/config/argument/argument.yaml").read_text(encoding="utf-8")
    llm_runtime = (ROOT / "module/llm.py").read_text(encoding="utf-8")
    llm_ru_i18n = (ROOT / "module/config/i18n/ru-RU.json").read_text(encoding="utf-8")
    docker_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    issue_labeler = (ROOT / ".github/workflows/ai-issue-labeler.yml").read_text(
        encoding="utf-8"
    )
    issue_labeler_script = (ROOT / ".github/scripts/ai_issue_labeler.py").read_text(
        encoding="utf-8"
    )
    alas_utils = (ROOT / "assets/gui/js/alas-utils.js").read_text(encoding="utf-8")
    gui_argument = (ROOT / "module/config/argument/gui.yaml").read_text(encoding="utf-8")
    ru_i18n = (ROOT / "module/config/i18n/ru-RU.json").read_text(encoding="utf-8")
    en_i18n = (ROOT / "module/config/i18n/en-US.json").read_text(encoding="utf-8")
    combat_runtime = (ROOT / "module/combat/combat.py").read_text(encoding="utf-8")
    docker_publish = (ROOT / ".github/workflows/docker-publish.yml").read_text(
        encoding="utf-8"
    )
    developer_tools = (ROOT / "module/webui/app_developer_tools.py").read_text(
        encoding="utf-8"
    )
    remote_access = (ROOT / "module/webui/remote_access.py").read_text(
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

    # Advanced Material theme больше не делает скрытый браузерный запрос за случайным фоном.
    external_background = "api.yppp" + ".net"
    assert external_background not in theme
    assert "--alas-apple-bg-image: linear-gradient(" in theme

    # Полезный сервис статуса игровых серверов сохраняется без изменений endpoint.
    assert 'http://sc.shiratama.cn' in server_checker
    assert '/server/get_state' in server_checker
    assert '/server/get_all_state' in server_checker
    assert '/server/list' in server_checker

    # Китайский connectivity probe удалён, функциональность проверки сети сохранена.
    assert 'www.baidu.com' not in server_checker
    assert 'http://www.msftconnecttest.com/connecttest.txt' in server_checker
    assert 'Microsoft Connect Test' in server_checker

    # Legacy LLM-анализатор сохранён как явная opt-in функция без предустановленного провайдера.
    assert "LlmAnalysis: false" in llm_argument
    assert 'LlmApiBase:\n    type: textarea\n    value: ""' in llm_argument
    assert 'LlmModel:\n    value: ""' in llm_argument
    assert "if not api_key or not api_base or not model:" in llm_runtime
    assert "max_tokens=1200" in llm_runtime
    assert "lines[-200:]" in llm_runtime
    assert "Предустановленного провайдера нет" in llm_ru_i18n
    for token in (
        "xiaomimimo" + ".com",
        "platform.deepseek" + ".com",
        "mimo-v2.5-pro",
    ):
        assert token not in llm_argument
        assert token not in llm_runtime
        assert token not in llm_ru_i18n

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

    # Остатки удалённой announcement/API инфраструктуры не должны возвращаться.
    announcement_tokens = (
        "alasShow" + "Announcement",
        "alas_shown_" + "announcements",
        "alas-announcement-" + "modal",
    )
    for token in announcement_tokens:
        assert token not in alas_utils
    assert "  Announcement:\n" not in gui_argument
    assert '"Announcement":' not in ru_i18n
    assert '"Announcement":' not in en_i18n
    assert not (ROOT / "module/base/api_client.py").exists()
    assert "module.base." + "api_client" not in combat_runtime
    assert "Api" + "Client" not in combat_runtime

    # Labeler теперь GitHub-only: dormant GitCode transport/event compatibility удалена.
    gitcode_token = "git" + "code"
    assert gitcode_token not in issue_labeler_script.lower()
    assert "LABELER_PLATFORM" not in issue_labeler
    assert "GITHUB_REPOSITORY" in issue_labeler_script
    assert "https://api.github.com" in issue_labeler_script

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

    # Старый встроенный Git-updater уже удалён из runtime: его CN-oriented UI residues не должны возвращаться.
    deploy_config = (ROOT / "deploy/config.py").read_text(encoding="utf-8")
    for key in ("Repository", "Branch", "GitExecutable", "GitProxy", "SSLVerify"):
        assert f'"{key}"' in deploy_config
    legacy_repo_hint = "git://git." + "pull/AzurPilot"
    assert legacy_repo_hint not in ru_i18n
    assert legacy_repo_hint not in en_i18n
    assert "Пользователи из КНР" not in ru_i18n
    assert "CN users may use" not in en_i18n
    assert "китайскими эмуляторами Android" not in ru_i18n
    assert "Chinese Android emulators" not in en_i18n
    assert "конфликтов версий" in ru_i18n
    assert "version conflicts" in en_i18n

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

    # SSH provider остаётся полностью настраиваемым: private runtime не имеет скрытого fallback-хоста.
    obsolete_provider = "app.pywebio" + ".online"
    assert obsolete_provider not in remote_access
    assert "server, server_port = _parse_host_port(State.deploy_config.SSHServer)" in remote_access
    assert '"server": target' in remote_access

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

    # Калькулятор события сохраняется, но его китайский Wiki endpoint больше не вызывается при открытии страницы.
    event_calculator = (ROOT / "module/webui/event_calculator.py").read_text(encoding="utf-8")
    event_tools = (ROOT / "module/webui/app_event_tools.py").read_text(encoding="utf-8")
    assert "if not force_refresh:" in event_calculator
    assert "needs_refresh" in event_calculator
    assert "requests.get(WIKI_RAW_URL, timeout=10)" in event_calculator
    assert "выполняется только после явного нажатия" in event_tools
    assert "Загрузить данные Wiki" in event_tools

    # Старый uiautomator2 installer больше не переключается на скрытый внешний fallback.
    u2_sources = [
        (ROOT / "deploy/adb.py").read_text(encoding="utf-8"),
        (ROOT / "deploy/Windows/adb.py").read_text(encoding="utf-8"),
        (ROOT / "deploy/patch.py").read_text(encoding="utf-8"),
        (ROOT / "module/device/connection.py").read_text(encoding="utf-8"),
    ]
    hidden_u2_host = "tool.appetizer" + ".io"
    for source in u2_sources:
        assert hidden_u2_host not in source
    assert "uiautomator2cache" in u2_sources[2]
    assert "внешний fallback отключён" in u2_sources[0]
    assert "внешний fallback отключён" in u2_sources[1]
    assert "внешний fallback отключён" in u2_sources[3]

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
