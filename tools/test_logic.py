"""不接硬件的逻辑测试。

重点回归三件事：
1. 采集块必须是**拷贝**——曾出现信号排队期间缓冲被下一批覆盖，数据被静默污染
   （进程版由 pickle 保证；线程版/无界面版靠 ``sample_loop`` 里的 ``.copy()``）；
2. TraceStore 的增量相对时间缓存与直接计算一致；
3. 跨进程时间戳：子进程只发 perf，父进程用自己的锚点换算墙钟，两边必须单调一致。

用法：python tools/test_logic.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from lfa.acq_process import AcquisitionConfig, sample_loop
from lfa.store import TraceStore, envelope
from lfa.timebase import NS, TimeBase


def test_block_is_copy() -> None:
    """同一缓冲在后续样本写入后，已取到的块内容不得改变。

    用一个假设备连续喂样本，检查 ``sample_loop`` 交给回调的数组与预分配
    缓冲不共享内存 —— 这是"数据被静默覆盖"那个老 bug 的护栏。
    """
    class FakeDev:
        def __init__(self, n: int, stop):
            self.n = n
            self.i = 0
            self.stop = stop

        def read_temperature_word(self):
            if self.i >= self.n:
                self.stop.set()          # 喂完就收工，避免触发连续超时保护
                return b""
            self.i += 1
            # 20.0 + i*0.1 编码回 2 字节
            word = int(round((20.0 + self.i * 0.1) * 10 + 1000))
            return bytes([(word >> 8) & 0xFF, word & 0xFF])

    import threading

    cfg = AcquisitionConfig(block_size=10, flush_interval_s=10.0)
    got = []
    stop = threading.Event()
    sample_loop(FakeDev(30, stop), cfg, lambda t, v, bad: got.append((t, v)),
                stop)

    assert len(got) == 3, f"分块数应为 3，实际 {len(got)}"
    first_t, first_v = got[0]
    # 后续块的内容必须与第一块不同（说明缓冲没被复用覆盖）
    assert first_v[0] == 20.1, f"首块首点温度错了：{first_v[0]}"
    assert got[2][1][-1] == 23.0, f"末块末点温度错了：{got[2][1][-1]}"
    # 每块都是独立内存
    ptrs = {a.__array_interface__["data"][0] for a, _ in got}
    assert len(ptrs) == len(got), "分块共享了同一块内存，会被后续样本覆盖"
    print(f"  block is copy            OK ({len(got)} 块，各自独立内存)")


def test_store_incremental_rel() -> None:
    st = TraceStore(initial_capacity=8)
    for k in range(5):
        t = np.arange(k * 100, k * 100 + 100, dtype=np.int64) * 100_000  # 0.1 ms 步长
        w = t + 1_700_000_000 * NS
        v = np.full(100, 20.0 + k)
        st.append_block(t, w, v)
    rel = st.relative_seconds()
    expect = (st.t_perf_all - st.t_perf_all[0]) / NS
    assert np.allclose(rel, expect), "增量缓存与全量计算不一致"
    assert len(st) == 500
    print(f"  store rel cache           OK (n={len(st)}, 末点 {rel[-1]:.4f} s)")


def test_store_growth() -> None:
    st = TraceStore(initial_capacity=16)
    for _ in range(10):
        t = np.arange(16, dtype=np.int64)
        st.append_block(t, t, np.zeros(16))
    assert len(st) == 160
    assert np.array_equal(st.t_perf_all, np.tile(np.arange(16), 10))
    print("  store growth              OK")


def test_envelope_keeps_extremes() -> None:
    x = np.arange(10_000, dtype=np.float64)
    y = np.zeros_like(x)
    y[4321] = 99.0        # 单点尖峰
    y[8765] = -50.0
    xs, ys = envelope(x, y, 400)
    assert 99.0 in ys and -50.0 in ys, "包络抽稀丢失极值"
    assert xs.size <= 400
    print(f"  envelope keeps extremes   OK ({x.size} -> {xs.size} 点)")


def test_timebase_sync() -> None:
    tb = TimeBase(resync_interval_s=0.0)
    for _ in range(5):
        p = tb.perf_now()
        w = tb.wall_from_perf(p)
        assert abs(w - tb.wall_now()) < 50 * NS
    arr = tb.wall_from_perf_array(np.array([0, 1_000_000], dtype=np.int64))
    assert arr.dtype == np.int64 and arr[1] - arr[0] == 1_000_000
    print(f"  timebase mapping          OK (drift {tb.last_drift_us:.0f} us)")


def _perf_probe_child(q) -> None:
    """模块级（spawn 要求目标可 pickle）。"""
    q.put((TimeBase.perf_now(), TimeBase.perf_now()))


def test_perf_shared_across_process() -> None:
    """子进程的 perf_counter 与父进程是否同一时间基。

    这决定 ``AcquisitionController`` 能否直接把子进程的 perf 时间戳拿来用。
    Windows 上两者都基于 QPC，应当只差"进程启动耗时"；若某平台不成立，
    controller 会自动退到 hello 偏移换算（见 ``_SAME_BASE_TOL_NS``）。
    """
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_perf_probe_child, args=(q,), daemon=True)
    t0 = TimeBase.perf_now()
    p.start()
    a, b = q.get(timeout=30)
    t1 = TimeBase.perf_now()
    p.join(5)
    # 子进程读数必须落在父进程"启动->收尾"这段真实时间之内
    assert t0 < a < t1 and t0 < b < t1, (
        f"父子 perf 不同源：child {a}/{b} 不在 parent [{t0}, {t1}] 内")
    print(f"  perf same base (spawn)    OK (启动耗时 {(a - t0) / 1e6:.0f} ms)")


if __name__ == "__main__":
    print("逻辑测试：")
    test_block_is_copy()
    test_store_incremental_rel()
    test_store_growth()
    test_envelope_keeps_extremes()
    test_timebase_sync()
    test_perf_shared_across_process()
    print("全部通过")
