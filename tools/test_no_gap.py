"""回归测试：实时曲线不得在左侧出现空白。

用户报的现象："左侧点消失比右侧点更新快，左侧出现空白"。
根因：数据按"目标窗口起点"截取，而视图只在偏离 15% 时才 setXRange，
导致视图左边缘停在旧位置、与数据起点之间出现空白（实测最大 1.4 s）。

本测试在真实刷新循环下，持续检查"视图左边缘"与"曲线最左数据点"的差值。
用法：QT_QPA_PLATFORM=offscreen python tools/test_no_gap.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lfa.plot import LivePlot
from lfa.store import TraceStore

NS = 1_000_000_000
RATE = 6500
STEP = 650          # 每帧灌入 0.1 s 数据


def run_case(span: float | None, total_s: float = 25.0) -> tuple[float, float, int]:
    """返回 (最大左空白, 最大右空白, 检查次数)。"""
    store = TraceStore()
    p = LivePlot(store)
    p.resize(1200, 600)
    p.show()
    p.start_session(1_700_000_000 * NS)
    p.set_span(span)
    p.follow = True

    n = int(total_s * RATE)
    t = np.arange(n, dtype=np.int64) * (NS // RATE)
    temp = 25.0 + np.sin(t / NS) * 0.1

    gap_left = 0.0
    gap_right = 0.0
    checks = 0
    for k in range(1, n // STEP + 1):
        a, b = (k - 1) * STEP, k * STEP
        store.append_block(t[a:b], t[a:b] + 1_700_000_000 * NS, temp[a:b])
        p.refresh()
        xs, _ = p._curve.getData()
        if xs is None or not len(xs):
            continue
        (cx0, cx1), _ = p.plot.getViewBox().viewRange()
        gap_left = max(gap_left, max(0.0, float(xs.min()) - cx0))
        gap_right = max(gap_right, max(0.0, cx1 - float(xs.max())))
        checks += 1
    return gap_left, gap_right, checks


def main() -> int:
    app = QApplication(sys.argv)
    # 空白不超过一个像素列对应的时长即可（1200 px 宽、10 s 跨度 -> 8.3 ms/px）
    LIMIT_S = 0.010

    ok = True
    for label, span in (("10 s 跨度", 10.0), ("30 s 跨度", 30.0),
                        ("60 s 跨度", 60.0), ("全览模式", None)):
        gl, gr, n = run_case(span)
        good = gl <= LIMIT_S
        ok &= good
        print(f"{label:10s} 检查 {n:4d} 次  左空白 max {gl*1000:6.2f} ms "
              f"右空白 max {gr*1000:6.2f} ms  {'OK' if good else 'FAIL'}")

    print("全部通过" if ok else "存在左侧空白 —— 未通过")
    app.quit()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
