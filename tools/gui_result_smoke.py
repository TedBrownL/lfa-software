"""实验结果流程的端到端测试（不需要真机）。

覆盖：
1. 主界面三栏布局是否就位（左栏、实验配置栏、曲线）；
2. 材料预设联动、单位切换；
3. 用合成数据走「开始实验→结束实验→结果窗口」全流程；
4. 结果窗口的截取、标注、平滑；
5. zip 导出与回读。

用法：
    D:/MINICONDA/envs/casimir/python.exe tools/gui_result_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from lfa import experiment as ex
from lfa.experiment import ExperimentConfig
from lfa.main_window import MainWindow

_fail = []


def check(cond, name, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (f"   [{detail}]" if detail else ""))
    if not cond:
        _fail.append(name)


def synth(n_pulse=1, fs=6500.0, dur=5.0, rise=1.5, noise=0.12, gap=0.9,
          seed=7):
    rng = np.random.default_rng(seed)
    n = int(dur * fs)
    t = np.arange(n) / fs
    y = np.full(n, 25.0)
    for k in range(n_pulse):
        t0 = 0.8 + k * gap
        m = t >= t0
        y[m] += rise * np.exp(-(t[m] - t0) / 0.30)
    return t, y + rng.normal(0, noise, n)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow()
    win.show()
    app.processEvents()

    print("[1] 主界面布局")
    check(win.cb_unit.count() == 2, "温度单位有 °C / K 两项")
    check(win.sp_ambient.value() == 300.0, "环境温度默认 300 K")
    check(win.sp_power.value() == 10.0, "激光功率默认 10 W")
    check(abs(win.sp_pulsew.value() - 0.1) < 1e-9, "脉冲时间默认 0.1 s")
    check(win.sp_pulsen.value() == 1, "脉冲数默认 1")
    check(win.sp_pulsen.minimum() == 1, "脉冲数下限为 1")
    check(win.sp_heat.value() == 500.0, "比热容默认 500")
    check(win.sp_density.value() == 7900.0, "密度默认 7900")
    check(win.sp_radius.value() == 10.0, "测温半径默认 10 mm")
    check(win.btn_exp_start.isEnabled() and not win.btn_exp_stop.isEnabled(),
          "初始：可开始、不可结束")

    print("[2] 材料预设联动")
    win.cb_material.setCurrentText("铜")
    app.processEvents()
    check(abs(win.sp_heat.value() - 385.0) < 1e-9
          and abs(win.sp_density.value() - 8960.0) < 1e-9,
          "选「铜」→ 385 / 8960",
          f"{win.sp_heat.value()} / {win.sp_density.value()}")
    win.cb_material.setCurrentText("铝")
    app.processEvents()
    check(abs(win.sp_heat.value() - 900.0) < 1e-9, "选「铝」→ 900")
    win.cb_material.setCurrentText("不锈钢")
    app.processEvents()
    check(abs(win.sp_heat.value() - 500.0) < 1e-9
          and abs(win.sp_density.value() - 7900.0) < 1e-9, "选「不锈钢」→ 500/7900")

    # 自定义材料名不应覆盖已填数值
    win.sp_heat.setValue(777.0)
    win.cb_material.setCurrentText("钛合金")
    app.processEvents()
    check(abs(win.sp_heat.value() - 777.0) < 1e-9,
          "自定义材料名不覆盖用户填的参数", f"{win.sp_heat.value()}")

    print("[3] 单位切换")
    win.cb_unit.setCurrentIndex(1)      # K
    app.processEvents()
    check(win.current_unit == "K", "current_unit 切到 K")
    check("K" in win.plot.plot.getPlotItem().getAxis("left").labelText, "纵轴标注为 K")
    win.cb_unit.setCurrentIndex(0)
    app.processEvents()
    check(win.current_unit == "C", "切回 °C")

    print("[4] 实验全流程（合成数据）")
    n_pulse = 2
    win.sp_pulsen.setValue(n_pulse)
    win.cb_material.setCurrentText("不锈钢")
    t, y = synth(n_pulse)

    # 绕过真机：直接灌入 store 并伪造零点
    win.store.clear()
    perf = np.int64(np.round(t * 1e9)) + 1_000_000_000_000
    wall = perf + 100_000
    win.store.append_block(perf, wall, y.astype(np.float32))
    win.zero_wall_ns = int(wall[0])
    win.exp_cfg = win._collect_config()
    win.exp_cfg.zero_wall_ns = int(wall[0])

    # 让 zero_perf_ns() 与该零点一致
    ap, aw = win.timebase.anchor()
    win.zero_wall_ns = int(wall[0])
    cfg = win.exp_cfg
    n = len(win.store)
    tp = win.store.t_perf_all
    tt = (tp - win.zero_perf_ns()) / 1e9
    cfg.n_samples = n
    cfg.sample_rate_hz = (n - 1) / float(tt[-1] - tt[0])
    res = ex.analyze(tt, win.store.temp_all.astype(np.float64), cfg)
    check(res.ok, "合成 2 脉冲被正确识别", f"peaks={len(res.peaks)}")
    check(len(res.peaks) == n_pulse, f"识别到 {n_pulse} 个峰")
    check(res.window is not None, "窗口被截取")

    from lfa.result_window import ResultWindow

    rw = ResultWindow(tt, win.store.temp_all.astype(np.float64), cfg, res,
                      unit="C", parent=win)
    win.result_win = rw
    rw.show()
    app.processEvents()
    check(rw.isVisible(), "结果窗口打开")
    check(len(rw._bands) > 0, "标注横线已绘制", f"{len(rw._bands)} 个图元")
    check(len(rw._markers) > 0, "峰点标记已绘制", f"{len(rw._markers)} 个图元")
    check(rw.sl_smooth.value() == 50, "默认平滑度 0.5",
          f"{rw.sl_smooth.value()}")

    print("[5] 平滑不改变标注")
    before = [(p.t, p.temp_c) for p in rw.res.peaks]
    rw.sl_smooth.setValue(60)
    app.processEvents()
    after = [(p.t, p.temp_c) for p in rw.res.peaks]
    check(before == after, "拖平滑后峰点不变")
    check(len(rw._bands) > 0, "平滑后标注仍在")
    ydata = rw._curve.yData
    check(ydata is not None and np.all(np.isfinite(ydata)), "平滑后曲线数据有限")
    rw.sl_smooth.setValue(50)
    app.processEvents()

    print("[6] 导出 zip 并回读")
    tmp = tempfile.mkdtemp(prefix="lfa_test_")
    zpath = os.path.join(tmp, "result_test.zip")
    from lfa.result_export import export_result, load_result

    out = export_result(zpath, cfg, rw.res, rw.t_all, rw.temp_all, "C",
                        smooth_level=0.3)
    check(os.path.exists(out), "zip 已生成")
    check(os.path.getsize(out) > 5000, "zip 非空", f"{os.path.getsize(out)} B")
    with zipfile.ZipFile(out) as z:
        names = [os.path.basename(x) for x in z.namelist()]
    for want in ("raw_AB.csv", "config.json", "curve.png", "实验结论.md"):
        check(want in names, f"包内含 {want}")
    loaded = load_result(out)
    check("config" in loaded, "config.json 可解析")
    check(loaded["config"]["config"]["pulse_count"] == n_pulse, "脉冲数一致")
    check(loaded["config"]["analysis"]["ok"] is True, "分析结论为成功")
    check(len(loaded["config"]["analysis"]["peaks"]) == n_pulse, "峰数一致")
    csv_lines = loaded["csv"].decode("utf-8-sig").splitlines()
    check(len(csv_lines) > 1000, "CSV 行数正常", f"{len(csv_lines)}")

    print("[7] 异常实验（无温升）")
    flat = np.full(2000, 25.0) + np.random.default_rng(1).normal(0, 0.03, 2000)
    tf = np.arange(2000) / 6500.0
    cfg2 = ExperimentConfig(pulse_count=1)
    r2 = ex.analyze(tf, flat, cfg2)
    check(not r2.ok, "平线判为异常")
    check(r2.window is None, "异常时不截取")
    rw2 = ResultWindow(tf, flat, cfg2, r2, unit="C", parent=win)
    rw2.show()
    app.processEvents()
    check(len(rw2._bands) == 0 and len(rw2._markers) == 0, "异常时无标注")
    check("异常" in rw2.lbl_result.text(), "左栏提示异常")
    rw2.close()

    # 清理
    for f in ("raw_AB.csv", "config.json", "curve.png", "实验结论.md"):
        pass
    rw.close()
    app.processEvents()
    check(win.result_win is None, "关闭结果窗口后主界面解除锁定")
    check(win.btn_exp_start.isEnabled(), "可再次开始实验")

    win.close()
    print()
    if _fail:
        print(f"失败 {len(_fail)} 项：" + "、".join(_fail))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
