"""独立进程中的温度采集（子进程侧）。

为什么采集要放进独立**进程**，而不是线程
----------------------------------------
CPython 同一时刻只允许一个线程执行字节码。GUI 线程每帧调用 ``setData`` 让
pyqtgraph 重算/重绘，会连续持有 GIL 约 2-3 ms；采集线程在这段时间里完全拿
不到 GIL，表现就是时间戳上出现 2-3 ms 的空档。

实测（``tools/bench_gui.py`` / ``tools/bench_multiprocess.py``，设备平均时间
1 ms、界面 16 ms 重绘）：

======================  ==========  ================  ==========
采集所在位置            采样率      >1 ms 停顿次数    累计停顿
======================  ==========  ================  ==========
GUI 同进程内的线程       3,397 Hz    482               2,383 ms
独立进程                 6,530 Hz    1                 31 ms
======================  ==========  ================  ==========

因此本模块把采样循环放进 ``multiprocessing`` 子进程。子进程不导入 Qt，
启动只需要 numpy + pyserial。

父子进程之间的时间戳
--------------------
子进程只发 ``perf_counter_ns()``，墙钟换算统一由父进程的
:class:`lfa.timebase.TimeBase` 完成——这样界面时钟、零点标记、CSV 用的是
**同一个锚点**，不会出现两套墙钟各自漂移。

Windows 的 ``perf_counter`` 基于 QPC，全系统共用一个时间基，实测父子进程
读数可以直接相减。为防万一（其他平台可能每个进程有独立原点），父进程在
收到 ``hello`` 时会校验一次，不成立就改用"子进程原点偏移"换算。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .optris import OptrisCT4M

NS = 1_000_000_000


@dataclass
class AcquisitionConfig:
    """一次采集的全部参数（会被 pickle 到子进程，故只放基本类型）。"""

    port: str = "COM10"
    baudrate: int = 115200
    # 半往返时延补偿（ns）。由 tools/bench_latency.py 实测 p50 ≈ 148 us 给出。
    latency_half_ns: int = 74_000
    # 每积累多少个样本向界面推送一次。6.5 kHz 下 64 点 ≈ 10 ms。
    # 这个值必须**小于界面刷新间隔**（16 ms），否则会出现"两帧才来一次新数据"，
    # 画面实际只有 30 多 Hz，看起来就是一跳一跳的——这是"帧率低"的真实来源，
    # 而不是重绘本身慢（实测重绘 p50 仅 1.6 ms）。
    block_size: int = 64
    # 即使点数没凑够，超过这个时间也推一次，保证界面持续跳动
    flush_interval_s: float = 0.008
    # 读超时后是否清空缓冲（防止读到滞后数据）
    drop_on_timeout: bool = True


def sample_loop(dev, cfg: AcquisitionConfig, emit, stop_evt) -> None:
    """采样热路径。``emit(t_perf, temp, bad)`` 每凑满一块调用一次。

    传入的数组是预分配缓冲的**切片**，调用方必须自行拷贝（子进程里
    ``Queue.put`` 会立刻 pickle，等价于拷贝；线程版信号是排队投递的，
    必须显式 ``.copy()``，否则会被下一批样本覆盖）。

    时间戳打在**发出请求之前**，读回后减去半个往返时延。这样时间戳不会
    系统性晚于真实采样时刻（半程 ≈ 74 us，远小于 ms 级显示需求，但做 LFA
    拟合时这点偏差会进入截距）。
    """
    cap = int(cfg.block_size)
    flush_ns = int(cfg.flush_interval_s * NS)
    half = int(cfg.latency_half_ns)
    perf_now = time.perf_counter_ns

    t_buf = np.empty(cap, dtype=np.int64)
    temp_buf = np.empty(cap, dtype=np.float64)
    k = 0
    bad = 0
    t_block = perf_now()

    while not stop_evt.is_set():
        t_write = perf_now()
        raw = dev.read_temperature_word()
        if len(raw) != 2:
            bad += 1
            # 连续超时说明设备掉线；单次超时只丢掉这一点
            if bad > 50:
                raise RuntimeError(f"连续 {bad} 次读超时，设备可能已断开")
            continue
        bad = 0
        word = (raw[0] << 8) | raw[1]

        if k == cap:                     # 保险：配置变更或超发时先冲刷
            emit(t_buf[:k].copy(), temp_buf[:k].copy(), bad)
            k = 0
        t_buf[k] = t_write - half
        temp_buf[k] = (word - 1000) / 10.0
        k += 1

        if k >= cap or perf_now() - t_block >= flush_ns:
            emit(t_buf[:k].copy(), temp_buf[:k].copy(), bad)
            k = 0
            t_block = perf_now()

    if k:
        emit(t_buf[:k].copy(), temp_buf[:k].copy(), bad)


def _put(q, msg) -> None:
    """父进程若已退出，Queue 会抛 BrokenPipe —— 这里静默吞掉。"""
    try:
        q.put(msg)
    except (BrokenPipeError, OSError, ValueError):
        pass


def child_main(cfg: AcquisitionConfig, out_q, stop_evt) -> None:
    """子进程入口（必须是模块级函数，spawn 才能按引用 pickle）。"""
    dev = OptrisCT4M(cfg.port, cfg.baudrate)
    try:
        dev.open()
    except Exception as exc:                     # 串口占用 / 设备未插
        _put(out_q, ("failed", f"无法打开 {cfg.port}：{exc}"))
        return

    try:
        try:
            info = dev.identify()
            info_d = {"port": info.port, "baudrate": info.baudrate,
                      "serial_number": info.serial_number,
                      "firmware": info.firmware}
        except Exception:
            info_d = {"port": cfg.port, "baudrate": cfg.baudrate,
                      "serial_number": "", "firmware": ""}
        # hello 里带上子进程此刻的 perf/墙钟读数，供父进程校验两边的
        # perf_counter 是否同一时间基（见 lfa.acquisition._on_hello）
        _put(out_q, ("hello", time.perf_counter_ns(), time.time_ns(), info_d))
        sample_loop(dev, cfg,
                    lambda t, v, bad: _put(out_q, ("block", t, v, bad)),
                    stop_evt)
    except Exception as exc:
        _put(out_q, ("failed", f"采集中断：{exc}"))
    finally:
        dev.close()
        _put(out_q, ("bye",))
