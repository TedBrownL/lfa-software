"""CSV 导出。

列设计对齐后续 LFA 分析的需要：

* ``t_wall_ms``   —— 绝对墙钟时间（毫秒精度，带日期），人看的；
* ``t_rel_s``     —— 相对会话起点的秒数（float64，全精度），拟合用；
* ``t_perf_ns``   —— 单调时钟原始时间戳，做完备复算用；
* ``temp_c``      —— 摄氏温度。

时间零点（激光触发）单独写进文件头注释，后续接入触发通道后自动填。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass

import numpy as np

from .store import TraceStore
from .timebase import NS, format_wall_iso


@dataclass
class ExportMeta:
    device: str = ""
    serial_number: str = ""
    firmware: str = ""
    note: str = ""
    zero_wall_ns: int | None = None


def export_csv(path: str, store: TraceStore, meta: ExportMeta) -> int:
    """写出全部样本，返回写出的行数。"""
    n = len(store)
    if n == 0:
        return 0
    t_perf = store.t_perf_all
    t_wall = store.t_wall_all
    temp = store.temp_all
    t0_perf = int(t_perf[0])
    t0_wall = int(t_wall[0])

    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        fh.write(f"# LFA 温度采集记录\n")
        fh.write(f"# 设备: {meta.device}  序列号: {meta.serial_number}  "
                 f"固件: {meta.firmware}\n")
        fh.write(f"# 会话起点(墙钟): {format_wall_iso(t0_wall)}\n")
        if meta.zero_wall_ns is not None:
            fh.write(f"# 时间零点(激光触发): {format_wall_iso(meta.zero_wall_ns)}\n")
        if meta.note:
            fh.write(f"# 备注: {meta.note}\n")
        fh.write("# 列: t_wall_ms(绝对时间) t_rel_s(相对会话起点) "
                 "t_perf_ns(单调时钟) temp_c\n")

        w = csv.writer(fh)
        w.writerow(["t_wall_ms", "t_rel_s", "t_perf_ns", "temp_c"])
        # 逐行写 20 万行约 0.3 s，可接受；用 numpy 组行避免 Python 层格式化开销
        rel = (t_perf - t0_perf) / NS
        for i in range(n):
            w.writerow([
                _wall_str(int(t_wall[i])),
                f"{rel[i]:.6f}",
                int(t_perf[i]),
                f"{float(temp[i]):.2f}",
            ])
    return n


def _wall_str(wall_ns: int) -> str:
    """'YYYY-MM-DD HH:MM:SS.mmm'，直接给 Excel/Origin 用。"""
    return format_wall_iso(wall_ns)


def as_arrays(store: TraceStore) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """供后续分析模块直接取 (t_rel_s, t_perf_ns, temp_c)。"""
    t_perf = store.t_perf_all.copy()
    temp = store.temp_all.astype(np.float64)
    if t_perf.size == 0:
        return np.empty(0), t_perf, temp
    rel = (t_perf - t_perf[0]) / NS
    return rel, t_perf, temp
