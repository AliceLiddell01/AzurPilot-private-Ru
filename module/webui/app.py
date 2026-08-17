"""Совместимая точка входа AzurPilot WebUI и фабрика ASGI-приложения.

Главный WebUI-контроллер собирается из mixin-классов для панели, меню и
настроек разработчика, статистики, инструментов событий и других страниц.
Модуль также создаёт ASGI-приложение, регистрирует маршруты и используется
``gui.py`` как верхнеуровневая точка запуска WebUI.
"""

from hashlib import sha256
from pathlib import Path

from module.webui.app_dashboard import DashboardMixin
from module.webui.app_dependencies import (
    Dict,
    Frame,
    IS_ON_PHONE_CLOUD,
    List,
    PUBLIC_WEBUI_PASSWORD_GENERATE_FAILED_MESSAGE,
    ProcessManager,
    RichLog,
    State,
    argparse,
    asgi_app,
    get_localstorage_values,
    info,
    lang,
    load_webui_styles,
    local,
    logger,
    login,
    popup,
    run_js,
    set_env,
    task_handler,
    time,
    webconfig,
)
from module.webui.app_developer_menu import DeveloperMenuMixin
from module.webui.app_developer_settings import DeveloperSettingsMixin
from module.webui.app_developer_tools import DeveloperToolsMixin
from module.webui.app_event_datamine import EventDatamineMixin
from module.webui.app_event_general_presentation import EventGeneralPresentationMixin
from module.webui.app_event_general_v2 import EventGeneralV2Mixin
from module.webui.app_event_layout import EventLayoutMixin
from module.webui.app_event_profiles import EventProfilesMixin
from module.webui.app_event_shop_live import EventShopLiveMixin
from module.webui.app_event_shop_safety import EventShopSafetyMixin
from module.webui.app_event_tools import EventToolsMixin
from module.webui.app_helpers import (
    DEMO_DEVICE_ID_TEXT,
    WEBUI_AUTO_PASSWORD_FILE,
    build_copyable_device_id,
    build_muted_notice,
    build_recommendation_box,
    build_simple_table,
    ensure_public_webui_password,
    generate_webui_password,
    is_demo_mode,
    is_public_webui_host,
    is_webui_password_set,
    read_webapp_template,
    timedelta_to_text,
)
from module.webui.app_home import HomeMixin
from module.webui.app_instances import InstanceMixin
from module.webui.app_lifecycle import clearup, startup
from module.webui.app_manage import app_manage
from module.webui.app_overview import OverviewMixin
from module.webui.app_shell import AppShellMixin
from module.webui.app_stat_action_point import ActionPointStatisticsMixin
from module.webui.app_stat_action_point_toolbar import ActionPointToolbarMixin
from module.webui.app_stat_commission import CommissionIncomeStatisticsMixin
from module.webui.app_stat_opsi import OpsiStatisticsMixin
from module.webui.app_stat_opsi_export import OpsiExportMixin
from module.webui.app_stat_resource import ResourceStatisticsMixin
from module.webui.app_stat_ship import ShipExperienceStatisticsMixin
from module.webui.app_statistics_page import StatisticsPageMixin
from module.webui.app_task_config import TaskConfigMixin
from module.webui.utils import add_css, filepath_css


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _versioned_static_asset(relative_path: str) -> str:
    """Вернуть относительный адрес статического ресурса с хешем содержимого."""
    digest = sha256((PROJECT_ROOT / relative_path).read_bytes()).hexdigest()[:12]
    return f"static/{relative_path}?v={digest}"


INITIAL_WEBUI_CSS = _versioned_static_asset("assets/gui/css/alas.css")


class AlasGUI(
    AppShellMixin,
    StatisticsPageMixin,
    ActionPointStatisticsMixin,
    ActionPointToolbarMixin,
    ResourceStatisticsMixin,
    OpsiStatisticsMixin,
    OpsiExportMixin,
    ShipExperienceStatisticsMixin,
    CommissionIncomeStatisticsMixin,
    EventShopLiveMixin,
    EventGeneralPresentationMixin,
    EventGeneralV2Mixin,
    EventProfilesMixin,
    EventDatamineMixin,
    EventShopSafetyMixin,
    EventLayoutMixin,
    TaskConfigMixin,
    EventToolsMixin,
    OverviewMixin,
    DashboardMixin,
    DeveloperMenuMixin,
    DeveloperSettingsMixin,
    DeveloperToolsMixin,
    InstanceMixin,
    HomeMixin,
    Frame,
):
    """Сеансовый контроллер, объединяющий все представления WebUI.

    Порядок mixin-классов задаёт слой композиции возможностей. Статистическая
    страница вызывает конкретные методы представлений через ``self``, поэтому
    отдельные модули остаются независимо сопровождаемыми при сохранении
    существующего сеансового интерфейса.
    """

    ALAS_MENU: Dict[str, Dict[str, List[str]]]
    ALAS_ARGS: Dict[str, Dict[str, Dict[str, Dict[str, str]]]]
    theme = "default"
    _log = RichLog


def debug() -> None:
    """Инициализировать WebUI и запустить интерактивный отладочный сеанс."""
    startup()
    AlasGUI().run()


def app():
    """Создать ASGI-приложение для запуска через Uvicorn."""
    parser = argparse.ArgumentParser(description="Веб-служба AzurPilot")
    parser.add_argument(
        "-k", "--key", type=str, help="Пароль AzurPilot. По умолчанию пароль не используется"
    )
    parser.add_argument(
        "--cdn",
        action="store_true",
        help="Загружать статические файлы PyWebIO (CSS, JS) через CDN jsDelivr. По умолчанию используются локальные файлы.",
    )
    parser.add_argument(
        "--run",
        nargs="+",
        type=str,
        help="Запустить при старте указанные конфигурации AzurPilot",
    )
    args, _ = parser.parse_known_args()

    from deploy.language_migration import migrate_deploy_language
    from module.config.utils import UI_LOCALE

    migration = migrate_deploy_language()
    if migration.changed:
        logger.info("[WebUI] Старое значение Language безопасно изменено на ru-RU")

    AlasGUI.set_theme(theme=State.deploy_config.Theme)
    lang.LANG = UI_LOCALE
    key = args.key if is_webui_password_set(args.key) else State.deploy_config.Password
    key, password_error = ensure_public_webui_password(key)
    cdn: str | bool = args.cdn if args.cdn else State.deploy_config.CDN
    runs: List[str] | None = None
    if args.run:
        runs = args.run
    elif State.deploy_config.Run:
        # Старый формат deploy.yaml хранит Run как строку с разделителями-запятыми;
        # сохраняем совместимость до появления списков в конфигурационном reader.
        tmp = State.deploy_config.Run.split(",")
        runs = [item.strip(" ['\"]") for item in tmp if item]
    # Без --run сохраняем None, чтобы менеджер процессов не запускал экземпляры.
    instances: List[str] | None = runs

    logger.hr("[WebUI] Конфигурация WebUI")
    logger.attr("Тема", State.deploy_config.Theme)
    logger.attr("Язык", lang.LANG)
    logger.attr("Пароль задан", is_webui_password_set(key))
    logger.attr("CDN", cdn)
    logger.attr("Облачное устройство", IS_ON_PHONE_CLOUD)

    from deploy.atomic import atomic_failure_cleanup

    atomic_failure_cleanup("./config")
    static_mounts = {
        "/static/assets": str(PROJECT_ROOT / "assets"),
        "/static/doc": str(PROJECT_ROOT / "doc"),
    }

    def _block_public_webui_password_error() -> bool:
        if is_demo_mode() or password_error is None:
            return False
        popup(
            "Защита",
            PUBLIC_WEBUI_PASSWORD_GENERATE_FAILED_MESSAGE,
            implicit_close=False,
            closable=False,
        )
        return True

    def _run_gui(initial_page: str = "home") -> None:
        set_env(title="AzurPilot", output_animation=False)
        load_webui_styles(
            theme=AlasGUI.theme,
            is_mobile=info.user_agent.is_mobile,
            preloaded_styles=("alas",),
        )
        # CSS события загружается до построения меню и контента, чтобы первый кадр
        # не зависел от асинхронной загрузки stylesheet через DOM.
        add_css(filepath_css("event-profiles-alas"))
        add_css(filepath_css("event-general-v2-alas"))
        add_css(filepath_css("event-shop-stability-alas"))
        add_css(filepath_css("traceback-alas"))
        if _block_public_webui_password_error():
            return
        localstorage = None
        if is_webui_password_set(key):
            localstorage = get_localstorage_values(("password", "aside"))
        if is_webui_password_set(key) and not login(
            key, stored_password=localstorage.get("password")
        ):
            logger.warning(f"[WebUI] Неудачная попытка входа с адреса {info.user_ip}")
            time.sleep(1.5)
            run_js("location.reload();")
            return
        gui = AlasGUI()
        local.gui = gui
        gui.run(initial_page=initial_page, localstorage=localstorage)

    @webconfig(css_file=INITIAL_WEBUI_CSS)
    def index() -> None:
        _run_gui()

    @webconfig(css_file=INITIAL_WEBUI_CSS)
    def manage() -> None:
        _run_gui(initial_page="manage")

    from mcp_server_sse import app as mcp_app

    application = asgi_app(
        applications=[index, manage],
        cdn=cdn,
        static_mounts=static_mounts,
        debug=True,
        on_startup=[
            startup,
            lambda: ProcessManager.restart_processes(instances=instances),
        ],
        on_shutdown=[clearup],
    )
    application.mount("/mcp", mcp_app)
    return application
