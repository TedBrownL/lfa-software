"""真机端到端测试：走一次完整的「开始实验 → 结束实验 → 结果窗口」。

不需要手动遮挡。没有温升时，软件应当判定为"实验异常"并如实提示——
这本身就是一条必须验证的路径（也是用户最可能遇到的路径）。

同时验证：
1. 零点与首点的实际时间差（这是机械遮挡方案要解决的量，必须量化出来）；
2. 采集中界面不卡（刷新帧数）；
3. 停止后数据完整、无缺口；
4. 结果窗口、导出 zip 在真机数据上可用。

用法：
    D:/MINICONDA/envs/casimir/python.exe tools/real_experiment_test.py [时长秒]
"""

from __future__ import annotations

import os
import sys
import time
import zipfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lfa.main_window import MainWindow

_fail = []


def check(cond, name, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (f"   [{detail}]" if detail else ""))
    if not cond:
        _fail.append(name)


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    app = QApplication.instance() or QApplication(sys.argv)

    win = MainWindow()
    win.show()
    app.processEvents()

    port = win.cb_port.currentData()
    print(f"串口: {port}")
    check(port == "COM10", "选中 COM10", str(port))

    print("[1] 设备设置自动读取")
    win.refresh_device_settings(auto=True)
    app.processEvents()
    st = getattr(win, "_signal_state", None)
    print("   状态:", st)
    check(st is not None, "读到设备设置")
    if st:
        check(st[0] is not None and st[0] <= 20, "平均时间适合 LFA", f"{st[0]} ms")

    print(f"[2] 开始实验（采集 {duration:g} s，无遮挡 → 预期判异常）")
    win.sp_pulsen.setValue(1)
    t_start = time.perf_counter()
    win._start_experiment()
    app.processEvents()

    # 记录零点与首点的差
    frames = {"n": 0}
    stop_at = t_start + duration

    def tick():
        frames["n"] += 1
        if time.perf_counter() >= stop_at and win.exp_cfg is not None:
            timer.stop()
            win._stop_experiment()

    timer = QTimer()
    timer.setInterval(16)
    timer.timeout.connect(tick)
    timer.start()

    # 等实验结束（结果窗口弹出会清空 exp_cfg）
    guard = time.perf_counter() + duration + 25
    while time.perf_counter() < guard:
        app.processEvents()
        if win.result_win is not None:
            break
        time.sleep(0.005)

    elapsed = time.perf_counter() - t_start
    check(win.result_win is not None, "结果窗口已自动弹出",
          f"用时 {elapsed:.1f} s")
    if win.result_win is None:
        win.close()
        print("失败：结果窗口没弹出")
        return 1

    rw = win.result_win
    n = len(rw.t_all)
    print(f"[3] 数据完整性")
    print(f"    点数 {n:,}")
    check(n > 1000, "采到足够数据", f"{n} 点")
    dur = float(rw.t_all[-1] - rw.t_all[0])
    rate = (n - 1) / dur if dur > 0 else 0
    print(f"    时长 {dur:.2f} s  速率 {rate:,.0f} Hz")
    check(rate > 5000, "采样率正常", f"{rate:,.0f} Hz")

    # 时间零点与首点的差：这是"按下按钮 → 真正开始采样"的延迟
    print("[4] 零点对齐（机械遮挡方案要压掉的就是这个）")
    zero_wall = rw.cfg.zero_wall_ns
    t0_rel = float(rw.t_all[0])
    print(f"    零点(墙钟)   {zero_wall}")
    print(f"    首点相对零点 {t0_rel*1000:.0f} ms  ← 这就是启动延迟")
    check(t0_rel >= 0, "首点不早于零点", f"{t0_rel*1000:.0f} ms")
    # 真机实测稳定在 ~300 ms，其中 ~280 ms 是 spawn 子进程的固定开销。
    # 这个值必须如实暴露，不能被"看起来对齐"掩盖。
    check(t0_rel < 1.0, "启动延迟 <1 s", f"{t0_rel*1000:.0f} ms")
    check(abs(rw.cfg.start_delay_s - t0_rel) < 1e-6,
          "启动延迟已记录进配置", f"{rw.cfg.start_delay_s*1000:.0f} ms")
    if t0_rel > 0.6:
        print(f"    注意：本次延迟 {t0_rel*1000:.0f} ms 偏大（基准 ~300 ms），"
              f"可能是首次启动或系统繁忙")

    # 数据缺口检查：相邻点最大间隔
    dt = np.diff(rw.t_all)
    print(f"    dt 中位 {np.median(dt)*1e6:.1f} us  max {dt.max()*1e3:.2f} ms")
    check(dt.max() < 0.05, "无 >50 ms 的数据缺口", f"{dt.max()*1e3:.2f} ms")

    print("[5] 界面刷新（采集期间）")
    fps = frames["n"] / duration if duration > 0 else 0
    print(f"    刷新 {frames['n']} 帧 / {duration:.1f} s = {fps:.0f} FPS")
    check(fps > 30, "采集期间界面刷新 >30 FPS", f"{fps:.0f} FPS")

    print("[6] 分析判定（无遮挡应为异常）")
    print(f"    最大温升 {rw.res.delta_t:.3f} °C")
    print(f"    判定 ok={rw.res.ok}  {rw.res.reason}")
    check(not rw.res.ok, "未遮挡时正确判为实验异常")
    check(rw.res.window is None, "异常时不截取窗口")
    check(len(rw._bands) == 0, "异常时无区间标注")
    check("异常" in rw.lbl_result.text(), "左栏显示异常提示")

    print("[7] 导出 zip（真机数据）")
    out = os.path.join("data", "result_realtest.zip")
    from lfa.result_export import export_result, load_result

    p = export_result(out, rw.cfg, rw.res, rw.t_all, rw.temp_all,
                      "C", smooth_level=0.2)
    check(os.path.exists(p), "zip 生成")
    print(f"    大小 {os.path.getsize(p)/1024:.0f} KB")
    with zipfile.ZipFile(p) as z:
        names = [os.path.basename(x) for x in z.namelist()]
    for want in ("raw_AB.csv", "config.json", "curve.png", "实验结论.md"):
        check(want in names, f"含 {want}")
    loaded = load_result(p)
    check(loaded["config"]["analysis"]["ok"] is False, "config 记录了异常结论")
    check(loaded["config"]["config"]["n_samples"] == n, "点数一致")

    rw.close()
    app.processEvents()
    check(win.result_win is None, "关闭后主界面解锁")
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
