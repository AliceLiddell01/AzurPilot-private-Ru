"""Модуль задачи этапа SP совместных операций (Coalition Event).

Инкапсулирует логику планирования задач в режиме SP совместных операций.
Вызывает базовый класс Coalition для проведения одного боя на сложности SP,
после чего в зависимости от результата планирует следующее выполнение задачи или останавливает её.
"""

from module.coalition.coalition import Coalition
from module.config.config import TaskEnd


class CoalitionSP(Coalition):
    def run(self, *args, **kwargs):
        try:
            super().run(mode='sp', total=1)
        except TaskEnd:
            # Catch task switch
            pass
        if self.run_count > 0:
            self.config.task_delay(server_update=True)
        else:
            self.config.task_stop()
