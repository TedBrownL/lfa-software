"""LFA 测温采集主窗口。

本阶段目标（第一步）：横轴为当前实际精确时间（到 ms）、纵轴为温度的实时曲线，
并支持开始/停止、CSV 导出、时间跨度切换。激光触发通道（时间零点）留出接口。
"""

from __future__ import annotations

import os
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QStatusBar, QVBoxLayout, QWidget,
)

from .acquisition import AcquisitionConfig, AcquisitionController
from .experiment import (
    DEFAULT_DENSITY, DEFAULT_SPECIFIC_HEAT, PRESETS, ExperimentConfig, analyze,
)
from .export import ExportMeta, export_csv
from .optris import DeviceInfo
from .plot import LivePlot
from .store import TraceStore
from .timebase import NS, TimeBase, format_wall


SPAN_CHOICES = [("10 s", 10.0), ("30 s", 30.0), ("60 s", 60.0),
                ("5 min", 300.0), ("全览", None)]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LFA 温度采集 — 实时曲线")
        # 窗口要足够大：左栏 + 实验配置栏各占固定宽，剩下的全给曲线。
        # 1280 宽时曲线只剩约 680 px，图上标注会挤在一起，所以放大默认尺寸。
        self.resize(1680, 950)
        self.setMinimumSize(1200, 720)

        self.timebase = TimeBase()
        self.store = TraceStore()
        self.acq = AcquisitionController(self.timebase)
        self.device_info: DeviceInfo | None = None
        self.zero_wall_ns: int | None = None
        self.session_notes: list[str] = []
        self.current_unit = "C"
        #: 实验状态：None=空闲，否则为进行中的 ExperimentConfig
        self.exp_cfg: ExperimentConfig | None = None
        #: 结果窗口（打开期间禁止再开始实验）
        self.result_win = None

        self._build_ui()
        self._connect()

        # 界面刷新：16 ms 恰好是一个 Windows 系统定时器粒度（15.6 ms），
        # 实测能跑满 62 Hz；其他间隔会被向上量化（66 ms -> 78.7 ms = 12.7 Hz），
        # 这才是之前"界面卡顿"的主因。配合去掉网格（渲染 9.9 -> 3.3 ms），
        # 可以显著提高画面流畅度。
        self._ui_timer = QTimer(self)
        self._ui_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._ui_timer.setInterval(16)
        self._ui_timer.timeout.connect(self._on_refresh)
        self._ui_timer.start()
        self._dirty = False

        # 采样率/点数这类统计量不需要 60 Hz 刷新，单独用 4 Hz 定时器，
        # 避免在主循环里做 searchsorted 之类的额外工作
        self._stat_timer = QTimer(self)
        self._stat_timer.setInterval(250)
        self._stat_timer.timeout.connect(self._update_stats)
        self._stat_timer.start()

        # 顶栏时钟：独立 100 ms 定时器，空闲时也走时间
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(100)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start()

        # 启动后自动读取一次设备设置并常显（延迟到事件循环启动之后执行，
        # 避免在构造函数里阻塞窗口显示）
        QTimer.singleShot(0, lambda: self.refresh_device_settings(auto=True))

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        # 左栏 1：仪器与采集控制。
        # 宽 236：够放下"平均 1 ms　智能 关　保持 关"这类状态行，
        # 又不像旧版 300 px 那样白占曲线的地方。
        panel = QWidget()
        panel.setFixedWidth(236)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, 0, 0, 0)

        # 设备
        gb_dev = QGroupBox("测温仪")
        f_dev = QFormLayout(gb_dev)
        self.cb_port = QComboBox()
        self._fill_ports()
        self.btn_rescan = QPushButton("刷新串口")
        port_row = QHBoxLayout()
        port_row.addWidget(self.cb_port, 1)
        port_row.addWidget(self.btn_rescan)
        f_dev.addRow("串口", _wrap(port_row))
        self.cb_baud = QComboBox()
        self.cb_baud.addItems(["115200", "921600"])
        f_dev.addRow("波特率", self.cb_baud)
        self.lbl_dev = QLabel("未连接")
        self.lbl_dev.setWordWrap(True)
        f_dev.addRow("状态", self.lbl_dev)
        pl.addWidget(gb_dev)

        # 采集（预览用：不记录实验，只看信号是否正常）
        gb_run = QGroupBox("预览采集")
        f_run = QFormLayout(gb_run)

        self.btn_start = QPushButton("开始采集")
        self.btn_start.setToolTip(
            "仅用于检查信号。实验请用中间的「开始实验」，\n"
            "它会把按下时刻记为时间零点并保存整段数据。")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.setEnabled(False)
        self.btn_clear = QPushButton("清空曲线")
        for b in (self.btn_start, self.btn_stop, self.btn_export, self.btn_clear):
            f_run.addRow(b)

        # 高级参数：默认折叠。推送粒度是内部调优项，对曲线形状没有影响，
        # 放在主界面只会让人误以为"改了没生效"。
        self.btn_adv = QPushButton("高级 ▸")
        self.btn_adv.setCheckable(True)
        self.btn_adv.setChecked(False)
        self.btn_adv.setFlat(True)
        f_run.addRow(self.btn_adv)
        self._adv_widget = QWidget()
        f_adv = QFormLayout(self._adv_widget)
        f_adv.setContentsMargins(0, 0, 0, 0)
        self.sp_block = QSpinBox()
        self.sp_block.setRange(16, 2000)
        self.sp_block.setValue(64)
        self.sp_block.setSingleStep(16)
        self.sp_block.setSuffix(" 点/推送")
        self.sp_block.setToolTip(
            "采集进程每次打包送给界面的采样点数。\n"
            "只影响内部通信节奏，不改变曲线形状与最终数据。\n"
            "经验规则：对应时长应小于界面刷新间隔（16 ms），\n"
            "6.5 kHz 下 64 点 ≈ 10 ms；设太大会变成“两帧才更新一次”，\n"
            "画面看起来一跳一跳的。")
        f_adv.addRow("推送粒度", self.sp_block)
        self._adv_widget.setVisible(False)
        f_run.addRow(self._adv_widget)
        pl.addWidget(gb_run)

        # 信号处理（决定测到的温升是否真实）
        gb_sig = QGroupBox("信号处理")
        f_sig = QFormLayout(gb_sig)
        # 常显：启动即自动读取，不需要手动点按钮。
        self.lbl_sig = QLabel("正在读取设备设置…")
        self.lbl_sig.setWordWrap(True)
        f_sig.addRow(self.lbl_sig)

        # 平均时间：输入框 + 明确的「应用」按钮。
        # 输入框本身**不**直接写设备（避免边打字边发串口命令），
        # 但按钮必须写清楚它做的是"应用框里的值"，而不是只有"设成 1 ms"。
        self.sp_avg = QSpinBox()
        self.sp_avg.setRange(1, 65000)
        self.sp_avg.setValue(1)
        self.sp_avg.setSuffix(" ms")
        self.sp_avg.setKeyboardTracking(False)
        self.sp_avg.setToolTip(
            "平均时间常数（一阶低通），不是滑动窗口。出厂默认 100 ms。\n"
            "改完按回车，或点下面的「应用」按钮写入设备。\n"
            "LFA 测量应设为最小 1 ms：100 ms 会把温升前沿抹平、使半升温时间偏大。\n"
            "注意：静止物体上看不出差别，它只在温度有变化时才显现。")
        f_sig.addRow("平均时间", self.sp_avg)

        self.btn_avg_apply = QPushButton("应用平均时间（写入设备）")
        self.btn_avg_apply.setToolTip(
            "把左边输入框里的值写入测温仪，并回读确认。\n"
            "快捷键：在输入框里按回车效果相同。")
        f_sig.addRow(self.btn_avg_apply)

        self.btn_avg_lfa = QPushButton("设为 1 ms + 关保持（LFA 推荐）")
        self.btn_avg_lfa.setToolTip(
            "把平均时间设为 1 ms 并关闭保持模式，适用于 LFA 测量。\n"
            "这只是个快捷方式，不会使用左边输入框里的值。")
        f_sig.addRow(self.btn_avg_lfa)
        pl.addWidget(gb_sig)

        # 显示
        gb_view = QGroupBox("显示")
        f_view = QFormLayout(gb_view)
        self.cb_span = QComboBox()
        for label, _ in SPAN_CHOICES:
            self.cb_span.addItem(label)
        self.cb_span.setCurrentIndex(0)
        f_view.addRow("时间跨度", self.cb_span)
        self.btn_follow = QPushButton("回到跟随")
        f_view.addRow(self.btn_follow)
        # 温度单位：只影响显示，内部与 CSV 一律 °C
        self.cb_unit = QComboBox()
        self.cb_unit.addItem("°C", "C")
        self.cb_unit.addItem("K", "K")
        self.cb_unit.setToolTip(
            "曲线的纵轴单位。内部数据与导出的 CSV 始终是 °C，\n"
            "切换单位只改变显示与实验结果的标注文字。")
        f_view.addRow("温度单位", self.cb_unit)
        pl.addWidget(gb_view)

        # 读数
        gb_now = QGroupBox("当前读数")
        f_now = QFormLayout(gb_now)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.lbl_clock = QLabel("--:--:--.---")
        self.lbl_temp = QLabel("--.- °C")
        self.lbl_rate = QLabel("-- Hz")
        self.lbl_count = QLabel("0")
        for w, txt in ((self.lbl_clock, None), (self.lbl_temp, None),
                       (self.lbl_rate, None), (self.lbl_count, None)):
            w.setFont(mono)
        self.lbl_clock.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.lbl_temp.setStyleSheet("font-size: 26px; font-weight: 700; color:#c0392b;")
        f_now.addRow("墙钟时间", self.lbl_clock)
        f_now.addRow("温度", self.lbl_temp)
        f_now.addRow("采样率", self.lbl_rate)
        f_now.addRow("样本数", self.lbl_count)
        pl.addWidget(gb_now)
        pl.addStretch(1)

        root.addWidget(panel)

        # 中栏：实验配置（本次实验的信息 + 开始/结束）
        root.addWidget(self._build_experiment_panel())

        # 右侧曲线（占大部分面积）
        self.plot = LivePlot(self.store)
        self.plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.plot.setMinimumWidth(600)
        root.addWidget(self.plot, 1)

        # 左栏与配置栏都固定宽，曲线吃掉其余空间；显式指定拉伸因子，
        # 保证拉大窗口时多出来的宽度全部给曲线，而不是被两栏分走。
        root.setStretch(0, 0)
        root.setStretch(1, 0)
        root.setStretch(2, 1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("就绪：选择串口后点击「开始实验」")

    def _build_experiment_panel(self) -> QWidget:
        """实验配置栏：一次实验的描述信息，以及开始/结束按钮。"""
        panel = QWidget()
        panel.setFixedWidth(248)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, 0, 0, 0)

        gb_cfg = QGroupBox("实验配置")
        f = QFormLayout(gb_cfg)

        self.sp_ambient = _dspin(0.0, 1000.0, 300.0, 1.0, " K")
        self.sp_ambient.setToolTip("环境温度，默认 300 K。")
        f.addRow("环境温度", self.sp_ambient)

        self.sp_power = _dspin(0.0, 10000.0, 10.0, 0.5, " W")
        f.addRow("激光功率", self.sp_power)

        self.sp_pulsew = _dspin(0.001, 1000.0, 0.1, 0.01, " s")
        self.sp_pulsew.setToolTip("单个脉冲的激光持续时间。用于识别峰的合并窗口。")
        f.addRow("激光脉冲时间", self.sp_pulsew)

        self.sp_pulsen = QSpinBox()
        self.sp_pulsen.setRange(1, 100)
        self.sp_pulsen.setValue(1)
        self.sp_pulsen.setToolTip("脉冲数，必须是 >= 1 的整数。")
        f.addRow("脉冲数", self.sp_pulsen)

        # 样品：下拉可选预设，也可直接输入材料名（此时用默认参数）
        self.cb_material = QComboBox()
        self.cb_material.setEditable(True)
        self.cb_material.addItem("")
        for name in PRESETS:
            self.cb_material.addItem(name)
        self.cb_material.setToolTip(
            "选择预设材料会自动填入比热容与密度；\n"
            "也可以直接输入名称，此时两者取默认值。")
        f.addRow("样品材料", self.cb_material)

        self.sp_heat = _dspin(1.0, 100000.0, DEFAULT_SPECIFIC_HEAT, 10.0,
                              " J/(kg·K)")
        f.addRow("样品比热容", self.sp_heat)

        self.sp_density = _dspin(1.0, 100000.0, DEFAULT_DENSITY, 10.0,
                                 " kg/m³")
        f.addRow("样品密度", self.sp_density)

        self.sp_radius = _dspin(0.1, 1000.0, 10.0, 1.0, " mm")
        f.addRow("测温半径", self.sp_radius)
        pl.addWidget(gb_cfg)

        gb_run = QGroupBox("实验控制")
        f_run = QFormLayout(gb_run)
        self.btn_exp_start = QPushButton("开始实验")
        self.btn_exp_start.setToolTip(
            "以按下此刻为**时间零点**，开始记录温升曲线。\n"
            "接入机械遮挡后，零点将改为硬件触发边沿。")
        self.btn_exp_stop = QPushButton("结束实验")
        self.btn_exp_stop.setEnabled(False)
        f_run.addRow(self.btn_exp_start)
        f_run.addRow(self.btn_exp_stop)
        self.lbl_exp_state = QLabel("未开始")
        self.lbl_exp_state.setWordWrap(True)
        f_run.addRow(self.lbl_exp_state)
        pl.addWidget(gb_run)

        pl.addStretch(1)
        return panel

    def _fill_ports(self) -> None:
        import serial.tools.list_ports as lp

        cur = self.cb_port.currentData()
        self.cb_port.clear()
        for p in sorted(lp.comports(), key=lambda x: x.device):
            hwid = p.hwid or ""
            if "BTHENUM" in hwid:          # 蓝牙虚拟串口，与课题无关
                continue
            desc = (p.description or "").strip()
            self.cb_port.addItem(f"{p.device} — {desc}", p.device)
        if cur:
            idx = self.cb_port.findData(cur)
            if idx >= 0:
                self.cb_port.setCurrentIndex(idx)

    # ---------------- 信号 ----------------
    def _connect(self) -> None:
        self.btn_rescan.clicked.connect(self._on_rescan)
        self.btn_avg_apply.clicked.connect(self._apply_avg_value)
        self.btn_avg_lfa.clicked.connect(self._apply_lfa_setup)
        self.btn_adv.toggled.connect(self._on_adv_toggled)
        # 输入框回车 = 点「应用」。用 editingFinished 而不是 valueChanged：
        # 前者只在用户真正结束编辑（回车/失焦）时发一次，后者会在
        # 上下箭头、程序同步设备真值时也发，容易误触发写设备。
        self.sp_avg.editingFinished.connect(self._apply_avg_value)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_export.clicked.connect(self._export)
        self.btn_clear.clicked.connect(self._clear)
        self.cb_span.currentIndexChanged.connect(self._on_span_changed)
        self.btn_follow.clicked.connect(self.plot.resume_follow)
        self.plot.view_changed.connect(self._on_view_changed)
        self.cb_unit.currentIndexChanged.connect(self._on_unit_changed)

        # 实验
        self.btn_exp_start.clicked.connect(self._start_experiment)
        self.btn_exp_stop.clicked.connect(self._stop_experiment)
        self.cb_material.currentTextChanged.connect(self._on_material_changed)

        self.acq.block_ready.connect(self._on_block)
        self.acq.opened.connect(self._on_opened)
        self.acq.failed.connect(self._on_failed)
        self.acq.stopped.connect(self._on_stopped)

    def _on_unit_changed(self) -> None:
        self.current_unit = self.cb_unit.currentData() or "C"
        self.plot.set_temp_unit(self.current_unit)
        self._dirty = True

    def _on_material_changed(self, name: str) -> None:
        """选择预设材料时自动填入比热容与密度。

        用户手动输入材料名时（不在预设里）不覆盖他已改过的数值——
        否则改完参数再补个名字，数值就被悄悄重置了。
        """
        name = (name or "").strip()
        if name in PRESETS:
            heat, dens = PRESETS[name]
            self.sp_heat.setValue(heat)
            self.sp_density.setValue(dens)

    def _on_adv_toggled(self, checked: bool) -> None:
        self._adv_widget.setVisible(checked)
        self.btn_adv.setText("高级 ▾" if checked else "高级 ▸")

    def _on_rescan(self) -> None:
        self._fill_ports()
        self.refresh_device_settings(auto=True)

    # ---------------- 信号处理设置 ----------------
    def _open_device(self):
        """打开一次测温仪连接（静默版，不弹窗）。返回设备对象或 None。"""
        if self.acq.is_running:
            return None
        port = self.cb_port.currentData()
        if not port:
            return None
        from .optris import OptrisCT4M

        dev = OptrisCT4M(port, int(self.cb_baud.currentText()))
        try:
            dev.open()
            return dev
        except Exception:
            return None

    def refresh_device_settings(self, auto: bool = False) -> None:
        """读取并常显设备的信号处理设置。

        采集前会自动调用（见 :meth:`_start`），界面启动和换串口时也会调用，
        因此去掉了原来那个"点了看不出变化"的手动按钮。
        """
        if self.acq.is_running:
            return
        dev = self._open_device()
        if dev is None:
            self.lbl_sig.setText("⚠ 未连接设备")
            self.lbl_sig.setStyleSheet("color:#c0392b; font-weight:600;")
            self._signal_state = None
            return
        try:
            self._signal_state = (dev.read_averaging_time_ms(),
                                  dev.read_smart_averaging(),
                                  dev.read_hold_mode())
        finally:
            dev.close()
        self._render_signal_state()

    def _write_device_settings(self, avg_ms: int | None = None,
                               hold: int | None = None) -> bool:
        """把设置写入设备并回读确认。返回是否成功。

        ``avg_ms`` / ``hold`` 为 None 表示该项不动。这里是唯一的写设备入口，
        两个按钮都走它，避免"某个入口忘了回读"这类不一致。
        """
        if self.acq.is_running:
            self.statusBar().showMessage("采集中不能改设备设置，请先停止采集。")
            self._sync_avg_spinbox()
            return False
        if getattr(self, "_avg_writing", False):
            return False

        self._avg_writing = True
        try:
            dev = self._open_device()
            if dev is None:
                self.statusBar().showMessage(
                    f"⚠ 无法打开 {self.cb_port.currentData()}，设置未改变")
                self._sync_avg_spinbox()
                return False
            try:
                ok = True
                if avg_ms is not None:
                    ok &= dev.set_averaging_time_ms(avg_ms)
                if hold is not None:
                    ok &= dev.set_hold_mode(hold)
                # 无论成败都回读一次，界面永远显示设备真值而不是我们的意图
                self._signal_state = (dev.read_averaging_time_ms(),
                                      dev.read_smart_averaging(),
                                      dev.read_hold_mode())
            finally:
                dev.close()
        finally:
            self._avg_writing = False

        self._render_signal_state()
        real = self._signal_state[0]
        if ok and (avg_ms is None or real == avg_ms):
            extra = "，保持已关闭" if hold == 0 else ""
            self.statusBar().showMessage(
                f"已写入设备：平均 {real} ms{extra}（已回读确认）")
        else:
            self.statusBar().showMessage(
                f"⚠ 写入失败，设备实际为 {real} ms")
            self._sync_avg_spinbox()
        return bool(ok and (avg_ms is None or real == avg_ms))

    def _apply_avg_value(self) -> None:
        """「应用平均时间」/ 输入框回车：把框里的值写入设备。"""
        self._write_device_settings(avg_ms=int(self.sp_avg.value()))

    def _apply_lfa_setup(self) -> None:
        """「设为 1 ms + 关保持」：LFA 推荐的固定配置（不使用框里的值）。"""
        if self._write_device_settings(avg_ms=1, hold=0):
            self.statusBar().showMessage(
                "已写入设备：平均 1 ms，保持已关闭（已回读确认）")

    def _sync_avg_spinbox(self) -> None:
        """把数字框同步成设备的真实值（blockSignals 防误触发写设备）。"""
        avg = getattr(self, "_signal_state", (None,))[0]
        if avg is None:
            return
        blocked = self.sp_avg.blockSignals(True)
        try:
            self.sp_avg.setValue(int(avg))
        finally:
            self.sp_avg.blockSignals(blocked)

    def _render_signal_state(self) -> None:
        avg, smart, hold = getattr(self, "_signal_state", (None, None, None))
        if avg is None:
            self.lbl_sig.setText("⚠ 设备无响应")
            self.lbl_sig.setStyleSheet("color:#c0392b; font-weight:600;")
            return
        self._sync_avg_spinbox()
        hold_txt = {0: "关", 1: "峰值保持", 2: "谷值保持"}.get(hold, str(hold))
        lines = [f"设备当前：平均 {avg} ms　智能 {'开' if smart else '关'}　"
                 f"保持 {hold_txt}"]

        warn = []
        if avg > 1:
            warn.append("平均时间过大，会抹平升温沿")
        if hold not in (None, 0):
            warn.append("保持模式会粘住峰值")
        if smart:
            warn.append("智能平均在大边沿处会改变时间常数")

        if warn:
            self.lbl_sig.setStyleSheet("color:#c0392b; font-weight:600;")
            lines.append("⚠ " + "；".join(warn))
        else:
            self.lbl_sig.setStyleSheet("color:#1a7f37; font-weight:600;")
            lines.append("✓ 适合 LFA 测量")
        self.lbl_sig.setText("\n".join(lines))

    def _ensure_signal_ready(self) -> bool:
        """采集前检查设备信号处理设置；返回 False 表示用户选择放弃。

        为什么必须检查：平均时间是**一阶低通时间常数**，出厂 100 ms 会把
        LFA 的温升前沿整体平滑掉，导致半升温时间偏大，而且从曲线上看不出来
        ——这是最容易悄悄毁掉一次测量的设置。
        """
        # 采集前自动读取一次，保证显示的是"此刻"的真实设置
        self.refresh_device_settings(auto=True)
        state = getattr(self, "_signal_state", None)
        if state is None:
            return True          # 读不到就不拦，交给采集线程报错

        avg, smart, hold = state
        problems = []
        if avg is not None and avg > 20:
            problems.append(f"平均时间 {avg} ms（建议 1 ms）")
        if hold not in (None, 0):
            problems.append("保持模式未关闭")

        if not problems:
            return True
        ans = QMessageBox.question(
            self, "信号处理设置不适合 LFA",
            "设备当前设置：\n  " + "\n  ".join(problems) +
            "\n\n这会抹平温升前沿、使半升温时间偏大，且从曲线上看不出来。\n"
            "建议先点「设为 1 ms + 关保持（LFA 推荐）」。\n\n仍要以当前设置继续采集吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return ans == QMessageBox.Yes

    def _start(self) -> None:
        port = self.cb_port.currentData()
        if not port:
            QMessageBox.warning(self, "无串口", "没有可用串口，请检查测温仪连接。")
            return
        cfg = AcquisitionConfig(
            port=port,
            baudrate=int(self.cb_baud.currentText()),
            block_size=self.sp_block.value(),
        )
        # 平均时间过大时提醒：这会直接抹平温升前沿，是不易察觉的系统性误差
        if not self._ensure_signal_ready():
            return

        self.store.clear()
        self.zero_wall_ns = None
        self.plot.set_zero_time(None)
        # 会话起点用当前墙钟；worker 的首点会略晚于它，导出时以首点为准
        self.plot.start_session(self.timebase.wall_now())
        self._follow_started = False

        self.acq.start(cfg)
        self.btn_start.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.statusBar().showMessage(f"正在打开 {port} …")
    # ---------------- 实验 ----------------
    def _collect_config(self) -> ExperimentConfig:
        """从界面读取一次实验配置。"""
        return ExperimentConfig(
            ambient_k=float(self.sp_ambient.value()),
            laser_power_w=float(self.sp_power.value()),
            pulse_width_s=float(self.sp_pulsew.value()),
            pulse_count=int(self.sp_pulsen.value()),
            material=(self.cb_material.currentText() or "").strip(),
            specific_heat=float(self.sp_heat.value()),
            density=float(self.sp_density.value()),
            radius_mm=float(self.sp_radius.value()),
        )

    def _start_experiment(self) -> None:
        """开始一次正式实验：此刻即时间零点，整段数据保存到结束为止。"""
        if self.result_win is not None:
            QMessageBox.information(
                self, "请先关闭结果窗口",
                "「实验结果」窗口还开着。关闭它之后才能开始新的实验。")
            return
        if self.acq.is_running:
            QMessageBox.information(
                self, "正在采集", "请先停止当前的预览采集。")
            return

        port = self.cb_port.currentData()
        if not port:
            QMessageBox.warning(self, "无串口", "没有可用串口，请检查测温仪连接。")
            return
        if not self._ensure_signal_ready():
            return

        cfg = self._collect_config()
        cfg.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        # 时间零点：按下按钮这一刻的墙钟。机械遮挡方案接入后，
        # 这一行要改成硬件触发边沿的时间戳（其余逻辑不变）。
        zero_ns = self.timebase.wall_now()
        cfg.zero_wall_ns = int(zero_ns)

        self.store.clear()
        self.zero_wall_ns = int(zero_ns)
        self.plot.start_session(int(zero_ns))
        self.plot.set_zero_time(int(zero_ns))
        self._follow_started = False
        self.exp_cfg = cfg

        acq_cfg = AcquisitionConfig(
            port=port,
            baudrate=int(self.cb_baud.currentText()),
            block_size=self.sp_block.value(),
        )
        self.acq.start(acq_cfg)
        self.btn_exp_start.setEnabled(False)
        self.btn_exp_stop.setEnabled(True)
        self.btn_start.setEnabled(False)
        # 横轴切到"距离零点多少秒"：0 就是按下本按钮的那一刻，曲线从左往右
        # 从 0 长出来，而不是从墙钟时刻或第一个数据点开始。
        self.plot.set_zero_perf(self.zero_perf_ns())
        self.plot.set_rel_mode(True)
        self.lbl_exp_state.setText(
            f"进行中\n零点 {format_wall(int(zero_ns))}\n0 点")
        self.lbl_exp_state.setStyleSheet("color:#c0392b;font-weight:600;")
        self.statusBar().showMessage("实验中 …（此刻为时间零点）")

    def _stop_experiment(self) -> None:
        """结束实验：停采 → 分析 → 弹出结果窗口。"""
        if self.exp_cfg is None:
            return
        self.btn_exp_stop.setEnabled(False)
        self.lbl_exp_state.setText("正在结束 …")
        self.statusBar().showMessage("正在结束实验 …")
        self.acq.stop()

    def _show_result(self) -> None:
        """实验结束后做分析并打开结果窗口。"""
        from .result_window import ResultWindow

        cfg = self.exp_cfg
        self.exp_cfg = None
        if cfg is None:
            return

        cfg.ended_at = time.strftime("%Y-%m-%d %H:%M:%S")
        n = len(self.store)
        if n == 0:
            QMessageBox.warning(self, "无数据", "本次实验没有采到任何数据。")
            self._reset_experiment_ui()
            return

        t_perf = self.store.t_perf_all
        temp = self.store.temp_all.astype(np.float64)
        # 相对时间零点（不是相对会话起点）：零点就是按下开始实验的时刻
        t = (t_perf - self.zero_perf_ns()) / NS
        cfg.n_samples = int(n)
        dur = float(t[-1] - t[0]) if n > 1 else 0.0
        cfg.sample_rate_hz = (n - 1) / dur if dur > 0 else 0.0
        # 启动延迟：零点到首点之间是空的。真机实测约 300 ms，其中约
        # 280 ms 是子进程 spawn。这个数必须记录并显示——它直接决定
        # "零点相对真实出光时刻"的误差，机械遮挡方案要压掉的就是它。
        cfg.start_delay_s = float(t[0])

        res = analyze(t, temp, cfg)
        self._dirty = False

        info = self.device_info
        note = ""
        if info is not None:
            note = f"Optris CT4ML  SN {info.serial_number}  FW {info.firmware}"

        self.result_win = ResultWindow(
            t, temp, cfg, res, unit=self.current_unit,
            device_note=note, parent=self)
        self.result_win.show()
        self.result_win.raise_()
        self.statusBar().showMessage(
            "实验结束，已打开结果窗口" if res.ok else
            f"实验结束，但判定异常：{res.reason}")

    def zero_perf_ns(self) -> int:
        """时间零点对应的 perf 时间戳。

        采集存的是 perf 时间戳，而零点记的是墙钟。两者通过 TimeBase 换算：
        ``wall = anchor_wall + (perf - anchor_perf)`` 逆解即可。
        用同一个锚点，保证零点与数据在同一时间基准上。
        """
        ap, aw = self.timebase.anchor()
        return int(self.zero_wall_ns) - aw + ap

    def _on_result_closed(self) -> None:
        self.result_win = None
        self._reset_experiment_ui()

    def _reset_experiment_ui(self) -> None:
        self.btn_exp_start.setEnabled(True)
        self.btn_exp_stop.setEnabled(False)
        self.btn_start.setEnabled(True)
        self.lbl_exp_state.setText("未开始")
        self.lbl_exp_state.setStyleSheet("")

    def _stop(self) -> None:
        self.btn_stop.setEnabled(False)
        self.statusBar().showMessage("正在停止 …")
        self.acq.stop()

    def _on_opened(self, info: DeviceInfo) -> None:
        self.device_info = info
        self.btn_stop.setEnabled(True)
        self.btn_export.setEnabled(True)
        self.lbl_dev.setText(f"{info.port} @ {info.baudrate}\n"
                             f"SN {info.serial_number}  FW {info.firmware}")
        self.statusBar().showMessage("采集中 …")

    def _on_block(self, block) -> None:
        self.store.append_block(block.t_perf_ns, block.t_wall_ns, block.temp_c)
        self._dirty = True          # 有数据才重绘，没数据不白刷
        # 预览采集时用首点确定会话起点，使横轴零点与数据严格对齐。
        # 正式实验不这样做：零点必须是"按下开始实验"的时刻，否则 AM 区间
        # 会被首点之前的空白吃掉，标注就与真实时间对不上。
        if self.exp_cfg is None:
            if not getattr(self, "_follow_started", False) and len(self.store):
                self.plot.start_session(int(self.store.t_wall_all[0]))
                self._follow_started = True

    def _on_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "采集错误", msg)
        self.statusBar().showMessage(msg)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if self.exp_cfg is not None:
            self.exp_cfg = None
            self._reset_experiment_ui()

    def _on_stopped(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_export.setEnabled(len(self.store) > 0)
        # 实验结束走结果窗口；预览采集只更新状态栏
        if self.exp_cfg is not None:
            self._show_result()
            return
        # 预览采集结束后回到墙钟横轴，避免下次预览还停在相对模式
        self.plot.set_rel_mode(False)
        self.plot.set_zero_perf(None)
        self.statusBar().showMessage(f"已停止，共 {len(self.store)} 点")

    # ---------------- 刷新 ----------------
    def _on_span_changed(self) -> None:
        self.plot.set_span(SPAN_CHOICES[self.cb_span.currentIndex()][1])

    def _on_view_changed(self) -> None:
        """跨度 / 跟随 / 手动缩放变化后要求重绘一次（停采时同样有效）。"""
        self._dirty = True

    def _on_refresh(self) -> None:
        # 只在"有新数据"时重绘。采集放进独立进程后，重绘不再阻塞采样，
        # 但每帧对同一批数据重复 setData 仍是纯浪费——推送间隔已经调到
        # 小于 16 ms，正常情况下每帧都会有新数据。
        if self._dirty:
            self.plot.refresh()
            self._dirty = False
        self._update_readouts()

    def _update_readouts(self) -> None:
        now = self.timebase.wall_now()
        self.lbl_clock.setText(format_wall(now))
        if len(self.store):
            t = float(self.store.temp_all[-1])
            unit = "K" if self.current_unit == "K" else "°C"
            v = t + 273.15 if self.current_unit == "K" else t
            self.lbl_temp.setText(f"{v:.1f} {unit}")
            self.lbl_count.setText(f"{len(self.store):,}")
            if self.exp_cfg is not None:
                self.lbl_exp_state.setText(
                    f"进行中\n零点 "
                    f"{format_wall(int(self.exp_cfg.zero_wall_ns))}\n"
                    f"{len(self.store):,} 点")

    def _update_rate(self) -> None:
        """瞬时采样率：取最近约 1 s 的窗口，而不是整段平均。

        整段平均会在采集初期显示偏低（首块尚未填满），停采后也不再变化，
        容易误判设备性能。
        """
        n = len(self.store)
        if n < 30:
            self.lbl_rate.setText("-- Hz")
            return
        t = self.store.t_perf_all
        span_ns = 1 * NS
        i0 = int(np.searchsorted(t, t[-1] - span_ns, "left"))
        if n - i0 < 10:
            i0 = max(0, n - 100)
        dur = (t[-1] - t[i0]) / NS
        if dur <= 0:
            return
        self.lbl_rate.setText(f"{(n - 1 - i0) / dur:,.0f} Hz")

    def _update_stats(self) -> None:
        """4 Hz 更新采样率（60 Hz 刷新这个量既没意义也多余）。"""
        self._update_rate()

    def _update_clock(self) -> None:
        if not self.acq.is_running:
            self.lbl_clock.setText(format_wall(self.timebase.wall_now()))

    # ---------------- 其它 ----------------
    def _clear(self) -> None:
        if self.acq.is_running:
            QMessageBox.information(self, "采集中", "请先停止采集再清空。")
            return
        self.store.clear()
        self.plot.plot.clear()
        self.plot.start_session(self.timebase.wall_now())
        self.plot.set_rel_mode(False)
        self.plot.set_zero_perf(None)
        self.lbl_count.setText("0")
        self.btn_export.setEnabled(False)

    def _export(self) -> None:
        if len(self.store) == 0:
            return
        default = time.strftime("lfa_temp_%Y%m%d_%H%M%S.csv")
        # 默认存到项目 data 目录，避免落在不确定的当前工作目录
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", os.path.join(data_dir, default), "CSV (*.csv)")
        if not path:
            return
        info = self.device_info
        meta = ExportMeta(
            device=f"Optris CT4ML",
            serial_number=info.serial_number if info else "",
            firmware=info.firmware if info else "",
            zero_wall_ns=self.zero_wall_ns,
        )
        n = export_csv(path, self.store, meta)
        self.statusBar().showMessage(f"已导出 {n} 点到 {path}")

    def closeEvent(self, event) -> None:                                  # noqa: N802
        self.acq.stop()
        super().closeEvent(event)


def _wrap(layout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    return w


def _dspin(lo: float, hi: float, val: float, step: float,
           suffix: str) -> QDoubleSpinBox:
    """统一的浮点输入框：去掉千分位、固定小数位、带单位后缀。"""
    sp = QDoubleSpinBox()
    sp.setRange(lo, hi)
    sp.setDecimals(3 if step < 0.01 else (2 if step < 0.1 else 1))
    sp.setSingleStep(step)
    sp.setValue(val)
    sp.setSuffix(suffix)
    sp.setKeyboardTracking(False)
    sp.setGroupSeparatorShown(False)
    return sp
