"""采集控制层：独立进程采样 + 主进程接收。

职责划分
--------
* **子进程**（:mod:`lfa.acq_process`）：只做串口 I/O 和时间戳，不导入 Qt。
* **主进程接收线程**（本模块的 ``_QueueReader``）：阻塞在队列上取块，
  用 :class:`TimeBase` 把 perf 时间戳批量换算成墙钟，再以 Qt 信号交给界面。
* **界面线程**：只收信号、入库、重绘。

为什么不是"再开一个线程"
------------------------
采集原先就已经在独立 ``QThread`` 里了。问题出在 CPython 的 GIL：界面线程
每帧 ``setData`` 重绘会连续持有 GIL 2-3 ms，采集线程在这期间完全停摆，
时间戳上就出现 2-3 ms 的空档（帧率越高空档越多，所以"提高刷新率"反而让
采集更差）。GIL 是**进程内**的，只有换进程才能真正隔离。

实测（设备 1 ms、界面 16 ms 重绘，见 ``tools/bench_multiprocess.py``）：

==========================  ==========  ================  ==========
方案                        采样率      >1 ms 停顿次数    累计停顿
==========================  ==========  ================  ==========
界面同进程内的线程           3,397 Hz    482               2,383 ms
独立进程                     6,530 Hz    1                 31 ms
==========================  ==========  ================  ==========

时间戳语义保持不变
------------------
子进程只发 ``perf_counter_ns()``（已在请求发出时刻、减去半往返补偿），
墙钟换算全部由主进程的**同一个** :class:`TimeBase` 锚点完成。因此界面时钟、
零点标记、CSV 导出仍共用一套时间基准，不会出现两套墙钟各自漂移。
"""

from __future__ import annotations

import queue as queue_mod
import threading
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, Signal

from .acq_process import AcquisitionConfig, child_main
from .optris import DeviceInfo
from .timebase import TimeBase

__all__ = ["AcquisitionConfig", "SampleBlock", "AcquisitionController",
           "HeadlessAcquisition"]


@dataclass
class SampleBlock:
    """一次回调携带的一批样本。"""

    t_perf_ns: np.ndarray | None = None   # 单调时钟 ns（已做半往返补偿）
    t_wall_ns: np.ndarray | None = None   # 墙钟 ns（绝对时间）
    temp_c: np.ndarray | None = None      # 摄氏温度
    bad: int = 0                          # 本轮读失败的次数

    def __len__(self) -> int:
        return 0 if self.temp_c is None else int(self.temp_c.size)


class _QueueReader(threading.Thread):
    """主进程侧：阻塞取队列消息，转成 Qt 信号。

    放在普通线程而不是 QThread，是因为这里 99% 的时间阻塞在 ``Queue.get``
    上（阻塞期间释放 GIL），不需要 Qt 事件循环。信号从非 Qt 线程发出时，
    Qt 会自动按队列连接投递到界面线程。
    """

    def __init__(self, q, ctl: "AcquisitionController"):
        super().__init__(daemon=True, name="lfa-acq-reader")
        self._q = q
        self._ctl = ctl
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # 注意 "排空" 语义：只有在队列取空（Empty）时才检查停止标志。
        # 否则 stop() 一置标志就会丢掉队列里最后几块已采到的数据。
        while True:
            try:
                msg = self._q.get(timeout=0.1)
            except queue_mod.Empty:
                if self._stop.is_set():
                    break
                continue
            except (EOFError, OSError, ValueError):
                break
            kind = msg[0]
            if kind == "block":
                self._ctl._handle_block(msg[1], msg[2], msg[3])
            elif kind == "hello":
                self._ctl._handle_hello(msg[1], msg[2], msg[3])
            elif kind == "failed":
                self._ctl._handle_failed(msg[1])
            elif kind == "bye":
                break
        self._ctl._finish()


class AcquisitionController(QObject):
    """向界面暴露稳定接口：start / stop / 一组信号。"""

    block_ready = Signal(object)      # SampleBlock
    opened = Signal(object)           # DeviceInfo
    failed = Signal(str)              # 错误信息
    stopped = Signal()

    #: 子进程 perf 与父进程 perf 时间基相差超过这个值，就认为两边原点不同，
    #: 改用 hello 那一对读数做偏移换算（Windows 上实测同为 QPC，差值 < 1 ms）
    _SAME_BASE_TOL_NS = 500_000_000

    def __init__(self, timebase: TimeBase, parent=None):
        super().__init__(parent)
        self.timebase = timebase
        self._ctx = None
        self._proc = None
        self._q = None
        self._stop_evt = None
        self._reader: _QueueReader | None = None
        self._running = False
        self._emitted_stopped = False
        self._ts_offset_ns = 0

    # ---------- 生命周期 ----------
    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, cfg: AcquisitionConfig) -> None:
        if self._running:
            return
        import multiprocessing as mp

        # spawn 而不是 fork：Windows 只有 spawn；且子进程不继承 Qt 状态，
        # 避免任何 GUI 相关代码在子进程里被初始化。
        self._ctx = mp.get_context("spawn")
        self._q = self._ctx.Queue()
        self._stop_evt = self._ctx.Event()
        self._ts_offset_ns = 0
        self._emitted_stopped = False
        self._proc = self._ctx.Process(
            target=child_main, args=(cfg, self._q, self._stop_evt),
            daemon=True, name="lfa-acq")
        self._proc.start()
        self._running = True
        self._reader = _QueueReader(self._q, self)
        self._reader.start()

    def stop(self, wait_ms: int = 3000) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_evt is not None:
            self._stop_evt.set()
        if self._proc is not None:
            self._proc.join(wait_ms / 1000.0)
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(1.0)
        # 先置停止标志，接收线程会继续把队列里剩下的块投递完、取空后才退出；
        # 顺序不能反——否则子进程被 terminate（没来得及发 bye）时要干等满超时。
        if self._reader is not None:
            self._reader.request_stop()
            self._reader.join(2.0)
            self._reader = None
        self._close_queue()
        self._finish()

    def _close_queue(self) -> None:
        q, self._q = self._q, None
        if q is not None:
            try:
                q.close()
                q.join_thread()
            except Exception:
                pass
        self._proc = None
        self._stop_evt = None

    def _finish(self) -> None:
        """无论正常结束、失败还是被 terminate，只发一次 stopped。"""
        if self._emitted_stopped:
            return
        self._emitted_stopped = True
        self._running = False
        self.stopped.emit()

    # ---------- 消息处理（在接收线程中调用） ----------
    def _handle_hello(self, child_perf: int, child_wall: int,
                      info_d: dict) -> None:
        parent_perf = TimeBase.perf_now()
        off = parent_perf - child_perf
        if abs(off) > self._SAME_BASE_TOL_NS:
            # 两边 perf 原点不同：用这一对读数把子进程时间轴搬到父进程上
            self._ts_offset_ns = off
        info = DeviceInfo(port=info_d.get("port", ""),
                          baudrate=int(info_d.get("baudrate", 115200)),
                          serial_number=info_d.get("serial_number", ""),
                          firmware=info_d.get("firmware", ""))
        self.opened.emit(info)

    def _handle_block(self, t_perf, temp, bad: int) -> None:
        block = SampleBlock()
        if self._ts_offset_ns:
            t_perf = t_perf + self._ts_offset_ns
        block.t_perf_ns = t_perf
        block.temp_c = temp
        block.bad = bad
        block.t_wall_ns = self.timebase.wall_from_perf_array(t_perf)
        self.block_ready.emit(block)

    def _handle_failed(self, msg: str) -> None:
        self.failed.emit(msg)


class HeadlessAcquisition:
    """无界面脚本用的采集器（自检 / 基准 / 将来的命令行采集）。

    复用子进程同一个 :func:`lfa.acq_process.sample_loop`，因此时间戳语义与
    软件的独立进程版本完全一致；区别只是它跑在当前进程的线程里——脚本没有
    GUI 重绘，不需要进程隔离，也就省掉 spawn 的开销和"必须先写文件"的麻烦。

    用法::

        acq = HeadlessAcquisition(cfg, TimeBase())
        acq.start(on_block)
        ...
        acq.stop()
    """

    def __init__(self, cfg: AcquisitionConfig, timebase: TimeBase):
        self.cfg = cfg
        self.timebase = timebase
        self.info: DeviceInfo | None = None
        self.error: str | None = None
        self._dev = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_block) -> None:
        from .optris import OptrisCT4M

        self._dev = OptrisCT4M(self.cfg.port, self.cfg.baudrate)
        self._dev.open()
        try:
            self.info = self._dev.identify()
        except Exception:
            self.info = None

        def emit(t_perf, temp, bad):
            block = SampleBlock()
            block.t_perf_ns = t_perf
            block.temp_c = temp
            block.bad = bad
            block.t_wall_ns = self.timebase.wall_from_perf_array(t_perf)
            on_block(block)

        def body():
            from .acq_process import sample_loop

            try:
                sample_loop(self._dev, self.cfg, emit, self._stop)
            except Exception as exc:
                self.error = str(exc)
            finally:
                self._dev.close()

        self._thread = threading.Thread(target=body, daemon=True,
                                        name="lfa-acq-headless")
        self._thread.start()

    def stop(self, wait_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(wait_s)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
