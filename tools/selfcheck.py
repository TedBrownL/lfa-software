"""无界面自检：接真实测温仪跑 N 秒，导出 CSV，检查采样率与时间戳质量。

用法：
    python tools/selfcheck.py            # 默认 COM10, 5 s
    python tools/selfcheck.py COM10 5
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from lfa.acquisition import AcquisitionConfig, HeadlessAcquisition
from lfa.export import ExportMeta, export_csv
from lfa.store import TraceStore
from lfa.timebase import NS, TimeBase


def main(port: str, seconds: float) -> int:
    tb = TimeBase()
    store = TraceStore()

    cfg = AcquisitionConfig(port=port, block_size=100)
    acq = HeadlessAcquisition(cfg, tb)
    acq.start(lambda b: store.append_block(b.t_perf_ns, b.t_wall_ns, b.temp_c))

    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end and acq.is_running:
        time.sleep(0.05)
    acq.stop()
    if acq.error:
        print("FAILED:", acq.error)

    n = len(store)
    if n == 0:
        print("没有采到数据")
        return 1

    info = acq.info
    t = store.t_perf_all
    dt = np.diff(t) / 1e3                       # us
    dur = (t[-1] - t[0]) / NS
    print(f"设备: {info}")
    print(f"样本数 {n:,}  时长 {dur:.2f} s  平均速率 {n / max(dur, 1e-9):,.0f} Hz")
    print(f"dt 中位 {np.median(dt):.1f} us  p1 {np.percentile(dt, 1):.1f} us  "
          f"p99 {np.percentile(dt, 99):.1f} us  max {dt.max():.1f} us")

    # 时间戳单调性
    print("单调递增:", bool(np.all(np.diff(t) > 0)))

    # 墙钟与单调时钟的一致性：两者差分应稳定在常数附近
    wall = store.t_wall_all
    d = np.diff(wall) - np.diff(t)
    print(f"墙钟-单调 差分残差: max|res| = {np.abs(d).max() / 1e3:.1f} us")

    # 温度
    temp = store.temp_all
    print(f"温度 {temp.min():.1f} .. {temp.max():.1f} °C  (首 {temp[0]:.1f}, "
          f"末 {temp[-1]:.1f})")

    out = os.path.join(tempfile.gettempdir(), "lfa_selfcheck.csv")
    meta = ExportMeta(device="Optris CT4ML",
                      serial_number=info.serial_number if info else "",
                      firmware=info.firmware if info else "")
    cnt = export_csv(out, store, meta)
    size = os.path.getsize(out)
    print(f"导出 {cnt:,} 行 -> {out} ({size / 1024:.0f} KB)")
    with open(out, encoding="utf-8-sig") as fh:
        for i, line in enumerate(fh):
            if i > 9:
                break
            print("   ", line.rstrip())
    return 0


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else "COM10"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    sys.exit(main(port, secs))
