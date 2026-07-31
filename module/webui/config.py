"""
Web界面部署配置管理。

提供 DeployConfig 的 WebUI 子类。配置读取无副作用；只有显式设置公开字段时，
才会将该字段写回部署文件。
"""

from deploy.config import DeployConfig as _DeployConfig


class DeployConfig(_DeployConfig):
    def show_config(self):
        pass

    def __setattr__(self, key: str, value):
        """Persist one explicit public setting without rewriting the file."""
        super().__setattr__(key, value)
        config = self.__dict__.get("config")
        if (
            config is not None
            and key
            and key[0].isupper()
            and key in config
            and config[key] != value
        ):
            config[key] = value
            self.write(keys={key})
