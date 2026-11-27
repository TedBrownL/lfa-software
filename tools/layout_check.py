"""验证主窗口布局与实时曲线的时间零点对齐。

1. 曲线宽度确实占了窗口的大部分（左栏+配置栏收窄后）；
2. 正式实验时横轴从 0 s 起，且 0 s = 按下「开始实验」那一刻，
   而不是第一个数据点（启动延迟那段空白如实保留）。
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from lfa.main_window import MainWindow
from lfa.plot import LivePlot
from lfa.store import TraceStore

NS = 1_000_000_000
_fail = []


def check(cond, name, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (f"   [{detail}]" if detail else ""))
    if not cond:
        _fail.append(name)


def main() -> int:
    app = QApplication(sys.argv)

    print("[1] 主窗口比例")
    win = MainWindow()
    win.resize(1680, 950)
    win.show()
    app.processEvents()

    w_win = win.width()
    w_plot = win.plot.width()
    frac = w_plot / max(w_win, 1)
    print(f"    窗口 {w_win} px，曲线 {w_plot} px")
    check(frac > 0.60, "曲线占窗口宽度 > 60%", f"{frac:.1%}")
    check(win.plot.height() > 400, "曲线高度足够", f"{win.plot.height()} px")

    # 左右两栏的实际宽度
    lay = win.centralWidget().layout()
    w_left = lay.itemAt(0).widget().width()
    w_cfg = lay.itemAt(1).widget().width()
    print(f"    左栏 {w_left} px，配置栏 {w_cfg} px")
    check(w_left <= 250 and w_cfg <= 260, "两栏已收窄",
          f"{w_left} / {w_cfg}")

    print("[2] 实时横轴：0 s = 时间零点（不是首点）")
    store = TraceStore()
    p = LivePlot(store)
    p.resize(1000, 500)
    p.show()
    ZERO_PERF = 1_700_000_000 * NS          # 按下「开始实验」那一刻
    p.start_session(ZERO_PERF)
    p.set_zero_perf(ZERO_PERF)
    p.set_rel_mode(True)
    p.set_span(None)

    # 首点在零点之后 300 ms（模拟真实的启动延迟）
    delay = int(0.3 * NS)
    n = 6500 * 3
    t_perf = ZERO_PERF + delay + np.arange(n, dtype=np.int64) * (NS // 6500)
    temp = 25.0 + np.linspace(0, 1.0, n)
    for a in range(0, n, 650):
        b = min(a + 650, n)
        store.append_block(t_perf[a:b], t_perf[a:b], temp[a:b])
        p.refresh()

    xs, _ = p._curve.getData()
    check(xs is not None and len(xs), "曲线有数据")
    check(abs(float(xs.min()) - 0.3) < 0.02, "首点位于 0.3 s（= 启动延迟）",
          f"{float(xs.min()):.3f} s")

    (cx0, cx1), _ = p.plot.getViewBox().viewRange()
    check(cx0 <= 1e-9, "视图左边缘 <= 0", f"{cx0:.4f}")
    check(abs(cx1 - float(xs.max())) < 0.05 or cx1 > float(xs.max()),
          "视图右边缘覆盖到最新点", f"{cx1:.2f} vs {float(xs.max()):.2f}")

    ticks = p.axis.tickStrings([0.0, 1.0, 2.0], 1.0, 1.0)
    print(f"    刻度文本 {ticks}")
    check(all(not (":" in s) for s in ticks), "刻度是秒数而不是墙钟")

    print("[3] 墙钟模式仍在（预览采集用）")
    p.set_rel_mode(False)
    ticks2 = p.axis.tickStrings([0.0], 1.0, 1.0)
    print(f"    刻度文本 {ticks2}")
    check(":" in ticks2[0], "切回墙钟后显示时刻")

    win.close()
    app.processEvents()

    print()
    if _fail:
        print(f"失败 {len(_fail)} 项：" + "、".join(_fail))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
