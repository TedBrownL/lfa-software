"""实验配置、峰值识别与结果窗口截取。

本模块只做**纯计算**，不导入 Qt：便于单独测试，也方便将来的「分析」模块复用。

几个约定
--------
* 时间 ``t`` 一律是**相对实验起点（时间零点）的秒数**。时间零点＝按下
  「开始实验」的那一刻，机械遮挡方案接入后要换成硬件边沿，届时只改
  ``main_window`` 里给零点赋值的那一行，本模块不受影响。
* 温度内部一律用 **°C**（与测温仪原始输出一致），只在显示层换算成 K。
  这样配置文件、CSV、标注之间不会出现两套数值。
* 峰值识别在**原始数据**上做，平滑只影响画图，因此拖动平滑滑条时
  标注（峰点、区间）不会跳动 —— 这是需求里明确要求的。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field

import numpy as np

# --------------------------------------------------------------------------
# 材料预设
# --------------------------------------------------------------------------

#: 样品预设：(比热容 J/(kg·K), 密度 kg/m³)，取室温附近的典型值。
#: 不锈钢按 304 奥氏体取值；这些数字只用于估算，测量时以实际牌号为准。
PRESETS: dict[str, tuple[float, float]] = {
    "不锈钢": (500.0, 7900.0),
    "铜": (385.0, 8960.0),
    "铝": (900.0, 2700.0),
}

DEFAULT_SPECIFIC_HEAT = 500.0
DEFAULT_DENSITY = 7900.0

#: 判定「没有明显温升特征」的阈值（°C）。CT4ML 分辨率 0.1 °C，
#: 真实 LFA 温升通常远大于此，因此 0.5 K 足以区分"没测到东西"与"测到了"。
MIN_RISE_C = 0.5

#: 结果窗口的截取比例：峰前 : 峰后 = 4 : 1
TAIL_RATIO = 4.0

#: 标注横线的位置参数，单位都是**数据跨度**（hi-lo）的比例。
#: 界面与导出的 PNG 共用这一套，保证看到的就是导出的。
#:
#: ``BAND_Y``：所有横线统一放在曲线最高点之上这么高的地方。
#:   取 0.34 而不取更小值，是因为峰点的 M 标注文字（8pt）向上会占掉
#:   跨度的一小部分，横线必须完全避开它——否则"横线压住 M 文字"。
#:   各区间在时间上首尾相连，同高不会互相遮挡，视觉上就是一条被分段的
#:   尺寸标注线，比阶梯状更易读（阶梯状会让人以为高度有含义）。
#: ``BAND_TOP_PAD``：横线之上再留给标签文字的空间。
BAND_Y = 0.34
BAND_TOP_PAD = 0.16
Y_BOTTOM_PAD = 0.12     # 曲线底部留白

#: 标注横线最多画这么多条，再多会看不清；超出部分只在左栏文字里给
MAX_BANDS = 12


def band_layout(lo: float, hi: float, n_rows: int):
    """算出「曲线纵轴范围」和「每条标注横线的 y 值」。

    这是上一版最要命的 bug 的修复点：旧代码把 ``ViewBox.height()``
    （像素）乘进数据坐标里，算出的 y 远远超出视图范围，横线被画到画面外，
    所以界面上根本看不到标注。

    现在统一在**数据坐标**里算，并且**所有横线共用同一个 y**：
    区间在时间上首尾相连（A→M₁→M₂→…→B′），同高只会水平并排、
    不会互相遮挡；而阶梯状排布会让人误以为高度有物理含义。

    返回 ``(y_min, y_max, [row_y, ...])``；``row_y`` 全部相等，
    长度等于 ``n_rows``，与 ``res.intervals`` 一一对应。
    """
    span = max(float(hi) - float(lo), 1e-9)
    n = max(0, int(n_rows))
    y_min = float(lo) - span * Y_BOTTOM_PAD
    if n == 0:
        return y_min, float(hi) + span * 0.12, []

    y_line = float(hi) + span * BAND_Y
    y_max = y_line + span * BAND_TOP_PAD
    return y_min, y_max, [y_line] * n


@dataclass
class ExperimentConfig:
    """一次实验的全部配置。字段名即 JSON 键名。"""

    ambient_k: float = 300.0                 # 环境温度 K
    laser_power_w: float = 10.0              # 激光功率 W
    pulse_width_s: float = 0.1               # 单个脉冲持续时间 s
    pulse_count: int = 1                     # 脉冲数（>=1 的整数）
    material: str = ""                       # 样品材料（可自定义）
    specific_heat: float = DEFAULT_SPECIFIC_HEAT   # 比热容 J/(kg·K)
    density: float = DEFAULT_DENSITY               # 密度 kg/m³
    radius_mm: float = 10.0                  # 测温半径 mm

    # 以下由程序填写
    started_at: str = ""                     # 实验开始时间（本地时间字符串）
    zero_wall_ns: int = 0                    # 时间零点（墙钟 ns）
    ended_at: str = ""
    sample_rate_hz: float = 0.0
    n_samples: int = 0
    #: 零点到首个数据点的时间（s）。零点此刻起其实还没有数据，这段是空的。
    start_delay_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentConfig":
        """从 JSON 恢复；忽略未知键，缺失键取默认值。"""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def summary_lines(self) -> list[str]:
        """给结果窗口左栏显示的简要说明。"""
        lines = [
            f"样品材料：{self.material or '（未填写）'}",
            f"比热容：{self.specific_heat:g} J/(kg·K)",
            f"密度：{self.density:g} kg/m³",
            f"测温半径：{self.radius_mm:g} mm",
            f"环境温度：{self.ambient_k:g} K",
            f"激光功率：{self.laser_power_w:g} W",
            f"脉冲时间：{self.pulse_width_s:g} s × {self.pulse_count}",
            f"开始：{self.started_at or '—'}",
        ]
        if self.start_delay_s:
            lines.append(f"启动延迟：{self.start_delay_s * 1000:.0f} ms "
                         f"（零点后空白）")
        return lines


# --------------------------------------------------------------------------
# 平滑
# --------------------------------------------------------------------------

def boxcar(y: np.ndarray, width: int) -> np.ndarray:
    """长度为 ``width`` 的滑动平均（边界按实际点数归一），O(n)。

    用累积和实现：6.6 kHz 下 1 分钟就有 40 万点，逐窗口求和会让滑条卡住。
    """
    y = np.asarray(y, dtype=np.float64)
    n = y.size
    if width <= 1 or n < 3:
        return y.copy()
    width = int(min(width, n))
    half = width // 2
    pad = np.pad(y, (half, width - half - 1), mode="edge")
    c = np.concatenate(([0.0], np.cumsum(pad)))
    return (c[width:] - c[:-width]) / width


def smooth_level_to_width(n: int, level: float) -> int:
    """把 0..1 的平滑程度映射成窗口点数。

    用平方映射：低端（0~0.3）变化细，方便去掉毛刺而不抹平升温沿；
    高端才有明显的 TensorBoard 式平滑。
    """
    level = min(max(float(level), 0.0), 1.0)
    if level <= 0.0:
        return 1
    max_w = max(5, min(n // 10, 2001))
    return int(round(1 + (max_w - 1) * level ** 2))


def smooth_series(y: np.ndarray, level: float) -> np.ndarray:
    """按 0..1 的平滑程度返回平滑后的曲线（0 = 原样）。"""
    return boxcar(y, smooth_level_to_width(len(y), level))


# --------------------------------------------------------------------------
# 峰值识别
# --------------------------------------------------------------------------

@dataclass
class Peak:
    t: float      # 相对时间零点的秒数
    temp_c: float
    index: int    # 在传入数组中的下标（便于回查原始数据）


@dataclass
class Interval:
    t0: float
    t1: float
    label: str

    @property
    def duration(self) -> float:
        return self.t1 - self.t0


@dataclass
class AnalysisResult:
    ok: bool = True
    reason: str = ""
    peaks: list[Peak] = field(default_factory=list)
    intervals: list[Interval] = field(default_factory=list)
    #: 展示窗口 (t0, t1)，相对时间零点；None 表示不做截取
    window: tuple[float, float] | None = None
    #: True 表示 4:1 的 B' 落在数据之外，被钳到结束时刻，所以比例不成立
    window_clamped: bool = False
    delta_t: float = 0.0          # 平滑后的温升幅度 °C
    noise_c: float = 0.0          # 单点噪声 σ（°C）
    rise_snr: float = 0.0         # 温升相当于多少倍噪声 σ
    t_b: float = 0.0              # 结束实验时刻（人工设定，通常偏晚）

    @property
    def truncated(self) -> bool:
        return self.window is not None


def _decimate_max(y: np.ndarray, max_points: int):
    """最大值抽稀：返回 (原下标, 抽稀值, 步长)。

    峰值识别只需要"哪里最高"，用 max 池化不会漏掉尖峰（等间隔取样会）。
    """
    n = y.size
    if n <= max_points:
        return np.arange(n), y, 1
    d = int(math.ceil(n / max_points))
    m = n // d
    yy = y[: m * d].reshape(m, d)
    off = yy.argmax(axis=1)
    idx = off + np.arange(m) * d
    return idx, y[idx], d


def _local_max(y: np.ndarray) -> np.ndarray:
    """局部极大点掩码（平台取最左端，避免同一平台报多个峰）。"""
    n = y.size
    m = np.zeros(n, dtype=bool)
    if n < 3:
        return m
    m[1:-1] = (y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:])
    return m


def _prominence(y: np.ndarray, i: int, limit: int = 600) -> float:
    """近似 prominence：从峰向两侧走，直到遇到更高的点，取两侧最低谷的较大者。"""
    hi = y[i]
    left_min = hi
    j, steps = i - 1, 0
    while j >= 0 and steps < limit:
        v = y[j]
        if v > hi:
            break
        if v < left_min:
            left_min = v
        j -= 1
        steps += 1
    right_min = hi
    j, steps = i + 1, 0
    n = y.size
    while j < n and steps < limit:
        v = y[j]
        if v > hi:
            break
        if v < right_min:
            right_min = v
        j += 1
        steps += 1
    return float(hi - max(left_min, right_min))


def robust_sigma(y: np.ndarray) -> float:
    """用一阶差分的 MAD 估计噪声标准差（对趋势/台阶免疫）。

    为什么不用 ``np.std(y)``：曲线上有真实的温升斜坡时，标准差几乎全部来自
    斜坡本身，估不出噪声。而相邻点的差分里斜坡只贡献一个常数偏移，MAD 会
    把它当离群值剔掉，剩下的就是噪声。
    """
    y = np.asarray(y, dtype=np.float64)
    if y.size < 8:
        return 0.0
    d = np.diff(y)
    med = np.median(d)
    mad = np.median(np.abs(d - med))
    # 1.4826 把 MAD 换算成 σ；差分使方差翻倍，故再除以 √2
    return float(1.4826 * mad / math.sqrt(2.0))


def _baseline(y: np.ndarray) -> float:
    """冷态基线：取低分位数。

    为什么不用"分桶最小值"：桶长必须远大于峰的特征宽度（衰减时间），
    否则桶内最小值紧贴峰顶，基线跟着峰一起抬高——实测桶长 2 点时
    1.5 °C 的峰只剩 0.13 °C 的"峰高"，全部被判成噪声。

    LFA 温升是"快升慢降"，一次实验里大部分时间处于冷态或衰减态，
    因此低分位天然落在冷态水平上。这也是判据"峰高 = 当前温度 − 冷态基线"
    能成立的物理前提。
    """
    y = np.asarray(y, dtype=np.float64)
    if y.size < 20:
        return float(np.min(y)) if y.size else 0.0
    return float(np.percentile(y, 3.0))


def _has_valley(y_d: np.ndarray, i: int, j: int,
                frac: float = 0.30) -> bool:
    """抽稀序列上两个候选峰之间是否存在"真正的谷"。

    这是区分"两次独立脉冲"与"同一个峰顶/衰减尾上的噪声起伏"的物理判据：
    两次脉冲之间温度必然回落，而同一脉冲的峰顶与尾部只是同一条单调衰减曲线
    上的相邻点，中间没有谷。

    实测教训：最初按"时间间隔 < 脉宽的 40%"合并，结果单脉冲的衰减尾上、
    距真峰仅 50 ms 的噪声起伏仍被当成第二个峰。脉宽是"激光开着多久"，
    不是"样品冷下来要多久"，后者通常长一个量级，用脉宽做窗口根本不够。

    判据：区间内最低点必须比两端峰都低出 ``frac × 较矮峰高``。
    """
    lo, hi = (i, j) if i < j else (j, i)
    if hi - lo < 2:
        return False
    seg = y_d[lo + 1:hi]
    if seg.size == 0:
        return False
    valley = float(seg.min())
    base = _baseline(y_d)
    h_lo = float(y_d[lo]) - base
    h_hi = float(y_d[hi]) - base
    if h_lo <= 0 or h_hi <= 0:
        return False
    return valley <= min(float(y_d[lo]), float(y_d[hi])) - frac * min(h_lo, h_hi)


def _rise_smooth_width(t: np.ndarray, y: np.ndarray) -> int:
    """判断"有没有温升"时用的平滑宽度。

    目的只是把量化噪声压下去，让真实的抬升显出来。取约 200 ms——
    比 LFA 的升温沿（几十 ms 到百 ms）宽，但远小于一次实验的时长，
    所以不会把真实的峰抹平。同时限制在总点数的 1/40 以内，
    短数据也能用。
    """
    n = y.size
    if n < 8:
        return 1
    dur = float(t[-1] - t[0]) if n > 1 else 0.0
    if dur <= 0:
        return 1
    fs = (n - 1) / dur
    w = int(round(fs * 0.2))
    return int(max(1, min(w, max(3, n // 40))))


def find_peaks(t: np.ndarray, temp: np.ndarray, count: int,
               pulse_width_s: float = 0.0,
               max_points: int = 20000,
               min_height_ratio: float = 0.30,
               noise_sigmas: float = 4.0) -> list[Peak]:
    """在整段数据里找出 ``count`` 个最显著的峰，按时间排序。

    **判据是"相对局部基线的高度"，不是 prominence。**

    最初用的是 prominence（峰与两侧最低鞍部之差）。实测发现它在噪声上会
    失效：纯噪声里一个小起伏的"两侧最低点"会被一路走低到几百点之外，
    算出的 prominence 可达 0.19 °C，而真实噪声只有 0.03 °C——于是噪声
    起伏全被当成峰，"配置 N 个脉冲就找到 N 个峰"永远成立，实验异常永远
    判不出来。

    改用峰高后判据变得物理直观：一次脉冲的温升会明显高于它之前的冷态基线，
    而噪声起伏只有基线附近 ±3σ 的抖动。

    流程：轻微平滑 → 分桶求局部基线 → 候选 = 高于基线的局部极大 →
    过滤掉峰高低于 ``max(min_height_ratio × 最大峰高, noise_sigmas × σ)`` 的
    → 按峰高取前 ``count`` 个（强制最小间隔，防止同一峰顶的锯齿被数两次）
    → 回到原始分辨率精修位置，峰顶平台取时间均值。

    找不到足够的峰时返回已有的（调用方据此判定实验异常）。
    """
    n = temp.size
    if n < 5 or count < 1:
        return []

    raw = np.asarray(temp, dtype=np.float64)
    # 只用轻微平滑做识别：太强的平滑会把相邻两个脉冲并成一个
    fs = n / max(t[-1] - t[0], 1e-9) if n > 1 else 1.0
    light_w = max(1, int(round(fs * 0.004)))        # ≈4 ms
    s = boxcar(raw, light_w)

    idx_d, y_d, d = _decimate_max(s, max_points)
    base = _baseline(y_d)
    height = y_d - base
    is_max = _local_max(y_d)

    # 噪声基准必须用**原始**数据：平滑把噪声压低约 √width 倍，
    # 用平滑后的 σ 会把阈值定到噪声以下。再折算回平滑后的尺度。
    sigma = robust_sigma(raw) / math.sqrt(max(1, light_w))

    peak_h = float(np.max(height)) if height.size else 0.0
    if peak_h <= 0:
        return []
    thresh = max(peak_h * min_height_ratio, noise_sigmas * sigma, 1e-9)

    cand = [(int(j), float(height[j]))
            for j in np.nonzero(is_max)[0] if height[j] >= thresh]
    if not cand:
        return []

    # 按峰高降序挑峰。两个候选能否并存，取决于它们之间有没有"真正的谷"
    # （见 :func:`_has_valley`）——峰顶/衰减尾上的噪声起伏没有谷，会被并掉；
    # 真正分开的两次脉冲之间有回落，才会各算一个峰。
    cand.sort(key=lambda kv: kv[1], reverse=True)
    picked: list[int] = []
    for j, _ in cand:
        if all(_has_valley(y_d, j, k) for k in picked):
            picked.append(j)
            if len(picked) >= count:
                break
    picked.sort()

    # 峰的定位窗口：取相邻峰间隔的一半，首尾峰向外延伸同样的量
    ts = [float(t[idx_d[j]]) for j in picked]
    gaps = [b - a for a, b in zip(ts, ts[1:])] or [0.05]

    peaks: list[Peak] = []
    for k_i, j in enumerate(picked):
        tj = ts[k_i]
        if len(ts) > 1 and k_i < len(ts) - 1:
            half = max(gaps[k_i] / 2.0, 1e-3)
        elif len(ts) > 1:
            half = max(gaps[-1] / 2.0, 1e-3)
        else:
            half = max((t[-1] - t[0]) / 2.0, 1e-3)
        lo = max(0, int(np.searchsorted(t, tj - half, "left")))
        hi = min(n, max(int(np.searchsorted(t, tj + half, "right")), lo + 1))
        seg = s[lo:hi]
        k = int(seg.argmax()) + lo

        # 峰顶平台：与峰顶相差不到 5% 幅度的点取时间均值
        # （对应需求里"一个脉冲内有多个峰点时取均值"）
        rise = float(s[k] - seg.min())
        band = max(1e-9, 0.05 * rise)
        near = np.nonzero(seg >= s[k] - band)[0] + lo
        t_peak = float(np.mean(t[near])) if near.size else float(t[k])

        peaks.append(Peak(t=t_peak, temp_c=float(raw[k]), index=k))
    return peaks


# --------------------------------------------------------------------------
# 分析与截取
# --------------------------------------------------------------------------

def _sub(i: int) -> str:
    """下标数字（1→₁），用于 AM₁ / M₁M₂ 这类标注。"""
    table = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
    return str(i).translate(table)


def analyze(t: np.ndarray, temp: np.ndarray,
            cfg: ExperimentConfig) -> AnalysisResult:
    """对一段实验数据做峰值识别 + 智能截取。

    截取规则（需求原文）：
    * 单脉冲：``A—M : M—B' = 4 : 1``，即 ``B' = M + (M - A)/4``；
    * 多脉冲：取**最后一段**峰间距为尺度，
      ``M(n-1)—Mn : Mn—B' = 4 : 1``，即 ``B' = Mn + (Mn - M(n-1))/4``。
    """
    res = AnalysisResult()
    n = temp.size
    if n < 20:
        res.ok = False
        res.reason = f"数据点太少（{n} 点），无法识别温升特征"
        return res

    t_a = float(t[0])
    t_b = float(t[-1])
    res.t_b = t_b

    # ---- 温升幅度：必须是"成形的抬升"，不是极端分位数之差 ----
    # 最初用 p99.5 - p0.5。真机上栽了跟头：静止物体的 3.3 万个样本里，
    # 这个差值稳定测出 0.70 °C，而曲线其实完全平坦（均值 22.12~22.15），
    # 差别全部来自 0.1 °C 的量化噪声尾部。于是"没遮挡"也被判成有温升。
    #
    # 正确做法：先平滑掉噪声，再取幅度。平滑后如果还有明显的抬升，
    # 那才是真实的温升；纯噪声被平滑后起伏会落到噪声水平以下。
    raw = np.asarray(temp, dtype=np.float64)
    sigma = robust_sigma(raw)                       # 单点噪声 σ
    smooth_w = _rise_smooth_width(t, raw)
    sm = boxcar(raw, smooth_w)
    # 平滑掉量化噪声后，曲线的"总抬升幅度"才是真实温升。
    # 不平滑直接取 p99.5-p0.5 会把噪声尾部算进来：真机静止物体 3.3 万点
    # 上该值稳定测出 0.70 °C，而实际曲线完全平坦（均值 22.12~22.15）。
    rise = float(np.percentile(sm, 99.5) - np.percentile(sm, 0.5))
    res.delta_t = rise
    res.noise_c = float(sigma)
    # 幅度相当于多少个单点噪声 σ。这是个朴素、可解释的量：
    # 真机静止物体约 1.3σ（0.14/0.10），真实温升是几十到上百 σ。
    res.rise_snr = rise / sigma if sigma > 0 else 0.0

    # 判据只有一条硬线：平滑后的抬升必须超过绝对下限。
    # 之所以不再叠加信噪比闸门——试过两种定义（÷√w 理论白噪声、
    # ÷二阶差分残余），都在真机上给出了没有物理意义的数字
    # （纯噪声也能算出"SNR 200+"），反而比单看幅度更容易误导。
    if not np.isfinite(rise) or rise < MIN_RISE_C:
        res.ok = False
        res.reason = (
            f"未检测到明显温升（平滑后幅度 {rise:.2f} °C，"
            f"仅约 {res.rise_snr:.1f} 倍噪声 σ={sigma:.2f} °C，"
            f"阈值 {MIN_RISE_C} °C），判定实验实际未开始")
        return res

    # ---- 极性检查：必须是"升温"，不能是"下陷" ----
    # 遮挡（挡住辐射）会让读数**下陷**，这与 LFA 的"激光加热→升温"是方向相反的
    # 物理过程。若不查极性，一次纯遮挡也会被当成实验成功。
    #
    # 判据：比较曲线"向上凸出的量"与"向下凹陷的量"。
    # 正常 LFA：样品在时间零点前后处于环境温度，之后被脉冲抬高 → up >> dn；
    # 纯遮挡  ：读数只在遮挡期间被压低，其余时间贴环境温度 → dn >> up。
    #
    # 参考电平取「起始段中位数」与「全程中位数」中**较低**的那个：
    #   * 用起始段：符合 LFA 语义（零点时样品就是环境温度），单调上升的
    #     曲线（升温后不回落）也能正确判成"向上"；
    #   * 同时不准高于全程中位数：防止起始段恰好落在噪声高点而虚增 dn。
    #
    # 试过三种更细的判据，都在正常曲线上误伤，已弃用：
    #   * "峰两侧都要有更低的点"——冷却段 τ 在秒量级，峰后 ±0.4 s 仍贴着
    #     峰顶，1~3 脉冲的正常曲线全被误判成谷；
    #   * "峰点是局部窗口内最大值"——200 ms 平滑把峰顶整体后移约 w/2，
    #     峰点值必然小于窗口最大值，正常峰同样全挂；
    #   * "最低点是否在最高点之后"——末段衰减到接近起始温度时最低点常落在最后。
    count = max(1, int(cfg.pulse_count))
    peaks = find_peaks(t, temp, count, pulse_width_s=cfg.pulse_width_s)

    if sm.size > 10:
        head = sm[: max(10, sm.size // 10)]
        base = min(float(np.median(head)), float(np.median(sm)))
        up = float(np.percentile(sm, 99.5) - base)
        dn = float(base - np.percentile(sm, 0.5))
        if dn > up + MIN_RISE_C:
            dip_t = float(t[int(np.argmin(sm))])
            lowest = float(np.min(sm))
            res.ok = False
            res.peaks = peaks
            res.reason = (
                f"曲线的主要形态是**下陷**而不是升温（相对基线向上 "
                f"{up:.2f} °C、向下 {dn:.2f} °C，最低点在 {dip_t:.2f} s 处 "
                f"{lowest:.2f} °C）。被识别出的极值是谷而不是峰，这通常意味着"
                f"遮挡挡住了辐射，而不是样品被加热。LFA 需要「先升温、后冷却」。"
                f"若只是在验证遮挡是否有效，这个结果符合预期；"
                f"正式测量请让样品先被加热。")
            return res

    if len(peaks) < count:
        res.ok = False
        res.peaks = peaks
        res.reason = (f"配置了 {count} 个脉冲，但只识别到 {len(peaks)} 个温度峰")
        return res

    res.peaks = peaks

    # ---- 截取窗口 ----
    if len(peaks) == 1:
        m1 = peaks[0].t
        tail = (m1 - t_a) / TAIL_RATIO
        t_end = m1 + tail
    else:
        mn = peaks[-1].t
        mprev = peaks[-2].t
        tail = (mn - mprev) / TAIL_RATIO
        t_end = mn + tail

    t_end = max(t_end, peaks[-1].t)
    # 4:1 要求 B' 落在数据范围内。若峰太靠近末尾（人工结束得太早），
    # B' 会超出，只能钳到 t_b —— 此时比例不再是 4:1。
    # 早期版本静默钳位，界面上仍写着"4:1"却显示 9.3:1，属于欺骗性输出。
    # 现在显式记下 clamped，界面与结论里都会如实标注。
    if t_end > t_b + 1e-9:
        res.window_clamped = True
        t_end = t_b
    res.window = (t_a, t_end)

    # ---- 标注区间：A→M1, M1→M2, ..., Mn→B' ----
    bounds = [t_a] + [p.t for p in peaks] + [t_end]
    labels = []
    if len(peaks) == 1:
        # 需求里单脉冲写作 AM 与 MB'；这里统一成带下标的形式，与多脉冲一致
        labels = ["AM₁", "M₁B′"]
    else:
        labels.append("AM₁")
        for i in range(1, len(peaks)):
            labels.append(f"M{_sub(i)}M{_sub(i + 1)}")
        labels.append(f"M{_sub(len(peaks))}B′")

    res.intervals = [
        Interval(t0=bounds[i], t1=bounds[i + 1], label=labels[i])
        for i in range(len(bounds) - 1)
    ]
    return res


# --------------------------------------------------------------------------
# 结论文本
# --------------------------------------------------------------------------

def build_conclusion(cfg: ExperimentConfig, res: AnalysisResult,
                     unit: str = "C") -> str:
    """生成 result_*/实验结论.md 的正文。"""

    def fmt_t(x: float) -> str:
        return f"{x:.4f} s"

    lines = [
        "# LFA 变温导热系数测量 — 实验结果",
        "",
        f"- 实验开始：{cfg.started_at or '—'}",
        f"- 实验结束：{cfg.ended_at or '—'}",
        f"- 采样点：{cfg.n_samples:,}",
        f"- 平均采样率：{cfg.sample_rate_hz:,.0f} Hz",
        f"- 启动延迟：{cfg.start_delay_s * 1000:.0f} ms（零点至首点）",
        "",
        "> 启动延迟 = 按下「开始实验」到采到第一个数据点的时间。这段时间内"
        "没有数据，零点只是一个时间标记。接入机械遮挡/硬件触发后会显著减小。",
        "",
        "## 实验配置",
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        f"| 样品材料 | {cfg.material or '（未填写）'} |",
        f"| 比热容 | {cfg.specific_heat:g} J/(kg·K) |",
        f"| 密度 | {cfg.density:g} kg/m³ |",
        f"| 测温半径 | {cfg.radius_mm:g} mm |",
        f"| 环境温度 | {cfg.ambient_k:g} K |",
        f"| 激光功率 | {cfg.laser_power_w:g} W |",
        f"| 激光脉冲 | {cfg.pulse_width_s:g} s × {cfg.pulse_count} |",
        "",
        "## 温升特征",
        "",
        f"- 最大温升：{res.delta_t:.2f} °C",
        f"- 结束实验时刻 B：{fmt_t(res.t_b)}",
    ]

    if res.ok and res.peaks:
        lines += ["", "### 峰点", "", "| 峰 | 时刻 | 温度 |", "| --- | --- | --- |"]
        for i, p in enumerate(res.peaks, 1):
            lines.append(f"| M{_sub(i)} | {fmt_t(p.t)} | {p.temp_c:.2f} °C |")

        lines += ["", "### 标注区间", "",
                  "| 区间 | 起点 | 终点 | 时长 |", "| --- | --- | --- | --- |"]
        for iv in res.intervals:
            lines.append(f"| {iv.label} | {fmt_t(iv.t0)} | {fmt_t(iv.t1)} "
                         f"| {iv.duration:.4f} s |")
        if res.window is not None:
            lines.append("")
            if res.window_clamped:
                lines.append(
                    f"截取窗口：{fmt_t(res.window[0])} — {fmt_t(res.window[1])}"
                    "（⚠ 峰太靠近结束时刻，B′ 被截到 B，实际不是 4:1）")
            else:
                lines.append(f"截取窗口：{fmt_t(res.window[0])} — "
                             f"{fmt_t(res.window[1])}（峰前 : 峰后 = "
                             f"{TAIL_RATIO:g} : 1）")
    else:
        lines += ["", f"> ⚠ 实验异常：{res.reason}",
                  ">", "> 曲线按完整时间窗口展示，未做截取与标注。"]

    lines += [
        "",
        "## 结论",
        "",
    ]
    if res.ok:
        lines.append(
            f"样品在 {cfg.pulse_count} 个脉冲下出现 {len(res.peaks)} 个可辨识的温度峰，"
            f"最大温升 {res.delta_t:.2f} °C。")
    else:
        lines.append("本次实验未获得可用的温升曲线，建议检查激光遮挡、"
                     "测温对准与平均时间设置后重测。")

    lines += [
        "",
        "> 热导率与热扩散系数：理论公式及修正项仍在研究中，本版本**未计算**。",
        "",
    ]
    return "\n".join(lines)


def config_to_json(cfg: ExperimentConfig, res: AnalysisResult,
                   unit: str = "C") -> str:
    """导出的 config.json：配置 + 分析摘要，便于将来「分析」模块直接读取。"""
    payload = {
        "format": "lfa-result/1",
        "config": cfg.to_dict(),
        "analysis": {
            "ok": res.ok,
            "reason": res.reason,
            "delta_t_c": res.delta_t,
            "t_b": res.t_b,
            "window": list(res.window) if res.window else None,
            "window_clamped": res.window_clamped,
            "peaks": [{"t": p.t, "temp_c": p.temp_c} for p in res.peaks],
            "intervals": [
                {"label": iv.label, "t0": iv.t0, "t1": iv.t1,
                 "duration": iv.duration}
                for iv in res.intervals
            ],
        },
        "display": {"temp_unit": unit},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
