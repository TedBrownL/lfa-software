"""端到端实测：真实设备 + 真实主窗口，同时测"界面帧节奏"和"采集空档"。

测什么
------
1. **界面实际刷新节奏**：记录每次真正 ``plot.refresh()`` 的时刻，算间隔分布。
   只看 QTimer 的设定值没有意义，要看它实际被触发得多快、均匀不均匀。
2. **采集有没有被界面拖住**：看采集时间戳里 >1 ms 的空档次数与累计时长。
   这是"采集与界面分离"是否生效的唯一硬指标。
3. **画面是不是"两帧才动一次"**：统计相邻两次 refresh 之间新增点数是否为 0。
   若频繁为 0，说明推送粒度 > 刷新间隔，视觉上就会一跳一跳。

用法：
    QT_QPA_PLATFORM=offscreen python tools/bench_ui_frames.py [秒数]
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lfa.acquisition import AcquisitionConfig
from lfa.main_window import MainWindow


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()

    frames: list[float] = []
    empty_frames = [0]
    last_n = [0]

    orig_refresh = win.plot.refresh

    def timed_refresh():
        orig_refresh()
        frames.append(time.perf_counter())
        n = len(win.store)
        if n == last_n[0]:
            empty_frames[0] += 1
        last_n[0] = n

    win.plot.refresh = timed_refresh
    # 平均时间 100 ms 时 _ensure_signal_ready 会弹模态框卡住无人值守的测试
    win._ensure_signal_ready = lambda: True

    def start():
        idx = win.cb_port.findData("COM10")
        if idx >= 0:
            win.cb_port.setCurrentIndex(idx)
        win._start()

    def finish():
        rate = win.lbl_rate.text()
        win._stop()
        n = len(win.store)
        t = win.store.t_perf_all
        print(f"\n采集: {n:,} 点  {rate}")
        if n > 100:
            dt = np.diff(t) / 1e3
            big = dt > 1000
            dur = (t[-1] - t[0]) / 1e9
            print(f"  实际速率 {n / max(dur, 1e-9):,.0f} Hz   "
                  f"dt 中位 {np.median(dt):.0f} us")
            print(f"  >1 ms 空档 {big.sum()} 次   累计 {dt[big].sum() / 1000:.1f} ms   "
                  f"最大 {dt.max() / 1000:.2f} ms")

        f = np.array(frames)
        if f.size > 10:
            iv = np.diff(f) * 1000
            # 首帧要等窗口首次绘制，必然是异常值，单独排除后再统计
            body = iv[1:] if iv.size > 1 else iv
            print(f"\n界面: {f.size} 次重绘")
            print(f"  间隔 中位 {np.median(body):.1f} ms "
                  f"(={1000 / np.median(body):.1f} Hz)  "
                  f"p95 {np.percentile(body, 95):.1f} ms  "
                  f"max {body.max():.1f} ms  sd {body.std():.1f} ms")
            print(f"  首帧 {iv[0]:.0f} ms（窗口首次绘制，不计入统计）")
            print(f"  新数据为空的重绘: {empty_frames[0]}/{f.size} "
                  f"({100 * empty_frames[0] / f.size:.0f}%)")
        app.quit()

    QTimer.singleShot(2000, start)
    QTimer.singleShot(int(2000 + seconds * 1000), finish)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
