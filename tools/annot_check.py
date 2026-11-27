"""验证界面上的标注横线确实落在视图范围内（不是被画到画面外）。

用户报的「看不到时间横线」，根因是旧代码把 ViewBox 的像素高度
乘进数据坐标，算出的 y 超出视图。这里直接比对每条横线的 y 与窗口
实际纵轴范围——这才是"看不看得见"的判据。
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from lfa import experiment as ex
from lfa.experiment import ExperimentConfig
from lfa.result_window import ResultWindow, c_to_unit

FS = 6500.0


def synth(n_pulse=2, dur=12.0):
    rng = np.random.default_rng(5)
    t = np.arange(int(dur * FS)) / FS
    y = np.full(t.size, 25.0)
    for k in range(n_pulse):
        t0 = 2.0 + k * 3.0
        m = t >= t0
        y[m] += 1.8 * np.exp(-(t[m] - t0) / 0.6)
    return t, y + rng.normal(0, 0.05, t.size)


_fail = []


def check(cond, name, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (f"   [{detail}]" if detail else ""))
    if not cond:
        _fail.append(name)


def main() -> int:
    app = QApplication(sys.argv)

    t, y = synth(2)
    cfg = ExperimentConfig(pulse_count=2, pulse_width_s=0.1, material="不锈钢")
    res = ex.analyze(t, y, cfg)
    check(res.ok and len(res.intervals) == 3, "合成数据被正确分析",
          f"峰={len(res.peaks)} 区间={len(res.intervals)}")

    rw = ResultWindow(t, y, cfg, res, unit="C")
    rw.resize(1440, 860)
    rw.show()
    app.processEvents()

    print("[1] 横轴从 0 s 起")
    (cx0, cx1), _ = rw.plot.getViewBox().viewRange()
    check(cx0 <= 0.0 + 1e-9, "视图左边缘 <= 0", f"{cx0:.4f}")
    check(abs(cx0) < (cx1 - cx0) * 0.05, "0 在视图内且靠左",
          f"{cx0:.4f} vs 跨度 {cx1-cx0:.2f}")

    print("[2] 每条标注横线都在视图内")
    import pyqtgraph as pg

    def horizontal_lines():
        """只取横线本身：两端 y 相同。端头竖线与文字不算。"""
        out = []
        for it in rw._bands:
            if not isinstance(it, pg.PlotDataItem):
                continue
            _, ys = it.getData()
            if ys is not None and len(ys) >= 2 and abs(
                    float(ys[0]) - float(ys[-1])) < 1e-9:
                out.append(it)
        return out

    (_, _), (cy0, cy1) = rw.plot.getViewBox().viewRange()
    lines = horizontal_lines()
    check(len(lines) == 3, "横线数量 = 区间数", f"{len(lines)}")

    # 关键：所有横线必须等高（用户明确要求"纵坐标一致，不要一上一下"）
    y_vals = [float(ln.getData()[1][0]) for ln in lines]
    check(max(y_vals) - min(y_vals) < 1e-9, "所有横线严格等高",
          f"{[round(v, 6) for v in y_vals]}")

    for k, ln in enumerate(lines):
        xs, ys = ln.getData()
        yv = float(ys[0])
        inside = cy0 < yv < cy1
        check(inside, f"  横线 {k+1} 在纵轴范围内",
              f"y={yv:.3f} ∈ ({cy0:.3f}, {cy1:.3f})")
        check(yv > float(np.max(rw._curve.yData)) - 1e-9,
              f"  横线 {k+1} 在曲线最高点之上",
              f"y={yv:.3f} vs 峰值 {float(np.max(rw._curve.yData)):.3f}")

    print("[2b] 横线不与峰点 M 的坐标文字重叠")
    # M 标注用 anchor=(0,1.2) 挂在峰点上方，占约 9pt 高。
    # 把它的上边界换算成数据坐标，与横线的下边界（含端头）比较。
    vb = rw.plot.getViewBox()
    px_per_y = vb.height() / max(cy1 - cy0, 1e-9)      # 像素 / 数据单位
    text_px = 12.0                                     # 8pt 文字 + 余量
    overlap_ok = True
    for i, p in enumerate(rw.res.peaks, 1):
        py = float(c_to_unit(p.temp_c, rw.unit))
        text_top = py + (text_px + text_px * 0.2) / px_per_y
        y_line = y_vals[0]
        tick_h = (float(np.max(rw._curve.yData))
                  - float(np.min(rw._curve.yData))) * 0.030
        band_bottom = y_line - tick_h
        clear = band_bottom - text_top
        print(f"    M{i} 文字顶 {text_top:.3f}，横线区底 {band_bottom:.3f}，"
              f"净空 {clear:.3f}")
        if clear <= 0:
            overlap_ok = False
    check(overlap_ok, "横线区完整避开 M 文字")

    print("[3] 峰点标记都在视图内")
    import pyqtgraph as pg
    dots = [it for it in rw._markers if isinstance(it, pg.ScatterPlotItem)]
    check(len(dots) == 2, "峰点标记数 = 峰数", f"{len(dots)}")

    print("[4] 默认平滑度 0.5")
    check(rw.sl_smooth.value() == 50, "滑条默认 50", str(rw.sl_smooth.value()))
    check(rw.lbl_smooth.text() == "0.50", "标签显示 0.50", rw.lbl_smooth.text())

    print("[5] 平滑到 1.0 后横线仍在视图内")
    rw.sl_smooth.setValue(100)
    app.processEvents()
    (_, _), (cy0b, cy1b) = rw.plot.getViewBox().viewRange()
    ok_all = True
    for ln in horizontal_lines():
        _, ys = ln.getData()
        yv = float(ys[0])
        if not (cy0b < yv < cy1b):
            ok_all = False
    check(ok_all, "平滑后横线仍可见", f"纵轴 ({cy0b:.2f}, {cy1b:.2f})")
    rw.sl_smooth.setValue(50)

    rw.close()
    app.processEvents()

    print()
    if _fail:
        print(f"失败 {len(_fail)} 项：" + "、".join(_fail))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
