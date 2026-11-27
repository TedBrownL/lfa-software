"""会话数据缓冲。

为什么需要它：轮询速率实测约 6.6 kHz，30 s 就有 20 万点。若把全部点交给绘图，
界面会卡；但导出和后续拟合又需要完整数据。因此：

* **全量保存**：预分配 numpy 数组，容量不足时倍增，避免每点做列表追加；
* **按需降采样**：绘图时对可见区间做 min/max 包络抽稀，点数只与像素宽度相关。

x 轴内部统一用"相对会话起点的秒数"（float64）。会话起点是一个墙钟时刻，
刻度由 :class:`WallClockAxis` 换算回绝对时间显示。
"""

from __future__ import annotations

import numpy as np

from .timebase import NS


class TraceStore:
    """按块追加的温度-时间序列（全量，内存内）。"""

    def __init__(self, initial_capacity: int = 1 << 16):
        self._cap = int(initial_capacity)
        self._n = 0
        self._t_perf = np.empty(self._cap, dtype=np.int64)   # 单调时钟 ns
        self._t_wall = np.empty(self._cap, dtype=np.int64)   # 墙钟 ns
        self._temp = np.empty(self._cap, dtype=np.float32)
        # 相对秒数增量缓存：6.6 kHz 下若每次刷新都重算全量，几分钟后界面必然卡死
        self._rel = np.empty(self._cap, dtype=np.float64)
        self._rel_n = 0
        self._t0_perf = 0

    # ---------- 基本属性 ----------
    def __len__(self) -> int:
        return self._n

    @property
    def is_empty(self) -> bool:
        return self._n == 0

    @property
    def t_wall_all(self) -> np.ndarray:
        return self._t_wall[: self._n]

    @property
    def t_perf_all(self) -> np.ndarray:
        return self._t_perf[: self._n]

    @property
    def temp_all(self) -> np.ndarray:
        return self._temp[: self._n]

    @property
    def t0_perf_ns(self) -> int:
        """首点的单调时钟时间戳（相对秒数的零点）。"""
        return int(self._t0_perf)

    # ---------- 写入 ----------
    def _ensure(self, extra: int) -> None:
        need = self._n + extra
        if need <= self._cap:
            return
        cap = self._cap
        while cap < need:
            cap *= 2
        self._t_perf = _grown(self._t_perf, cap)
        self._t_wall = _grown(self._t_wall, cap)
        self._temp = _grown(self._temp, cap)
        self._rel = _grown(self._rel, cap)
        self._cap = cap

    def append_block(self, t_perf_ns, t_wall_ns, temp_c) -> None:
        n = len(temp_c)
        if n == 0:
            return
        self._ensure(n)
        i, j = self._n, self._n + n
        self._t_perf[i:j] = t_perf_ns
        self._t_wall[i:j] = t_wall_ns
        self._temp[i:j] = temp_c
        if self._n == 0:
            self._t0_perf = int(self._t_perf[0])
        # 只算新增的那一段，避免全量重算
        self._rel[i:j] = (self._t_perf[i:j] - self._t0_perf) / NS
        self._rel_n = j
        self._n = j

    def clear(self) -> None:
        self._n = 0
        self._rel_n = 0
        self._t0_perf = 0

    # ---------- 读取 ----------
    def relative_seconds(self) -> np.ndarray:
        """相对会话起点的秒数（x 轴内部单位，增量维护，O(1) 取用）。"""
        return self._rel[: self._rel_n]

    def window(self, x_end: float, span_s: float | None) -> tuple[np.ndarray, np.ndarray]:
        """返回可见窗口内的 (x, y)，span_s=None 表示全览。"""
        if self._n == 0:
            return np.empty(0), np.empty(0)
        x = self.relative_seconds()
        i0 = 0 if span_s is None else int(np.searchsorted(x, x_end - span_s, "left"))
        return x[i0:], self._temp[: self._n][i0:]

    def stats_window(self, last_seconds: float = 10.0) -> tuple[float, float, float]:
        """最近一段的 (min, max, mean)，用于纵轴自动缩放。"""
        if self._n == 0:
            return (0.0, 1.0, 0.0)
        x = self.relative_seconds()
        i0 = int(np.searchsorted(x, x[-1] - last_seconds, "left"))
        seg = self._temp[i0: self._n]
        return (float(seg.min()), float(seg.max()), float(seg.mean()))


def _grown(arr: np.ndarray, cap: int) -> np.ndarray:
    out = np.empty(cap, dtype=arr.dtype)
    out[: arr.size] = arr
    return out


def envelope(x: np.ndarray, y: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """min/max 包络抽稀：保证每个像素列内的极值不丢失。

    直接用抽稀（等间隔取样）会丢掉尖峰，而 LFA 关心的恰恰是上升沿的陡变。

    实现上必须**全向量化**：早先用 Python 逐桶循环（1200 次/帧），会周期性地
    持有 GIL 约 3 ms，把采集线程的 p99 停顿顶到 3 ms、速率压到 4.6 kHz。
    这里改用 reshape + argmin/argmax(axis=1)，整个函数只有几次 numpy 调用。
    """
    n = x.size
    if n <= max_points or max_points < 4:
        return x, y

    buckets = max(2, max_points // 2)
    seg = int(np.ceil(n / buckets))
    buckets = n // seg                    # 实际可整除的桶数
    if buckets < 2:
        return x, y

    ycut = y[: buckets * seg].reshape(buckets, seg)
    lo = ycut.argmin(axis=1)
    hi = ycut.argmax(axis=1)

    # 同桶内极值相同时，argmin/argmax 都返回首个下标；此时 lo==hi，
    # 只需输出一个点。按时间先后排列，避免曲线自交。
    first = np.minimum(lo, hi)
    second = np.maximum(lo, hi)
    base = np.arange(buckets) * seg

    i1 = base + first
    i2 = base + second
    # 交错成 [i1_0, i2_0, i1_1, i2_1, ...]
    idx = np.empty(buckets * 2, dtype=np.int64)
    idx[0::2] = i1
    idx[1::2] = i2
    # 去掉同桶内重复的那一半（lo==hi 时两个下标相同）
    keep = np.ones(idx.size, dtype=bool)
    keep[1::2] = i2 != i1

    xs = x[idx[keep]]
    ys = y[idx[keep]]

    # 结尾不足一桶的余量，把最后一点补上，避免曲线右端缺一小段
    if buckets * seg < n:
        xs = np.append(xs, x[-1])
        ys = np.append(ys, y[-1])
    return xs, ys
