"""GUI 冒烟测试：真实采集 + 截图 + 导出，全程不弹出窗口。

用法：
    QT_QPA_PLATFORM=offscreen python tools/gui_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lfa.main_window import MainWindow
from lfa.export import export_csv


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "docs", "screenshots")
    os.makedirs(out_dir, exist_ok=True)
    log = []

    def start():
        idx = win.cb_port.findData("COM10")
        if idx >= 0:
            win.cb_port.setCurrentIndex(idx)
        win.cb_span.setCurrentIndex(0)     # 10 s
        # 冒烟测试无人值守：平均时间告警会弹模态框把测试卡死。
        # 这里直接跳过拦截，改为在日志里记录设备真实设置。
        win._ensure_signal_ready = lambda: True
        log.append(f"device avg={getattr(win, '_signal_state', None)}")
        win._start()
        log.append("started")

    def snap_and_stop():
        if not win.acq.is_running:
            log.append(f"NOT RUNNING at snap, n={len(win.store)}")
        img = win.grab()
        p = os.path.join(out_dir, "gui_run.png")
        img.save(p)
        log.append(f"saved {p} n={len(win.store)} "
                   f"rate={win.lbl_rate.text()} temp={win.lbl_temp.text()} "
                   f"clock={win.lbl_clock.text()}")
        win._stop()

    def finish():
        n = len(win.store)
        if n:
            meta = win.__dict__.get("_smoke_meta")
            out = os.path.join(tempfile.gettempdir(), "lfa_gui_export.csv")
            from lfa.export import ExportMeta
            export_csv(out, win.store, ExportMeta(device="Optris CT4ML"))
            log.append(f"exported {n} rows -> {out}")
        win.grab().save(os.path.join(out_dir, "gui_stopped.png"))
        log.append("stopped ok")
        print("\n".join(log))
        app.quit()

    QTimer.singleShot(300, start)
    QTimer.singleShot(6000, snap_and_stop)
    QTimer.singleShot(9000, finish)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
