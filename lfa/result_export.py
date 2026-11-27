"""把一次实验打包成 ``result_<编号>.zip``。

包内结构（全部在 zip 根目录下，解压即得一个文件夹）::

    result_20261125_183000/
    ├─ raw_AB.csv          原始 A→B 全段数据（未截取）
    ├─ config.json         实验配置 + 分析摘要
    ├─ curve.png           截取并标注后的温升曲线
    └─ 实验结论.md

为什么打包而不是散放一个文件夹：将来「分析」页的「导入」按钮只需要
选一个 zip，不必让用户去找目录；也避免用户误改其中某个文件导致
CSV 与标注对不上。
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
import zipfile

import numpy as np

from . import experiment as ex
from .experiment import AnalysisResult, ExperimentConfig
from .timebase import NS, format_wall_iso


def _csv_bytes(t: np.ndarray, temp_c: np.ndarray,
               cfg: ExperimentConfig) -> bytes:
    """原始 AB 段数据。t 为相对时间零点的秒数。"""
    buf = io.StringIO()
    buf.write("# LFA 实验结果 — 原始数据（A→B 全段，未截取）\n")
    buf.write(f"# 实验开始: {cfg.started_at}  结束: {cfg.ended_at}\n")
    buf.write(f"# 时间零点(墙钟): {format_wall_iso(cfg.zero_wall_ns)}"
              if cfg.zero_wall_ns else "# 时间零点: 未记录")
    buf.write("\n")
    buf.write(f"# 样品: {cfg.material or '未填写'}  "
              f"比热容 {cfg.specific_heat:g} J/(kg·K)  "
              f"密度 {cfg.density:g} kg/m³\n")
    buf.write("# 列: t_rel_s(相对时间零点) temp_c\n")

    w = csv.writer(buf)
    w.writerow(["t_rel_s", "temp_c"])
    for i in range(t.size):
        w.writerow([f"{t[i]:.6f}", f"{float(temp_c[i]):.2f}"])
    return buf.getvalue().encode("utf-8-sig")


def _render_png(t: np.ndarray, temp_c: np.ndarray, res: AnalysisResult,
                unit: str, smooth_level: float,
                cfg: ExperimentConfig | None = None,
                width: int = 2400, height: int = 1350) -> bytes:
    """用 matplotlib 离屏渲染标注后的曲线（高清、可直接放进论文）。

    为什么不再用 pyqtgraph 的 ImageExporter：它按场景尺寸出图，实测请求
    1800 px 宽只拿到 1333 px，再被拉伸回目标尺寸——这正是"导出的 png 很糊"
    的根源。matplotlib 可以按 dpi 精确出图，还能方便地排图题、图例、
    边距，符合论文插图习惯。

    图上包含：图标题（正上方）、坐标轴标题与单位、图例、峰点标注、
    区间横线（与界面同一套 :func:`lfa.experiment.band_layout`）。
    """
    import io as _io

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import ScalarFormatter

    from .store import envelope

    y = ex.smooth_series(temp_c, smooth_level)
    y_disp = y + 273.15 if unit == "K" else y
    unit_sym = "K" if unit == "K" else "°C"

    # 中文字体：Windows 自带微软雅黑，缺失时依次退到黑体/宋体
    matplotlib.rcParams["font.sans-serif"] = [
        "Microsoft YaHei", "SimHei", "SimSun", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False

    dpi = 200
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    # 边距留足：横轴标题与刻度数字曾经叠在一起，就是因为下边距太小
    fig.subplots_adjust(left=0.075, right=0.975, top=0.885, bottom=0.115)
    ax = fig.add_subplot(111)

    xs, ys = envelope(t, y_disp, width * 2)
    ax.plot(xs, ys, color="#c0392b", linewidth=1.6, label="温升曲线")

    n_rows = 0
    if res.ok and res.intervals:
        n_rows = min(len(res.intervals), ex.MAX_BANDS)

    lo, hi = float(np.min(ys)), float(np.max(ys))
    ylo, yhi, rows = ex.band_layout(lo, hi, n_rows)

    if res.ok:
        px, py = [], []
        for p in res.peaks:
            px.append(p.t)
            py.append(float(p.temp_c + (273.15 if unit == "K" else 0.0)))
        if px:
            ax.plot(px, py, linestyle="none", marker="o", markersize=8,
                    markerfacecolor="#1f6feb", markeredgecolor="white",
                    markeredgewidth=1.2, label="温度峰点", zorder=5)
            for i, (x, yy) in enumerate(zip(px, py), 1):
                # 用 mathtext 写下标：微软雅黑没有 ₁₂ 这些字形，
                # 直接写会渲染成方块。$M_1$ 由 matplotlib 的数学字体排。
                ax.annotate(f"$M_{i}$ ({x:.3f} s, {yy:.2f} {unit_sym})",
                            xy=(x, yy), xytext=(6, 10),
                            textcoords="offset points",
                            fontsize=9, color="#1f6feb")

        # 区间横线：与界面完全同一套排布（全部同高，分段 + 共享端头）
        if rows:
            y_line = rows[0]                 # 所有横线同高
            span = max(hi - lo, 1e-9)
            tick_h = span * 0.030
            ext_bottom = y_line - span * 0.10

            for iv in res.intervals[:len(rows)]:
                ax.plot([iv.t0, iv.t1], [y_line, y_line],
                        color="#333333", linewidth=1.3, zorder=4)

            bounds = sorted({float(iv.t0) for iv in res.intervals[:len(rows)]}
                            | {float(iv.t1) for iv in res.intervals[:len(rows)]})
            for xx in bounds:
                ax.plot([xx, xx], [y_line - tick_h, y_line + tick_h],
                        color="#333333", linewidth=1.3, zorder=4)
                # 向下的虚线延伸：让人看出这条横线管的是哪段时间
                ax.plot([xx, xx], [ext_bottom, y_line - tick_h],
                        color="#999999", linewidth=0.8,
                        linestyle=(0, (4, 3)), zorder=3)

            for iv in res.intervals[:len(rows)]:
                ax.text((iv.t0 + iv.t1) / 2.0, y_line + tick_h * 1.5,
                        f"{_label_tex(iv.label)}  {iv.duration:.4f} s",
                        ha="center", va="bottom", fontsize=9, color="#222222")

    ax.set_xlim(min(0.0, float(t[0])) - 0.01 * max(float(t[-1]), 1e-3),
                float(t[-1]) * 1.01)
    ax.set_ylim(ylo, yhi)
    ax.set_xlabel("时间 / s", fontsize=12)
    ax.set_ylabel(f"温度 / {unit_sym}", fontsize=12)
    ax.tick_params(labelsize=10)
    # 刻度不要用 1e-3 这种科学计数法，直接给小数
    ax.xaxis.set_major_formatter(ScalarFormatter(useOffset=False))
    ax.yaxis.set_major_formatter(ScalarFormatter(useOffset=False))
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(False)

    # 图标题放正上方（论文插图的标准位置）
    title = "温升曲线 — LFA 变温导热系数测量"
    if cfg is not None and cfg.material:
        title += f"（{cfg.material}）"
    fig.suptitle(title, fontsize=14, y=0.965)

    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(handles, labels, loc="lower right", fontsize=9,
                  frameon=False)

    buf = _io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def export_result(zip_path: str, cfg: ExperimentConfig, res: AnalysisResult,
                  t_all: np.ndarray, temp_all: np.ndarray, unit: str,
                  device_note: str = "", smooth_level: float = 0.0) -> str:
    """写 zip，返回实际写入的文件路径。"""
    zip_path = os.path.abspath(zip_path)
    folder = os.path.splitext(os.path.basename(zip_path))[0]

    # 只把展示窗口内的数据画进图里；CSV 保留 A→B 全段
    if res.window is not None:
        keep = (t_all >= res.window[0]) & (t_all <= res.window[1])
    else:
        keep = np.ones(t_all.size, dtype=bool)
    t_show, y_show = t_all[keep], temp_all[keep]
    if t_show.size < 2:
        t_show, y_show = t_all, temp_all

    png = _render_png(t_show, y_show, res, unit, smooth_level, cfg)
    csv_bytes = _csv_bytes(t_all, temp_all, cfg)
    json_text = ex.config_to_json(cfg, res, unit)
    md_text = ex.build_conclusion(cfg, res, unit)
    if device_note:
        md_text += f"\n---\n\n采集设备：{device_note}\n"

    os.makedirs(os.path.dirname(zip_path) or ".", exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{folder}/raw_AB.csv", csv_bytes)
        z.writestr(f"{folder}/config.json", json_text.encode("utf-8"))
        z.writestr(f"{folder}/curve.png", png)
        z.writestr(f"{folder}/实验结论.md", md_text.encode("utf-8"))
    return zip_path


def _sub(i: int) -> str:
    return str(i).translate(str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉"))


def _label_tex(label: str) -> str:
    """把 ``AM₁`` / ``M₁M₂`` 这类标签转成 matplotlib 数学式。

    微软雅黑没有下标字形（₁₂），直接画会变成方块。matplotlib 的数学字体
    能正确排下标，代价是要用 ``$...$`` 包起来并把下标写成 ``_n``。

    ``AM₁`` → ``$A M_1$``；``M₁B′`` → ``$M_1 B'$``；``M₁M₂`` → ``$M_1 M_2$``
    """
    sup = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
    out = []
    i = 0
    while i < len(label):
        ch = label[i]
        if ch in "₀₁₂₃₄₅₆₇₈₉":
            out.append("_" + ch.translate(sup))
        elif ch == "′":                      # 撇号用数学式里的 prime
            out.append("'")
        elif ch == "A":
            out.append("A ")
        else:
            out.append(ch)
        i += 1
    return "$" + "".join(out).rstrip() + "$"


def load_result(zip_path: str) -> dict:
    """读取 zip（供将来「分析」页使用）。返回 {'config','analysis','csv','md'}。"""
    import json

    out: dict = {}
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        for n in names:
            base = os.path.basename(n)
            if base == "config.json":
                out["config"] = json.loads(z.read(n).decode("utf-8"))
            elif base == "raw_AB.csv":
                out["csv"] = z.read(n)
            elif base.endswith(".md"):
                out["md"] = z.read(n).decode("utf-8")
    return out


__all__ = ["export_result", "load_result", "NS"]
