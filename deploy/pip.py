import sys

from deploy.config import DeployConfig, ExecutionError
from deploy.logger import logger
from deploy.uv import command_output, log_command_output, sync_project_venv, venv_python
from deploy.utils import cached_property


class PipManager(DeployConfig):
    @cached_property
    def python(self) -> str:
        python = venv_python()
        if python.exists():
            return str(python).replace("\\", "/")
        return sys.executable.replace("\\", "/")

    def pip_install(self):
        logger.hr("Обновление зависимостей", 0)
        if not self.InstallDependencies:
            logger.info("InstallDependencies отключён, обновление пропущено")
            return

        try:
            result = sync_project_venv(capture_output=True)
        except Exception as exc:
            logger.critical(f"Не удалось выполнить uv sync: {exc}")
            log_command_output(logger, command_output(exc))
            raise ExecutionError from exc
        else:
            log_command_output(logger, result.output)
