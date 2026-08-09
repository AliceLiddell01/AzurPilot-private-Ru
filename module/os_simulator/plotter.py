"""大世界模拟器可视化模块。

提供大世界模拟器的图表绘制功能，包括单样本轨迹图、
多轮模拟的行动力/代币/完成海域数分布直方图以及
综合报告图，使用 matplotlib 生成并保存为 PNG 图像。
"""
import os
import numpy as np
import matplotlib
from datetime import datetime

from module.os_simulator.constants import *

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

plt.rcParams['font.sans-serif'] = [
    'Microsoft YaHei', 'SimHei',  # Windows
    'PingFang SC', 'STHeiti', 'Heiti SC',  # macOS
    'Noto Sans CJK SC', 'Source Han Sans SC', 'WenQuanYi Micro Hei',  # Linux
    'Droid Sans Fallback'  # Fallback
]
plt.rcParams['axes.unicode_minus'] = False

class OSSimulatorPlotter:
    def __init__(self, logger):
        self.logger = logger
        self.result_figure_path = ''

    def plot_single_sample_history(self, history_single):
        """
        绘制单样本轨迹图。
        """
        self.logger.info("[Симулятор Операции «Сирена»] Создание траектории для одной выборки...")
        
        fig, ax1 = plt.subplots(figsize=(18, 6))
        
        times = np.array(history_single['time']) / 86400
        aps = history_single['ap']
        coins = history_single['coin']
        statuses = history_single['status']

        ax1.plot(times, aps, color='blue', label='Очки действия', linewidth=1.5)
        ax1.set_xlabel('Время (дни)')
        ax1.set_ylabel('Очки действия', color='blue')
        ax1.tick_params(axis='y', labelcolor='blue')

        ax2 = ax1.twinx()
        ax2.plot(times, coins, color='gold', label='Жёлтые монеты', linewidth=1.5)
        ax2.set_ylabel('Жёлтые монеты', color='gold')
        ax2.tick_params(axis='y', labelcolor='gold')

        if len(times) > 1:
            start_t = times[0]
            current_s = statuses[1]
            for i in range(1, len(times)):
                if statuses[i] != current_s:
                    end_t = times[i-1]
                    if current_s == STATUS_CL1:
                        ax1.axvspan(start_t, end_t, facecolor='green', alpha=0.15)
                    elif current_s == STATUS_MEOW:
                        ax1.axvspan(start_t, end_t, facecolor='orange', alpha=0.15)
                    elif current_s == STATUS_CRASHED:
                        ax1.axvspan(start_t, end_t, facecolor='red', alpha=0.15)
                    start_t = end_t
                    current_s = statuses[i]
            
            if current_s == STATUS_CL1:
                ax1.axvspan(start_t, times[-1], facecolor='green', alpha=0.15)
            elif current_s == STATUS_MEOW:
                ax1.axvspan(start_t, times[-1], facecolor='orange', alpha=0.15)
            elif current_s == STATUS_CRASHED:
                ax1.axvspan(start_t, times[-1], facecolor='red', alpha=0.15)
                
        cl1_patch = mpatches.Patch(color='green', alpha=0.15, label='Уровень коррозии 1')
        meow_patch = mpatches.Patch(color='orange', alpha=0.15, label='Фарм мяуфицеров')
        crash_patch = mpatches.Patch(color='red', alpha=0.15, label='Сбой')
        
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2 + [cl1_patch, meow_patch, crash_patch], 
                labels1 + labels2 + ['Уровень коррозии 1', 'Фарм мяуфицеров', 'Сбой'],
                loc='upper left')

        plt.title('Симулятор Operation Siren: траектория одного прогона')
        plt.tight_layout()

        self._save(fig, 'single_sample')

    def plot_multi_sample_history(self, result, history_multi_avg):
        """
        多样本模式：绘制所有样本的平均值和标准差随时间变化的轨迹图。
        """
        self.logger.info("[Симулятор Операции «Сирена»] Создание усреднённой траектории для нескольких выборок...")
        
        fig, ax1 = plt.subplots(figsize=(18, 6))
        
        times = history_multi_avg['time'] / 86400
        mean_ap = history_multi_avg['ap']
        std_ap = history_multi_avg['ap_std']
        mean_coin = history_multi_avg['coin']
        std_coin = history_multi_avg['coin_std']
        mean_crash = history_multi_avg['crash'] * 100.0  # 转为百分比

        ax1.plot(times, mean_ap, color='blue', label='Средние очки действия', linewidth=2)
        ax1.fill_between(times, mean_ap - std_ap, mean_ap + std_ap, color='blue', alpha=0.2)
        ax1.set_xlabel('Время (дни)')
        ax1.set_ylabel('Очки действия', color='blue')
        ax1.tick_params(axis='y', labelcolor='blue')

        ax2 = ax1.twinx()
        ax2.plot(times, mean_coin, color='gold', label='Среднее число жёлтых монет', linewidth=2)
        ax2.fill_between(times, mean_coin - std_coin, mean_coin + std_coin, color='gold', alpha=0.2)
        ax2.set_ylabel('Жёлтые монеты', color='gold')
        ax2.tick_params(axis='y', labelcolor='gold')

        ax3 = ax1.twinx()
        ax3.spines['right'].set_position(('outward', 60))
        ax3.plot(times, mean_crash, color='red', label='Накопленная вероятность сбоя', linewidth=2)
        ax3.fill_between(times, 0, mean_crash, color='red', alpha=0.1)
        ax3.set_ylabel('Накопленная вероятность (%)', color='red')
        ax3.tick_params(axis='y', labelcolor='red')
        ax3.set_ylim(-5, 105)

        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        lines3, labels3 = ax3.get_legend_handles_labels()
        ax1.legend(lines1 + lines2 + lines3, labels1 + labels2 + labels3, loc='upper left')

        plt.title('Симулятор Operation Siren: средняя траектория и вероятность сбоя')
        fig.tight_layout()
        fig.subplots_adjust(right=0.88)
        
        self._save(fig, 'multi_sample')

    def _save(self, fig, name):
        os.makedirs('./log/oss/figures', exist_ok=True)
        self.result_figure_path = f'./log/oss/figures/{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}_{name}.png'
        plt.savefig(self.result_figure_path)
        self.logger.info(f"[Симулятор Операции «Сирена»] График сохранён: {self.result_figure_path}")
        plt.close()
        return self.result_figure_path
