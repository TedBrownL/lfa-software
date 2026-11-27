"""时间基准：把高精度单调时钟映射到"墙钟"绝对时间。

设计要点（这是后面时间零点对齐的地基）：

* **采样间隔必须来自单调时钟** ``time.perf_counter_ns()``。``time.time()`` 受
  Windows 对时（NTP/授时）影响会跳变，绝不能用来做差分。
* 界面上要显示的"当前实际时间"是墙钟，于是用一个锚点把两者绑定：
      wall = anchor_wall_ns + (perf_now_ns - anchor_perf_ns)
  锚点定期检查是否需要重算（默认 1 s 查一次），但只有漂移超过 ``resync_deadband_ns``
  才真的换锚——避免每个周期都微调、让时间戳出现抖动或倒退。
* 本机 perf_counter 分辨率实测 100 ns，抖动远小于 1 ms，满足到 ms 的显示要求。
* 采集线程以约 6.6 kHz 调用 :meth:`wall_from_perf`，界面线程同时可能触发重锚，
  因此锚点的读与写都在锁内完成，保证不会读到"半新半旧"的一对锚点。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

NS = 1_000_000_000


@dataclass
class TimeBase:
    """单调时钟 + 墙钟的绑定器。"""

    resync_interval_s: float = 1.0
    resync_deadband_ns: int = 2_000_000     # 漂移 < 2 ms 不换锚，保时间戳稳定

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        perf = time.perf_counter_ns()
        wall = time.time_ns()
        self._anchor_perf = perf
        self._anchor_wall = wall
        self._last_sync = time.monotonic()
        self._last_drift_ns = 0

    # ---------- 读数 ----------
    @staticmethod
    def perf_now() -> int:
        """单调时钟（ns）。用于做任何差分/间隔计算。"""
        return time.perf_counter_ns()

    def wall_from_perf(self, perf_ns: int) -> int:
        """把某个 perf 时间戳换算成墙钟 ns（读锚点在锁内）。"""
        with self._lock:
            return self._anchor_wall + (perf_ns - self._anchor_perf)

    def anchor(self) -> tuple[int, int]:
        """一次性取出 (anchor_perf, anchor_wall)。

        高频采集不能每点都调 :meth:`wall_from_perf`——那等于每秒数千次抢锁。
        改为在"打包一块数据"时取一次锚点，再用 numpy 批量换算。
        """
        with self._lock:
            return self._anchor_perf, self._anchor_wall

    def wall_from_perf_array(self, perf_ns):
        """numpy 向量化的 perf -> wall 换算（同一锚点，保证块内一致）。"""
        import numpy as np

        ap, aw = self.anchor()
        return np.asarray(perf_ns, dtype=np.int64) - ap + aw

    def wall_now(self) -> int:
        """当前墙钟时间（ns，绝对时间尺度）。"""
        self.maybe_resync()
        return self.wall_from_perf(self.perf_now())

    # ---------- 重锚 ----------
    def maybe_resync(self) -> None:
        if time.monotonic() - self._last_sync < self.resync_interval_s:
            return
        self._last_sync = time.monotonic()
        # 同一时刻读两个时钟，漂移 = 现有映射的预测误差
        perf = time.perf_counter_ns()
        wall = time.time_ns()
        with self._lock:
            predicted = self._anchor_wall + (perf - self._anchor_perf)
            drift = wall - predicted
            self._last_drift_ns = drift
            if abs(drift) >= self.resync_deadband_ns:
                self._anchor_perf = perf
                self._anchor_wall = wall

    @property
    def last_drift_us(self) -> float:
        """最近一次检查到的墙钟漂移（us），用于界面/日志观测对时情况。"""
        return self._last_drift_ns / 1e3


def format_wall(wall_ns: int, with_date: bool = False) -> str:
    """墙钟 ns -> 'HH:MM:SS.mmm'（可选带日期）。"""
    sec, nsec = divmod(wall_ns, NS)
    tm = time.localtime(sec)
    ms = nsec // 1_000_000
    base = f"{tm.tm_hour:02d}:{tm.tm_min:02d}:{tm.tm_sec:02d}.{ms:03d}"
    if with_date:
        return f"{tm.tm_year:04d}-{tm.tm_mon:02d}-{tm.tm_mday:02d} {base}"
    return base


def format_wall_iso(wall_ns: int) -> str:
    """CSV 用：带日期的毫秒级时间戳 'YYYY-MM-DD HH:MM:SS.mmm'。"""
    return format_wall(wall_ns, with_date=True)


def wall_to_epoch_seconds(wall_ns: int) -> float:
    return wall_ns / NS
