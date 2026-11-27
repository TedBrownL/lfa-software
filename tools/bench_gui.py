"""采样率对照实验：定位 GUI 下采样率下降的环节。

在真实事件循环中依次测量三种配置下的采集速率：
  A 全部开启（绘图 + 读数 + 时钟）
  B 只关绘图刷新
  C 全部关闭（等价于纯采集）

用法：QT_QPA_PLATFORM=offscreen python tools/bench_gui.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lfa.main_window import MainWindow

DUR_MS = 4000


def report(label, win) -> str:
    t = win.store.t_perf_all
    if t.size < 10:
        return f"{label:28s} 无数据"
    dt = np.diff(t) / 1e3
    rate = len(t) / ((t[-1] - t[0]) / 1e9)
    return (f"{label:28s} n={len(t):6d} rate={rate:7.0f} Hz "
            f"med={np.median(dt):5.1f} p99={np.percentile(dt, 99):6.1f} "
            f"max={dt.max():7.1f} us")


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    i = win.cb_port.findData("COM10")
    if i >= 0:
        win.cb_port.setCurrentIndex(i)

    results = []
    cases = [
        ("A 绘图+读数+时钟", False, False, False),
        ("B 仅关绘图", True, False, False),
        ("C 全部关闭(纯采集)", True, True, True),
    ]
    state = {"idx": 0}

    def run_case():
        if state["idx"] >= len(cases):
            print("\n".join(results), flush=True)
            app.quit()
            return
        label, no_plot, no_clock, no_ui = cases[state["idx"]]
        win.store.clear()
        if no_plot:
            win._ui_timer.stop()
        else:
            win._ui_timer.start()
        if no_clock:
            win._clock_timer.stop()
        else:
            win._clock_timer.start()
        if no_ui:
            # 连读数面板也不更新：用空实现的定时器覆盖
            win._ui_timer.stop()
        win._start()
        QTimer.singleShot(DUR_MS, lambda lb=label: end_case(lb))

    def end_case(label):
        results.append(report(label, win))
        win._stop()
        state["idx"] += 1
        QTimer.singleShot(800, run_case)

    QTimer.singleShot(400, run_case)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
