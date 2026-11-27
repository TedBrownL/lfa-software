"""用用户手动测试的真实数据出图，验证新出图路径在有峰的真实数据上正常。

数据来源：data/result_<编号>.zip 里的 raw_AB.csv（用户第一次成功的手动实验）。
这比合成数据更有说服力：真实的噪声、真实的启动延迟、真实的峰。
"""

import csv
import io
import os
import sys
import zipfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfa import experiment as ex
from lfa.experiment import ExperimentConfig
from lfa.result_export import _render_png

ZIP = sys.argv[1] if len(sys.argv) > 1 else "data/result_20261125_203627.zip"
OUT = "data/_real_png.png"


def load_zip(path):
    with zipfile.ZipFile(path) as z:
        csv_name = [n for n in z.namelist() if n.endswith("raw_AB.csv")][0]
        text = z.read(csv_name).decode("utf-8-sig")
        import json
        cfg_name = [n for n in z.namelist() if n.endswith("config.json")][0]
        conf = json.loads(z.read(cfg_name).decode("utf-8"))
    t, y = [], []
    for row in csv.reader(io.StringIO(text)):
        if not row or row[0].startswith("#") or row[0] == "t_rel_s":
            continue
        t.append(float(row[0]))
        y.append(float(row[1]))
    return np.array(t), np.array(y), conf


def main():
    t, y, conf = load_zip(ZIP)
    cfg = ExperimentConfig.from_dict(conf.get("config", conf))
    print(f"读入 {t.size:,} 点，{t[0]:.3f} → {t[-1]:.3f} s，材料 {cfg.material}")

    res = ex.analyze(t, y, cfg)
    print(f"分析：ok={res.ok} 峰={len(res.peaks)} 温升={res.delta_t:.2f} °C")
    if res.reason:
        print(f"      原因 {res.reason[:60]}")
    for iv in res.intervals:
        print(f"      {iv.label}: {iv.t0:.3f} → {iv.t1:.3f} ({iv.duration:.3f} s)")

    if res.window is not None:
        lo, hi = res.window
        keep = (t >= lo) & (t <= hi)
    else:
        keep = np.ones(t.size, dtype=bool)
    ts, ys = t[keep], y[keep]

    png = _render_png(ts, ys, res, "C", 0.5, cfg)
    with open(OUT, "wb") as fh:
        fh.write(png)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.image as mpimg
    img = mpimg.imread(OUT)
    print(f"PNG {img.shape[1]}x{img.shape[0]}, {len(png)/1024:.0f} KB → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
