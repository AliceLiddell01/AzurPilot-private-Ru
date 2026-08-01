import copy
import os
import sys
from typing import Optional, Union

from deploy.logger import logger
from deploy.utils import *


LEGACY_IGNORED_KEYS = frozenset(
    {
        "AutoUpdate",
        "CheckUpdateInterval",
        "AutoRestartTime",
        "Repository",
        "Branch",
        "GitExecutable",
        "GitProxy",
        "SSLVerify",
        "GitOverCdn",
    }
)


class ExecutionError(Exception):
    pass


class ConfigModel:
    # Python 配置
    PythonExecutable: str = (
        "./.venv/Scripts/python.exe"
        if sys.platform == "win32"
        else "./.venv/bin/python"
    )
    PypiMirror: Optional[str] = None
    InstallDependencies: bool = True

    # ADB 配置
    AdbExecutable: str = (
        "./.venv/Scripts/adb.exe"
        if sys.platform == "win32"
        else "./.venv/bin/adb"
    )
    ReplaceAdb: bool = True
    AutoConnect: bool = True
    InstallUiautomator2: bool = True

    # OCR 配置
    UseOcrServer: bool = False
    StartOcrServer: bool = False
    OcrServerPort: int = 22268
    OcrClientAddress: str = "127.0.0.1:22268"

    # WebUI supervisor 配置
    EnableReload: bool = True

    # 杂项
    DiscordRichPresence: bool = False

    # 远程访问
    EnableRemoteAccess: bool = False
    RemoteAccessMode: str = "auto"
    SSHUser: Optional[str] = None
    SSHServer: Optional[str] = None
    SSHExecutable: Optional[str] = None
    SignalingServer: Optional[str] = None
    StunServers: Optional[str] = '["stun:stun.l.google.com:19302"]'
    TurnServers: Optional[str] = None
    TurnCredentialMode: str = "static"

    # WebUI 配置
    WebuiHost: str = "0.0.0.0"
    WebuiPort: int = 25548
    WebuiSSLKey: Optional[str] = None
    WebuiSSLCert: Optional[str] = None
    Language: str = "ru-RU"
    Theme: str = "default"
    DpiScaling: bool = True
    Password: Optional[str] = None
    CDN: Union[str, bool] = False
    Run: Optional[str] = None


class DeployConfig(ConfigModel):
    def __init__(self, file=DEPLOY_CONFIG, template_file=None):
        """初始化部署配置，不执行网络请求或隐式写入。"""
        self.file = file
        self.template_file = template_file or get_deploy_template()
        self.config = {}
        self.config_template = {}
        self.read()
        self.show_config()

    def show_config(self):
        logger.hr("Show deploy config", 1)
        hidden = {"Password", "SSHUser"} | LEGACY_IGNORED_KEYS
        for key, value in self.config.items():
            if key in hidden:
                continue
            if self.config_template.get(key) == value:
                continue
            logger.info(f"{key}: {value}")

        logger.info("Rest of the configs are the same as default")

    def read(self):
        """Load defaults and user values without network or file writes."""
        template = poor_yaml_read(self.template_file)
        self.config_template = copy.deepcopy(template)
        origin = poor_yaml_read(self.file)

        self.config = template
        self.config.update(origin)

        for key, value in self.config.items():
            if key in LEGACY_IGNORED_KEYS:
                continue
            if hasattr(self, key):
                super().__setattr__(key, value)

    def write(self, keys=None):
        """Persist explicit settings while preserving the existing user file."""
        poor_yaml_write(
            self.config,
            self.file,
            template_file=self.template_file,
            preserve_existing=True,
            keys=keys,
        )

    def filepath(self, key):
        return (
            os.path.abspath(os.path.join(self.root_filepath, self.config[key]))
            .replace(r"\\", "/")
            .replace("\\", "/")
            .replace('"', '"')
        )

    @cached_property
    def root_filepath(self):
        return (
            os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
            .replace(r"\\", "/")
            .replace("\\", "/")
            .replace('"', '"')
        )

    def execute(self, command, allow_failure=False, output=True):
        command = command.replace(r"\\", "/").replace("\\", "/").replace('"', '"')
        if not output:
            command = command + ' >nul 2>nul'
        logger.info(command)
        error_code = os.system(command)
        if error_code:
            if allow_failure:
                logger.info(f"[ allowed failure ], error_code: {error_code}")
                return False
            logger.info(f"[ failure ], error_code: {error_code}")
            self.show_error(command)
            raise ExecutionError
        logger.info("[ success ]")
        return True

    def show_error(self, command=None):
        logger.hr("Operation failed", 0)
        self.show_config()
        logger.info("")
        logger.info(f"Last command: {command}")
        logger.info(
            "Please check your deploy settings in config/deploy.yaml "
            "and re-open AzurPilot.exe"
        )
        logger.info("Take the screenshot of entire window if you need help")
