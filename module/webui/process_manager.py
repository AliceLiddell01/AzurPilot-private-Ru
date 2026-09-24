"""Узкая compatibility-прокладка к нейтральному Bot Runtime client."""

from module.application.bot_runtime_client import BotRuntimeClient

# Старое имя оставлено для необновлённых внешних расширений. Класс не владеет
# процессами и не запускает backend локально.
ProcessManager = BotRuntimeClient

__all__ = ["BotRuntimeClient", "ProcessManager"]
