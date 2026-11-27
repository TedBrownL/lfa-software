"""验证「导出数据（zip）」按钮的完整链路（含 QFileDialog 之后的全部代码）。

关键点：上一版 result_window 漏了 `import time`，而 _export 里用了
time.strftime 生成默认文件名。这个脚本直接调用 _export()，把
QFileDialog.getSaveFileName 打桩成返回一个固定路径，从而真正执行到
那段代码（而不是绕过它）。

用法：
    QT_QPA_PLATFORM=offscreen python tools/export_check.py
"""

import os
import sys
import zipfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from lfa import experiment as ex
from lfa.experiment import ExperimentConfig
from lfa.result_window import ResultWindow

FS = 6500.0
_fail = []


def check(cond, name, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (f"   [{detail}]" if detail else ""))
    if not cond:
        _fail.append(name)


def synth(n_pulse=1, dur=20.0):
    rng = np.random.default_rng(5)
    t = np.arange(int(dur * FS)) / FS
    y = np.full(t.size, 25.0)
    for k in range(n_pulse):
        t0 = 13.0 + k * 3.0
        m = t >= t0
        y[m] += 2.3 * np.exp(-(t[m] - t0) / 0.8)
    return t, y + rng.normal(0, 0.08, t.size)


def main() -> int:
    app = QApplication(sys.argv)

    out_dir = os.path.abspath("data")
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, "_export_check.zip")
    if os.path.exists(target):
        os.remove(target)

    # 1) 打桩文件对话框：返回目标路径（模拟用户点"保存"）
    QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (target, "ZIP (*.zip)"))

    # 2) 打桩错误弹窗：把内容记下来，别让模态框卡住无头测试
    errors = []
    QMessageBox.critical = staticmethod(
        lambda parent, title, text: errors.append(f"{title}: {text}")
        or QMessageBox.StandardButton.Ok)

    t, y = synth(1)
    cfg = ExperimentConfig(pulse_count=1, pulse_width_s=0.1,
                           material="不锈钢")
    cfg.started_at = "2026-11-25 20:33:28"
    cfg.ended_at = "2026-11-25 20:33:46"
    cfg.n_samples = int(t.size)
    cfg.sample_rate_hz = 6500.0
    cfg.start_delay_s = 0.278
    res = ex.analyze(t, y, cfg)

    rw = ResultWindow(t, y, cfg, res, unit="C", device_note="probe")
    rw.show()
    app.processEvents()

    print("[1] 点「导出数据（zip）」")
    try:
        rw._export()
    except Exception as exc:                                      # noqa: BLE001
        check(False, "导出未抛异常", f"{type(exc).__name__}: {exc}")
    else:
        check(True, "导出未抛异常")

    check(not errors, "没有弹出错误框",
          "; ".join(errors) if errors else "")
    check(os.path.exists(target), "zip 已生成",
          f"{os.path.getsize(target) if os.path.exists(target) else 0} B")

    if os.path.exists(target):
        with zipfile.ZipFile(target) as z:
            names = [os.path.basename(x) for x in z.namelist()]
            bad = z.testzip()
            png = [x for x in z.namelist() if x.endswith("curve.png")][0]
            data = z.read(png)
        check(bad is None, "zip 完整（无坏块）", str(bad))
        for want in ("raw_AB.csv", "config.json", "curve.png", "实验结论.md"):
            check(want in names, f"包内含 {want}")
        check(len(data) > 80000, "curve.png 足够大（高清）",
              f"{len(data)//1024} KB")

    print("[2] 用户没写 .zip 后缀时自动补")
    target2 = os.path.join(out_dir, "_export_check_noext")
    QFileDialog.getSaveFileName = staticmethod(
        lambda *a, **k: (target2, "ZIP (*.zip)"))
    rw._export()
    got = target2 + ".zip"
    check(os.path.exists(got), "自动补 .zip", got)

    print("[3] 用户取消（空路径）不应报错")
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
    errors.clear()
    rw._export()
    check(not errors, "取消时无错误弹窗")

    rw.close()
    app.processEvents()

    # 清理
    for p in (target, got):
        if os.path.exists(p):
            os.remove(p)

    print()
    if _fail:
        print(f"失败 {len(_fail)} 项：" + "、".join(_fail))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
