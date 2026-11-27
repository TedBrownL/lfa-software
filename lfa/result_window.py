"""实验结果窗口：截取后的温升曲线 + 峰点/区间标注 + 平滑 + 导出。

与主界面的关系
--------------
* 主界面负责"采"，本窗口负责"看这一次实验的结果"。
* 曲线横轴用**相对时间零点的秒数**，且从 **0 s 起**：0 就是按下
  「开始实验」的那一刻。第一个数据点通常落在 0 之后约 0.3 s（启动延迟），
  这段空白会如实画出来并标注，而不是把曲线硬挪到 0。
* 平滑只作用在显示上。峰值识别在原始数据上做（见 :mod:`lfa.experiment`），
  所以拖动滑条时标注位置不会跳。
"""

from __future__ import annotations

import os
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from . import experiment as ex
from .experiment import AnalysisResult, ExperimentConfig
from .store import envelope

#: 默认平滑度。0 太毛糙、1 会把升温沿抹平，0.5 在"看得清趋势"与
#: "不歪曲形状"之间比较平衡，也符合导出的 PNG 的要求。
DEFAULT_SMOOTH = 0.5


def c_to_unit(temp_c, unit: str):
    return np.asarray(temp_c) + 273.15 if unit == "K" else np.asarray(temp_c)


def unit_label(unit: str) -> str:
    return "温度 / K" if unit == "K" else "温度 / °C"


class ResultWindow(QMainWindow):
    """单次实验的结果查看窗口。关闭前主界面不允许再次开始实验。"""

    def __init__(self, t: np.ndarray, temp_c: np.ndarray,
                 cfg: ExperimentConfig, res: AnalysisResult,
                 unit: str = "C", device_note: str = "",
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("实验结果")
        self.resize(1440, 860)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.t_all = np.asarray(t, dtype=np.float64)
        self.temp_all = np.asarray(temp_c, dtype=np.float64)
        self.cfg = cfg
        self.res = res
        self.unit = unit
        self.device_note = device_note

        # 只保留展示窗口内的数据；异常时展示全部
        if res.window is not None:
            lo, hi = res.window
            keep = (self.t_all >= lo) & (self.t_all <= hi)
        else:
            keep = np.ones(self.t_all.size, dtype=bool)
        self.t = self.t_all[keep]
        self.temp = self.temp_all[keep]

        self._build_ui()
        self._redraw()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        panel = QWidget()
        panel.setFixedWidth(290)
        panel.setMinimumWidth(250)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, 0, 0, 0)

        # 实验信息
        gb_info = QGroupBox("实验配置")
        f_info = QFormLayout(gb_info)
        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        f_info.addRow(self.lbl_info)
        pl.addWidget(gb_info)

        # 结果判定
        gb_res = QGroupBox("温升特征")
        f_res = QFormLayout(gb_res)
        self.lbl_result = QLabel()
        self.lbl_result.setWordWrap(True)
        f_res.addRow(self.lbl_result)
        pl.addWidget(gb_res)

        # 显示
        gb_view = QGroupBox("显示")
        f_view = QFormLayout(gb_view)

        self.sl_smooth = QSlider(Qt.Orientation.Horizontal)
        self.sl_smooth.setRange(0, 100)
        self.sl_smooth.setValue(int(round(DEFAULT_SMOOTH * 100)))
        self.sl_smooth.setToolTip(
            "0 = 原始数据（毛刺多）；1 = 最大平滑（TensorBoard 式）。\n"
            "只影响曲线显示，峰点与区间标注固定在原始数据上，不随平滑变化。")
        self.lbl_smooth = QLabel(f"{DEFAULT_SMOOTH:.2f}")
        row_s = QHBoxLayout()
        row_s.addWidget(self.sl_smooth, 1)
        row_s.addWidget(self.lbl_smooth)
        f_view.addRow("平滑度", _wrap(row_s))

        self.btn_follow = QPushButton("全览 / 跟随")
        f_view.addRow(self.btn_follow)
        pl.addWidget(gb_view)

        # 导出
        gb_exp = QGroupBox("导出")
        f_exp = QFormLayout(gb_exp)
        self.btn_export = QPushButton("导出数据（zip）")
        f_exp.addRow(self.btn_export)
        self.lbl_export = QLabel("")
        self.lbl_export.setWordWrap(True)
        f_exp.addRow(self.lbl_export)
        pl.addWidget(gb_exp)

        pl.addStretch(1)

        self.btn_close = QPushButton("关闭")
        pl.addWidget(self.btn_close)

        root.addWidget(panel)

        # 曲线
        pg.setConfigOptions(antialias=False, background="w", foreground="k")
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=False, y=False)
        self.plot.setLabel("left", unit_label(self.unit))
        self.plot.setLabel("bottom", "时间 / s")
        self.plot.getPlotItem().setDownsampling(auto=False)
        self._curve = self.plot.plot(pen=pg.mkPen("#c0392b", width=1.4))
        self._markers: list = []
        self._bands: list = []
        self.plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.plot, 1)

        self.btn_close.clicked.connect(self.close)
        self.btn_export.clicked.connect(self._export)
        self.sl_smooth.valueChanged.connect(self._on_smooth)
        self.btn_follow.clicked.connect(self._apply_full_view)

    def _fill_side_panels(self) -> None:
        self.lbl_info.setText("\n".join(self.cfg.summary_lines()))

        if self.res.ok:
            lines = [f"识别到 {len(self.res.peaks)} 个温度峰",
                     f"最大温升 {self.res.delta_t:.2f} °C",
                     f"结束实验 B = {self.res.t_b:.3f} s"]
            if self.res.window is not None:
                lo, hi = self.res.window
                lines.append(f"展示窗口 {lo:.3f} – {hi:.3f} s")
                if self.res.window_clamped:
                    lines.append("⚠ 峰太靠近结束时刻，B′ 被截到 B，"
                                 "实际不是 4:1（下次请晚一点结束实验）")
                else:
                    lines.append(f"（峰前 : 峰后 = {ex.TAIL_RATIO:g} : 1）")
            self.lbl_result.setStyleSheet(
                "color:#b8860b;font-weight:600;" if self.res.window_clamped
                else "color:#1a7f37;font-weight:600;")
        else:
            lines = ["⚠ 实验异常", self.res.reason,
                     "", "曲线按完整时间窗口展示，未截取、未标注。"]
            self.lbl_result.setStyleSheet("color:#c0392b;font-weight:600;")
        self.lbl_result.setText("\n".join(lines))

    # ---------------- 绘制 ----------------
    def _redraw(self) -> None:
        self._fill_side_panels()
        if self.t.size == 0:
            return

        level = self.sl_smooth.value() / 100.0
        y = ex.smooth_series(self.temp, level)
        y_disp = c_to_unit(y, self.unit)

        width = max(int(self.plot.getViewBox().width()), 300)
        xs, ys = envelope(self.t, y_disp, width * 2)
        self._curve.setData(xs, ys, connect="finite")

        self._draw_annotations()
        self._apply_full_view()

    def _apply_full_view(self) -> None:
        """纵轴按「曲线 + 上方标注区」整体定范围；横轴固定从 0 s 起。

        横轴起点必须是 0（= 按下「开始实验」那一刻），不是第一个数据点。
        启动延迟那段没有数据，图上就是一段空白——这是事实，应当画出来。
        """
        if self.t.size == 0:
            return

        vb = self.plot.getViewBox()
        x1 = max(float(self.t[-1]), 1e-3)
        vb.setXRange(-x1 * 0.012, x1 * 1.012, padding=0)

        ys = self._curve.yData
        if ys is None or not len(ys):
            return
        lo, hi = float(np.min(ys)), float(np.max(ys))
        n_rows = min(len(self.res.intervals), ex.MAX_BANDS) if self.res.ok else 0
        ylo, yhi, _ = ex.band_layout(lo, hi, n_rows)
        vb.setYRange(ylo, yhi, padding=0)

    def _draw_annotations(self) -> None:
        for item in self._markers + self._bands:
            self.plot.removeItem(item)
        self._markers = []
        self._bands = []

        if not self.res.ok:
            return

        # ---- 峰点：曲线上打点 + 坐标文字 ----
        for i, p in enumerate(self.res.peaks, 1):
            x = p.t
            y = float(c_to_unit(p.temp_c, self.unit))
            dot = pg.ScatterPlotItem(
                [x], [y], size=9, brush=pg.mkBrush("#1f6feb"),
                pen=pg.mkPen("#ffffff", width=1.2))
            self.plot.addItem(dot)
            self._markers.append(dot)

            unit_sym = "K" if self.unit == "K" else "°C"
            txt = pg.TextItem(
                f"M{_sub(i)} ({x:.3f} s, {y:.2f} {unit_sym})",
                color="#1f6feb", anchor=(0.0, 1.2))
            txt.setFont(QFont("Microsoft YaHei", 8))
            txt.setPos(x, y)
            self.plot.addItem(txt)
            self._markers.append(txt)

        # ---- 区间标注：图像上侧一条等高横线 + 分段端头 + 标签 ----
        if not self.res.intervals:
            return

        ys = self._curve.yData
        if ys is None or not len(ys):
            return
        lo, hi = float(np.min(ys)), float(np.max(ys))
        span = max(hi - lo, 1e-9)
        rows = ex.band_layout(lo, hi, len(self.res.intervals))[2]
        y_line = rows[0]                      # 所有横线同高
        tick_h = span * 0.030                 # 端头半高（数据坐标）

        # 每个区间一条横线。同高 + 首尾相连 = 一条被分段的尺寸标注线；
        # 端头由下面的"边界集合"统一画，避免相邻区间各画一根、粗一倍。
        for iv in self.res.intervals:
            line = pg.PlotDataItem(
                [iv.t0, iv.t1], [y_line, y_line],
                pen=pg.mkPen("#333333", width=1.3))
            self.plot.addItem(line)
            self._bands.append(line)

        # 边界（去重）：A、M₁、M₂、…、B′ 各画一根端头 + 一根向下的延伸线。
        # 延伸线让读者一眼看出这条横线管的是哪段时间，而不是悬在空中。
        bounds = sorted({float(iv.t0) for iv in self.res.intervals}
                        | {float(iv.t1) for iv in self.res.intervals})
        ext_bottom = y_line - span * 0.10
        for xx in bounds:
            tick = pg.PlotDataItem(
                [xx, xx], [y_line - tick_h, y_line + tick_h],
                pen=pg.mkPen("#333333", width=1.3))
            self.plot.addItem(tick)
            self._bands.append(tick)

            ext = pg.PlotDataItem(
                [xx, xx], [ext_bottom, y_line - tick_h],
                pen=pg.mkPen("#999999", width=0.8,
                             style=Qt.PenStyle.DashLine))
            self.plot.addItem(ext)
            self._bands.append(ext)

        # 标签写在横线**之上**：横线已抬到曲线最高点上方 0.34 跨度处，
        # 与峰点的 M 坐标文字之间留了近 0.2 跨度，不会再压上去。
        for iv in self.res.intervals:
            label = pg.TextItem(
                f"{iv.label}  {iv.duration:.4f} s",
                color="#222222", anchor=(0.5, -0.12))
            label.setFont(QFont("Microsoft YaHei", 8))
            label.setPos((iv.t0 + iv.t1) / 2, y_line + tick_h)
            self.plot.addItem(label)
            self._bands.append(label)

    def _on_smooth(self, value: int) -> None:
        self.lbl_smooth.setText(f"{value / 100.0:.2f}")
        if self.t.size:
            self._redraw()

    # ---------------- 导出 ----------------
    def _export(self) -> None:
        from .result_export import export_result

        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)
        default = os.path.join(data_dir, "result_" + time.strftime("%Y%m%d_%H%M%S"))
        path, _ = QFileDialog.getSaveFileName(
            self, "导出实验结果", default, "ZIP (*.zip)")
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        try:
            out = export_result(
                path, self.cfg, self.res, self.t_all, self.temp_all,
                self.unit, self.device_note,
                smooth_level=self.sl_smooth.value() / 100.0)
        except Exception as exc:                                    # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.lbl_export.setText(f"已导出：\n{out}")
        self.lbl_export.setStyleSheet("color:#1a7f37;")

    # ---------------- 生命周期 ----------------
    def closeEvent(self, event) -> None:                                # noqa: N802
        # 通知主界面"结果窗口已关闭"，主界面据此恢复「开始实验」
        parent = self.parent()
        if parent is not None and hasattr(parent, "_on_result_closed"):
            parent._on_result_closed()
        super().closeEvent(event)


def _sub(i: int) -> str:
    return str(i).translate(str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉"))


def _wrap(layout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    return w
