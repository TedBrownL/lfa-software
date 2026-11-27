"""程序入口。

用法：
    D:/MINICONDA/envs/casimir/python.exe -m lfa
或：
    双击 run.bat

注意：这里**不能在模块顶层导入 Qt**。采集子进程用 ``spawn`` 启动时，
multiprocessing 会把 ``lfa.__main__`` 当作 ``__mp_main__`` 重新导入一遍；
若顶层就 ``import PySide6``，每个子进程都要白白加载一次整套 Qt（实测多花
100-300 ms 启动时间，且完全没有必要）。
"""

from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("LFA 温度采集")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
