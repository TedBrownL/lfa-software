"""验证：采集放进独立进程后，是否不再被 GUI 重绘阻塞。

背景：PySide6 + pyqtgraph 的重绘在事件循环中执行并持有 GIL，会把同进程内的
采集线程卡住 2-3 ms/帧（实测 62 Hz 下有 400+ 次、累计 2.4 s 的停顿）。
本脚本把采集放到独立进程，用 Queue 传数据，验证停顿是否消失。

用法：python tools/bench_multiprocess.py
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lfa.optris import OptrisCT4M
from lfa.plot import LivePlot
from lfa.store import TraceStore

NS = 1_000_000_000
PORT = "COM10"


def worker(q, stop_evt):
    """子进程：纯串口采集，不碰任何 Qt 对象。"""
    d = OptrisCT4M(PORT)
    d.open()
    buf = []
    try:
        while not stop_evt.is_set():
            tw = time.perf_counter_ns()
            raw = d.read_temperature_word()
            if len(raw) == 2:
                word = (raw[0] << 8) | raw[1]
                buf.append((tw, (word - 1000) / 10.0))
            if len(buf) >= 200:
                q.put(buf)
                buf = []
        if buf:
            q.put(buf)
    finally:
        d.close()


def main() -> int:
    d = OptrisCT4M(PORT)
    d.open()
    d.set_averaging_time_ms(1)
    d.close()

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    stop = ctx.Event()
    proc = ctx.Process(target=worker, args=(q, stop), daemon=True)
    proc.start()

    app = QApplication(sys.argv)
    store = TraceStore()
    plot = LivePlot(store)
    plot.resize(1200, 600)
    plot.show()
    plot.start_session(1_700_000_000 * NS)
    plot.set_span(10.0)

    tm = QTimer()

    def drain():
        while not q.empty():
            buf = q.get()
            t = np.array([a for a, _ in buf], dtype=np.int64)
            v = np.array([b for _, b in buf], dtype=np.float64)
            if store.is_empty:
                plot.start_session(int(t[0] + 1_700_000_000 * NS))
            store.append_block(t, t + 1_700_000_000 * NS, v)
        plot.refresh()

    tm.timeout.connect(drain)
    tm.setInterval(16)
    tm.start()

    t_end = time.perf_counter() + 6.0
    while time.perf_counter() < t_end:
        app.processEvents()
        time.sleep(0.001)

    stop.set()
    proc.join(3.0)
    if proc.is_alive():
        proc.terminate()

    t = store.t_perf_all
    if len(t) < 100:
        print("数据不足")
        return 1
    dt = np.diff(t) / 1e3
    big = dt > 1000
    print(f"【独立进程采集 + 16ms 重绘】")
    print(f"  {len(t):,} 点 / {(t[-1]-t[0])/1e9:.1f} s = "
          f"{len(t)/((t[-1]-t[0])/1e9):,.0f} Hz")
    print(f"  停顿(>1ms) {big.sum()} 个  累计 {dt[big].sum()/1000:.0f} ms  "
          f"max {dt.max()/1000:.1f} ms")
    print(f"  dt 中位 {np.median(dt):.0f} us")
    app.quit()

    d = OptrisCT4M(PORT)
    d.open()
    d.set_averaging_time_ms(100)
    d.close()
    print("  设备已恢复 100 ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
