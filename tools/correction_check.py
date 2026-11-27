"""校正检查：真机在场时，界面的各项设置与健壮性。

不启动采集的项全靠界面逻辑；需要采集的项只跑很短的窗口。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fail = []


def check(cond, name, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (f"   [{detail}]" if detail else ""))
    if not cond:
        _fail.append(name)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from lfa.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow()
    win.show()
    app.processEvents()

    print("[1] 启动后自动读取设备设置（不应阻塞窗口显示）")
    t0 = time.perf_counter()
    deadline = time.perf_counter() + 5
    while time.perf_counter() < deadline:
        app.processEvents()
        if getattr(win, "_signal_state", None) is not None:
            break
        time.sleep(0.01)
    print(f"    耗时 {(time.perf_counter()-t0)*1000:.0f} ms")
    check(win._signal_state is not None, "设备设置已自动读取")
    check(win.sp_avg.value() == win._signal_state[0], "输入框同步为设备真值",
          f"{win.sp_avg.value()} vs {win._signal_state[0]}")

    print("[2] 单位切换：纵轴与读数同步")
    win.cb_unit.setCurrentIndex(1)
    app.processEvents()
    lbl = win.plot.plot.getPlotItem().getAxis("left").labelText
    check("K" in lbl, "纵轴标注 K", lbl)
    win.cb_unit.setCurrentIndex(0)
    app.processEvents()
    check("°C" in win.plot.plot.getPlotItem().getAxis("left").labelText,
          "纵轴标注 °C")

    print("[3] 脉冲数约束")
    win.sp_pulsen.setValue(0)
    check(win.sp_pulsen.value() >= 1, "不能设为 0", str(win.sp_pulsen.value()))
    win.sp_pulsen.setValue(1)

    print("[4] 采集中禁止修改设备设置")
    win._start_experiment()
    app.processEvents()
    time.sleep(0.3)
    app.processEvents()
    ok = win._write_device_settings(avg_ms=100)
    check(not ok, "采集中写设备被拒绝")
    check(win._signal_state[0] == 1, "设备平均时间仍是 1 ms",
          str(win._signal_state[0]))

    print("[5] 结束实验 → 结果窗口 → 关闭后解锁")
    time.sleep(1.5)
    app.processEvents()
    win._stop_experiment()
    deadline = time.perf_counter() + 25
    while time.perf_counter() < deadline and win.result_win is None:
        app.processEvents()
        time.sleep(0.005)
    check(win.result_win is not None, "结果窗口弹出")
    if win.result_win is not None:
        rw = win.result_win
        check(not win.btn_exp_start.isEnabled(), "结果窗口开着时不能开始新实验")
        rw.close()
        app.processEvents()
        time.sleep(0.1)
        app.processEvents()
        check(win.result_win is None, "关闭后置空")
        check(win.btn_exp_start.isEnabled(), "关闭后可再次开始")

    print("[6] 设备状态核对（结束后不应被改动）")
    win.refresh_device_settings(auto=True)
    check(win._signal_state[0] == 1, "平均时间保持 1 ms")
    check(win._signal_state[1] == 0, "智能平均仍关")
    check(win._signal_state[2] == 0, "保持仍关")

    win.close()
    print()
    if _fail:
        print(f"失败 {len(_fail)} 项：" + "、".join(_fail))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
