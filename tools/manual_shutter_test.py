"""手动遮挡实测：验证"一个激光脉冲 → 一个温度峰"能被软件正确识别。

⚠ 挡片位置很关键
    挡片必须挡在**激光光路上**，即决定"激光能不能打到样品"；
    绝不能挡在**测温仪视场里**（挡在测温仪前面只会让读数骤降，
    那是"看不到样品"，不是样品升温，软件会（且应该）判为异常）。

物理过程
    激光器是 1064 nm **连续**激光，LFA 需要的脉冲靠"让光短暂通过"来造：
    * 平时：挡片遮住光路，样品不被加热，读数停在环境温度；
    * 脉冲：把挡片移开约 ``pulse_width_s``（0.1 s 量级），光打到样品上；
    * 之后：立刻挡回去，样品自然冷却，读数回落。

    于是曲线上出现"快升 → 慢降"，这正是 LFA 要的形态。一个脉冲造一个峰，
    配置几个脉冲就做几次。

用法：
    D:/MINICONDA/envs/casimir/python.exe tools/manual_shutter_test.py 12 2
        → 采集 12 秒，做 2 次脉冲（每次挡片移开 0.5 s，见 pulse_width 参数）

    第三个参数可改脉冲宽度：
    D:/MINICONDA/envs/casimir/python.exe tools/manual_shutter_test.py 12 2 0.2
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfa.experiment import MIN_RISE_C as MIN_RISE


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    pulses = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    pulse_w = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5

    from PySide6.QtWidgets import QApplication

    from lfa.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow()
    win.show()
    app.processEvents()

    win.sp_pulsen.setValue(pulses)
    win.sp_pulsew.setValue(pulse_w)

    print("=" * 68)
    print(f"采集 {duration:g} s，做 {pulses} 次激光脉冲（每次 {pulse_w:g} s）")
    print()
    print("  挡片放在【激光光路上】——决定光能不能打到样品。")
    print("  不要在测温仪前面晃，那样只会让读数掉下去。")
    print()
    print(f"  开始后，每隔约 {max(1.0, duration / (pulses + 1)):.0f} s")
    print(f"  把挡片移开 {pulse_w:g} s 再挡回去。")
    print("=" * 68)

    win._start_experiment()
    app.processEvents()

    zero = win.exp_cfg.zero_wall_ns if win.exp_cfg else 0
    print(f"\n>>> 已开始（零点 {zero}），请开始做脉冲 <<<\n")

    t0 = time.perf_counter()
    last_report = t0
    while time.perf_counter() - t0 < duration:
        app.processEvents()
        time.sleep(0.01)
        now = time.perf_counter()
        if now - last_report > 2.0:
            last_report = now
            n = len(win.store)
            temp = float(win.store.temp_all[-1]) if n else float("nan")
            print(f"    [{now-t0:4.1f}s]  {n:6,} 点   当前 {temp:.2f} °C")

    print("\n>>> 结束实验 <<<")
    win._stop_experiment()

    deadline = time.perf_counter() + 30
    while time.perf_counter() < deadline and win.result_win is None:
        app.processEvents()
        time.sleep(0.005)

    if win.result_win is None:
        print("结果窗口未弹出")
        return 1

    rw = win.result_win
    r = rw.res
    print()
    print("=" * 68)
    print(f"数据点      {len(rw.t_all):,}")
    print(f"温升幅度    {r.delta_t:.3f} °C   (噪声 σ={r.noise_c:.3f})")
    print(f"判定        ok={r.ok}")
    if r.ok:
        print(f"识别到      {len(r.peaks)} 个峰（配置 {pulses} 个）")
        for i, p in enumerate(r.peaks, 1):
            print(f"   M{i}  t={p.t:.3f}s  T={p.temp_c:.2f}°C")
        for iv in r.intervals:
            print(f"   {iv.label}: {iv.duration:.3f} s")
        if r.window:
            print(f"展示窗口    {r.window[0]:.3f} → {r.window[1]:.3f} s")
        if r.window_clamped:
            print("   ⚠ 峰太靠近结束时刻，B′ 被截到 B —— 下次请晚一点结束实验")
    else:
        print(f"原因        {r.reason}")
        print()
        print("排查顺序：")
        print("  1. 挡片是不是放在了测温仪视场里？那会让读数下陷，不是升温。")
        print("  2. 正好是做峰的时刻吗？波形要「快升 → 慢降」，而不是方形下陷。")
        print(f"  3. 温升是不是小于 {MIN_RISE:g} °C？看上面「温升幅度」——只有零点几度"
              f"说明光其实没打到样品（或打到的时间太短）。")
        print(f"  4. 做峰次数是否等于 {pulses}？少做了会报「只识别到 N 个峰」。")
    print("=" * 68)

    rw.close()
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
