"""推送通知（Push Notification）模块。

通过 onepush 库将任务执行结果推送到外部渠道（QQ、微信、Telegram 等）。
支持 YAML 格式的通知配置和 WebUI 本地推送。

主要函数：
    - handle_notify(): 解析 YAML 配置并通过指定渠道发送推送通知。
    - notify_webui(): 向本地 WebUI 服务发送 HTTP POST 通知。
"""

import onepush.core
import yaml
from onepush import get_notifier
from onepush.core import Provider
from onepush.exceptions import OnePushException
from onepush.providers.custom import Custom
from requests import Response

from module.logger import logger

onepush.core.log = logger


def handle_notify(_config: str, **kwargs) -> bool:
    """处理推送通知请求。

    解析 YAML 格式的配置，选择通知渠道（如 QQ、微信等），
    并通过 onepush 库发送通知消息。

    Args:
        _config: YAML 格式的通知配置字符串，包含 provider 和渠道参数。
        **kwargs: 附加的通知参数，如 title、content 等。

    Returns:
        通知发送成功返回 True，失败返回 False。
    """
    try:
        config = {}
        for item in yaml.safe_load_all(_config):
            config.update(item)
    except Exception:
        logger.error("Не удалось загрузить конфигурацию onepush; отправка пропущена")
        return False
    try:
        provider_name: str = config.pop("provider", None)
        if provider_name is None:
            logger.info("Провайдер push-уведомлений не указан; отправка пропущена")
            return False
        notifier: Provider = get_notifier(provider_name)
        required: list[str] = notifier.params["required"]
        config.update(kwargs)

        # 参数预检查
        for key in required:
            if key not in config:
                logger.warning(
                    f"[Уведомления] У канала {notifier.name} отсутствует обязательный параметр '{key}'"
                )

        if isinstance(notifier, Custom):
            if "method" not in config or config["method"] == "post":
                config["datatype"] = "json"
            if not ("data" in config or isinstance(config["data"], dict)):
                config["data"] = {}
            if "title" in kwargs:
                config["data"]["title"] = kwargs["title"]
            if "content" in kwargs:
                config["data"]["content"] = kwargs["content"]
                if "data" in config and "message" in config["data"] and '${content}' in config["data"]["message"]:
                    config["data"]["message"] = config["data"]["message"].replace("${content}", config["data"]["content"])
                    
        if provider_name.lower() == "gocqhttp":
            access_token = config.get("access_token")
            if access_token:
                config["token"] = access_token

        resp = notifier.notify(**config)
        if isinstance(resp, Response):
            if resp.status_code != 200:
                logger.warning("Не удалось отправить push-уведомление!")
                logger.warning(f"[Уведомления] HTTP-код состояния: {resp.status_code}")
                return False
            else:
                if provider_name.lower() == "gocqhttp":
                    return_data: dict = resp.json()
                    if return_data["status"] == "failed":
                        logger.warning("Не удалось отправить push-уведомление!")
                        logger.warning(
                            f"Ответ сервера:{return_data['wording']}")
                        return False
    except OnePushException:
        logger.error("Не удалось отправить push-уведомление")
        return False
    except Exception as e:
        # 不打印完整异常栈，避免暴露变量信息
        logger.error(e)
        return False

    logger.info("Push-уведомление отправлено успешно")
    return True


def notify_webui(instance: str, title: str, content: str, **kwargs) -> bool:
    """推送通知到 WebUI 本地端口，供启动器接收。

    向本地 WebUI 服务发送 HTTP POST 请求，传递实例名、标题和内容。
    默认端口为 25548，可通过配置自定义。

    Args:
        instance: 触发通知的实例名称。
        title: 通知标题。
        content: 通知正文内容。
        **kwargs: 其他附加字段，合并到请求体中。

    Returns:
        推送成功返回 True，失败返回 False。
    """
    try:
        from module.webui.setting import State
        port = int(State.deploy_config.WebuiPort) or 25548
    except Exception:
        port = 25548
    try:
        import requests
        payload = {"instance": instance, "title": title, "content": content}
        payload.update(kwargs)
        requests.post(
            f"http://127.0.0.1:{port}/api/notify",
            json=payload,
            timeout=2,
        )
        return True
    except Exception:
        return False
