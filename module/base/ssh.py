"""SSH 客户端公共工具。"""

from pathlib import Path
from subprocess import DEVNULL, PIPE, run

from module.logger import logger


def clear_ssh_host_key(host: str, port: int) -> bool:
    """仅删除本次连接目标的 SSH 主机指纹。"""
    host = str(host or "").rsplit("@", 1)[-1].strip("[]")
    if not host:
        return False

    try:
        port = int(port)
    except (TypeError, ValueError):
        logger.warning(f"SSH 端口无效，跳过清理主机指纹：{host}:{port}")
        return False

    targets = [f"[{host}]:{port}"]
    if port == 22:
        targets.insert(0, host)

    known_hosts = Path.home() / ".ssh" / "known_hosts"
    removed = False
    for target in targets:
        try:
            result = run(
                ["ssh-keygen", "-R", target, "-f", str(known_hosts)],
                stdin=DEVNULL,
                stdout=PIPE,
                stderr=PIPE,
                check=False,
                text=True,
            )
        except FileNotFoundError:
            logger.warning(f"找不到 ssh-keygen，无法清理 SSH 主机指纹：{target}")
            return removed

        if result.returncode == 0:
            logger.info(f"已清理 SSH 主机指纹：{target}（{known_hosts}）")
            removed = True
        elif result.returncode != 1:
            logger.warning(f"清理 SSH 主机指纹失败：{target}，{result.stderr.strip()}")

    return removed
