"""实时温度曲线控件（pyqtgraph）。

横轴需求：显示"当前实际精确时间（到 ms）"。实现上有两个坑，这里都处理了：

1. pyqtgraph 的 AxisItem 默认按数值格式化成科学计数法，直接放 epoch 秒会因为
   数值太大而刻度稀疏、难读。因此内部 x 用"相对会话起点的秒数"，
   刻度由 :class:`WallClockAxis` 换算成 ``HH:MM:SS.mmm``。
2. 轴在缩放/平移时需要知道"当前视图对应哪个墙钟时刻"。会话起点墙钟时间由
   外部在开始采集时注入，之后不再变化（单调时钟与墙钟已绑定，见 timebase）。

显示策略：
* 跟随模式：视图窗口固定时长（如 10 s / 30 s / 60 s），右端贴住最新样本；
* 全览模式：显示整段会话，随采集自动扩张；
* 拖拽/滚轮后自动退出跟随，按 F 或点按钮回到跟随。
"""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .store import TraceStore, envelope
from .timebase import NS


class WallClockAxis(pg.AxisItem):
    """横轴刻度：墙钟模式显示绝对时刻，相对模式显示「从 0 s 起」的秒数。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session_wall_ns = 0
        self._rel_mode = False

    def set_session_start(self, wall_ns: int) -> None:
        self._session_wall_ns = int(wall_ns)
        self.picture = None
        self.update()

    def set_rel_mode(self, on: bool) -> None:
        """切到相对模式：横轴直接读"距离时间零点多少秒"。"""
        self._rel_mode = bool(on)
        self.picture = None
        self.update()

    def tickStrings(self, values, scale, spacing):                       # noqa: N802
        if self._rel_mode:
            # 小数位随刻度间隔自适应，避免出现 1.0000 这种冗余位数
            dec = 0 if spacing >= 1.0 else min(3, int(np.ceil(-np.log10(spacing))) + 1)
            return [f"{v:.{dec}f}" for v in values]
        if self._session_wall_ns == 0:
            return [f"{v:.3f}" for v in values]
        # 刻度间隔 >= 1 s 时省掉毫秒，读数更干净；否则保留毫秒
        show_ms = spacing < 1.0
        out = []
        for v in values:
            wall = self._session_wall_ns + int(round(v * NS))
            sec, nsec = divmod(wall, NS)
            tm = time.localtime(sec)
            stamp = f"{tm.tm_hour:02d}:{tm.tm_min:02d}:{tm.tm_sec:02d}"
            if show_ms:
                stamp += f".{nsec // 1_000_000:03d}"
            out.append(stamp)
        return out

    def tickValues(self, minVal, maxVal, size):                          # noqa: N802
        """让刻度落在"整"毫秒/秒上，而不是浮点乱数。"""
        vals = super().tickValues(minVal, maxVal, size)
        if self._rel_mode or self._session_wall_ns == 0:
            return vals
        refined = []
        for spacing, ticks in vals:
            if spacing <= 0:
                refined.append((spacing, ticks))
                continue
            # 对齐到 1 ms 的整数倍
            q = np.round(np.asarray(ticks) * 1000.0) / 1000.0
            refined.append((spacing, q))
        return refined


class LivePlot(QWidget):
    """温度-绝对时间实时曲线。"""

    #: 视图需要重绘的信号：跨度切换、回到跟随、用户手动拖拽/缩放都会发出。
    #: 有了它，主窗口就能做到"有变化才重绘"，停止采集后切换跨度也不会僵住。
    view_changed = Signal()

    def __init__(self, store: TraceStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.session_wall_ns = 0
        self.follow = True
        self.span_s: float | None = 10.0     # None = 全览
        self.y_auto = True
        self.temp_unit = "C"                 # "C" 或 "K"，只影响显示
        #: 时间零点（perf ns）。设了它、且处于相对模式时，横轴的 0 就是
        #: 按下「开始实验」那一刻；首个数据点通常会晚 ~300 ms（启动延迟），
        #: 于是曲线从 0 之后一点才开始——这段空白是事实，如实画出来。
        self.zero_perf_ns: int | None = None
        self._rel_mode = False

        pg.setConfigOptions(antialias=False, background="w", foreground="k")
        self.plot = pg.PlotWidget()
        self.axis = WallClockAxis(orientation="bottom")
        self.plot.setAxisItems({"bottom": self.axis})
        # 不画网格：实测网格占单帧渲染的 2/3（有网格 9.9 ms，无网格 3.3 ms），
        # 是界面不流畅的主因。横轴已是绝对时间刻度、纵轴只有温度，
        # 无需网格定位读数。
        self.plot.showGrid(x=False, y=False)
        self.plot.setLabel("left", "温度 / °C")
        self.plot.setLabel("bottom", "时间（墙钟）")
        self.plot.getPlotItem().setDownsampling(auto=False)  # 自己做包络抽稀

        self._curve = self.plot.plot(pen=pg.mkPen("#c0392b", width=1.4))
        # 零点标记（后续接激光触发时会用到）
        self._zero_line = pg.InfiniteLine(angle=90, movable=False,
                                          pen=pg.mkPen("#2c7fb8", width=1.2,
                                                       style=Qt.DashLine))
        self._zero_line.setVisible(False)
        self.plot.addItem(self._zero_line)
        # 一条水平参考线（跟随纵轴自动缩放时的起始温度），成本远低于网格
        self._ref_line = pg.InfiniteLine(angle=0, movable=False,
                                         pen=pg.mkPen("#999999", width=1.0,
                                                      style=Qt.DotLine))
        self._ref_line.setVisible(False)
        self.plot.addItem(self._ref_line)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.plot)

        # 用户拖拽/缩放后自动停止跟随
        self.plot.getViewBox().sigRangeChangedManually.connect(self._on_manual)

    # ---------- 外部接口 ----------
    def start_session(self, session_wall_ns: int) -> None:
        self.session_wall_ns = int(session_wall_ns)
        self.axis.set_session_start(self.session_wall_ns)
        self.follow = True
        self.view_changed.emit()

    def set_span(self, span_s: float | None) -> None:
        self.span_s = span_s
        self.follow = True
        self.view_changed.emit()

    def set_zero_time(self, wall_ns: int | None) -> None:
        """显示/隐藏时间零点竖线（墙钟 ns）。"""
        if wall_ns is None:
            self._zero_line.setVisible(False)
            return
        x = (wall_ns - self.session_wall_ns) / NS
        self._zero_line.setPos(x)
        self._zero_line.setVisible(True)

    def set_rel_mode(self, on: bool) -> None:
        """横轴切换：False=墙钟时刻，True=距离时间零点的秒数（从 0 起）。

        正式实验用相对模式——实验结果关心的是"脉冲打上去后第几秒"，
        墙钟读数在 CSV 与配置里都有，不必挤在横轴上。
        """
        self._rel_mode = bool(on)
        self.axis.set_rel_mode(self._rel_mode)
        self.plot.setLabel("bottom", "时间 / s" if self._rel_mode else "时间（墙钟）")
        self.view_changed.emit()

    def set_zero_perf(self, perf_ns: int | None) -> None:
        """设定横轴 0 对应的 perf 时间戳（= 时间零点）。"""
        self.zero_perf_ns = None if perf_ns is None else int(perf_ns)
        self.view_changed.emit()

    def set_temp_unit(self, unit: str) -> None:
        """切换纵轴显示单位（"C" / "K"）。数据本身不变。"""
        self.temp_unit = "K" if unit == "K" else "C"
        self.plot.setLabel("left", "温度 / K" if self.temp_unit == "K"
                           else "温度 / °C")
        self.view_changed.emit()

    def resume_follow(self) -> None:
        self.follow = True
        self.view_changed.emit()

    # ---------- 刷新 ----------
    def _on_manual(self) -> None:
        self.follow = False
        self.view_changed.emit()

    def refresh(self) -> None:
        st = self.store
        if st.is_empty:
            return
        x = st.relative_seconds()
        if self._rel_mode and self.zero_perf_ns is not None:
            x = x + (st.t0_perf_ns - self.zero_perf_ns) / NS
        x_end = float(x[-1])
        span = self.span_s
        vb = self.plot.getViewBox()
        (cx0, cx1), _ = vb.viewRange()

        # ---- 第一步：决定视图范围 ----
        # 跟随模式的视图目标："右端贴最新点"。但 setXRange 很贵（触发 pyqtgraph
        # 全量重算，实测把采集速率从 6.5 kHz 压到 4.5 kHz），不能每帧调用。
        #
        # 曾经的做法是"偏离超过 15% 才更新"，结果视图最多落后 1.4 s，而数据
        # 却按目标截取 —— 视图左边缘到数据起点之间出现一段空白（用户报的 bug）。
        #
        # 正确思路：**限制视图左边缘的落后量**，右侧允许落后（右侧落后只是
        # 新点晚一点出现，不会产生空白，而左侧落后一定会留白）。
        if span is None:
            x0_target = 0.0
            x1_target = max(x_end, 1e-3)
        else:
            x1_target = x_end
            x0_target = max(0.0, x_end - span)

        if self.follow:
            target_span = max(x1_target - x0_target, 1e-9)
            # 左边缘落后容差：不超过跨度的 2%，且最多 60 ms
            # （60 ms 在屏幕上不足 1 像素列，肉眼不可见）
            tol_left = min(max(target_span * 0.02, 1e-3), 0.06)
            span_err = abs((cx1 - cx0) - target_span)
            if (cx0 < x0_target - tol_left
                    or cx0 > x0_target + tol_left
                    or span_err > target_span * 0.02):
                vb.setXRange(x0_target, x1_target, padding=0)
                (cx0, cx1), _ = vb.viewRange()

        # ---- 第二步：按**视图**范围取数据，并主动多留一点 ----
        # 关键：不能再按 x0_target 截取。视图若还停在旧位置，数据必须覆盖
        # 视图左边缘，否则就会出现空白。这里以视图范围为基准，左右各留
        # 5% 余量（防止抽稀后边缘缺一小段）。
        view_span = max(cx1 - cx0, 1e-9)
        pad = view_span * 0.05
        xi0 = int(np.searchsorted(x, cx0 - pad, "left"))
        xs = x[xi0:]
        ys = st.temp_all[xi0:]

        # 抽稀到约 2 点/像素，保留极值
        width = max(int(vb.width()), 200)
        xs, ys = envelope(xs, ys, width * 2)
        if self.temp_unit == "K":
            ys = ys + 273.15
        self._curve.setData(xs, ys, connect="finite")

        # ---- 第三步：纵轴 ----
        if self.y_auto:
            # 只按视图内的数据定纵轴，避免窗口外的一段把范围撑歪
            m = (xs >= cx0) & (xs <= cx1)
            self._autoscale_y(ys[m] if m.any() else ys)

    def _autoscale_y(self, ys: np.ndarray) -> None:
        if ys.size == 0:
            return
        lo = float(np.min(ys))
        hi = float(np.max(ys))
        pad = max(0.5, (hi - lo) * 0.12)
        # 同理：范围没实质变化就不碰 ViewBox
        (_, _), (cy0, cy1) = self.plot.getViewBox().viewRange()
        new0, new1 = lo - pad, hi + pad
        span = max(new1 - new0, 1e-6)
        if abs(cy0 - new0) > span * 0.1 or abs(cy1 - new1) > span * 0.1:
            self.plot.getViewBox().setYRange(new0, new1, padding=0)
        # 参考线放在"整刻度"处，替代被去掉的网格，便于目测读数
        self._update_ref_line(new0, new1)

    def _update_ref_line(self, y0: float, y1: float) -> None:
        """在视图纵轴中点附近取一个"整"温度值画一条淡参考线。"""
        span = y1 - y0
        if span <= 0:
            return
        # 选一个与跨度相称的步长（0.1/0.2/0.5/1/2/5/10...）
        step = 10 ** np.floor(np.log10(span / 4))
        for mult in (1, 2, 5, 10):
            if span / (step * mult) <= 4:
                step *= mult
                break
        y = np.round((y0 + y1) / 2 / step) * step
        self._ref_line.setPos(y)
        self._ref_line.setVisible(True)
