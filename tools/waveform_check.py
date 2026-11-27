"""复核：手动遮挡的波形形态能否被正确识别。

手动遮挡与激光脉冲的物理差别很大：
* 遮挡是"挡住辐射 → 读数下降 → 移开 → 回升"，形状接近方波，
  且下降/上升的边沿是秒级（手速），不是毫秒级。
* 遮挡期间读数处在**低**位，不是一个"峰"。若样品被激光加热后遮挡，
  遮挡移除时才会出现真正的温度峰。

本脚本覆盖三种形态，确认识别逻辑不会把手动遮挡的典型波形判错。
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfa import experiment as ex
from lfa.experiment import ExperimentConfig

_fail = []


def check(cond, name, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (f"   [{detail}]" if detail else ""))
    if not cond:
        _fail.append(name)


FS = 6500.0


def form_a(n_pulse=1, dur=8.0, rise=2.0, noise=0.10, tau=0.5, seed=11):
    """A：加热脉冲（激光打到样品），快升慢降 —— 标准 LFA 形态。"""
    rng = np.random.default_rng(seed)
    t = np.arange(int(dur * FS)) / FS
    y = np.full(t.size, 25.0)
    for k in range(n_pulse):
        t0 = 1.5 + k * 2.0
        m = t >= t0
        y[m] += rise * np.exp(-(t[m] - t0) / tau)
    return t, y + rng.normal(0, noise, t.size)


def form_b(n_shut=1, dur=8.0, depth=2.0, noise=0.10, seed=12, width=0.6):
    """B：遮挡式（读数被压低）—— 波形是向下的方波。"""
    rng = np.random.default_rng(seed)
    t = np.arange(int(dur * FS)) / FS
    y = np.full(t.size, 25.0)
    for k in range(n_shut):
        t0 = 1.5 + k * 2.0
        m = (t >= t0) & (t < t0 + width)
        y[m] -= depth
    return t, y + rng.normal(0, noise, t.size)


def form_c(n_pulse=1, dur=8.0, rise=2.0, noise=0.10, seed=13):
    """C：遮挡后打开、样品缓慢升温 —— 带 1 s 上升沿的梯形。"""
    rng = np.random.default_rng(seed)
    t = np.arange(int(dur * FS)) / FS
    y = np.full(t.size, 25.0)
    for k in range(n_pulse):
        t0 = 1.5 + k * 2.5
        m = t >= t0
        ramp = 1.0 - np.exp(-(t[m] - t0) / 1.0)
        y[m] += rise * ramp
    return t, y + rng.normal(0, noise, t.size)


def run(label, t, y, n_pulse, expect_ok, expect_n=None):
    cfg = ExperimentConfig(pulse_count=n_pulse, pulse_width_s=0.5)
    r = ex.analyze(t, y, cfg)
    detail = f"ok={r.ok} 峰={len(r.peaks)} ΔT={r.delta_t:.2f}"
    check(r.ok == expect_ok, label, detail)
    if expect_ok and expect_n is not None:
        check(len(r.peaks) == expect_n, f"  └ 峰数 = {expect_n}",
              str(len(r.peaks)))
    if r.window and r.ok:
        ratio = (r.peaks[0].t - r.window[0]) / max(r.window[1] - r.peaks[-1].t, 1e-9) \
            if len(r.peaks) == 1 else \
            (r.peaks[-1].t - r.peaks[-2].t) / max(r.window[1] - r.peaks[-1].t, 1e-9)
        check(abs(ratio - 4.0) < 0.05, "  └ 4:1 截取", f"{ratio:.3f}")
    return r


def main() -> int:
    print("[A] 标准加热脉冲（快升慢降）")
    for n in (1, 2, 3):
        t, y = form_a(n_pulse=n)
        run(f"  {n} 个加热脉冲", t, y, n, True, n)

    print("[B] 遮挡式向下方波（对应'挡住辐射'的形态）")
    t, y = form_b(n_shut=1)
    r = ex.analyze(t, y, ExperimentConfig(pulse_count=1, pulse_width_s=0.5))
    print(f"    ok={r.ok} 峰={len(r.peaks)} ΔT={r.delta_t:.2f}  [{r.reason[:40]}]")
    # 向下方波不是温升：必须被判为异常，否则"只是挡住了辐射"会被当成实验成功。
    check(not r.ok, "  向下方波判为异常（不是温升）",
          f"ok={r.ok} 峰={len(r.peaks)}")

    print("[C] 单调上升（遮挡移除后样品持续升温，无冷却段）")
    # 物理事实：1-exp 单调上升的曲线上没有局部极大点，软件无法、也不该
    # 从中找出"峰"。真实 LFA 必须是"升温→冷却"才有可拟合的信息。
    # 这里断言的是"不会把单调上升硬说成有 N 个峰"。
    t, y = form_c(n_pulse=1)
    r = ex.analyze(t, y, ExperimentConfig(pulse_count=1, pulse_width_s=0.5))
    check(r.ok, "  单脉冲单调上升：能识别出 1 个峰（在数据末尾）",
          f"峰={len(r.peaks)}")
    if r.window:
        check(r.window_clamped,
              "  └ 峰在末尾时明确标记 B′ 被截断（不谎称 4:1）",
              f"clamped={r.window_clamped}")

    t, y = form_c(n_pulse=2)
    r2 = ex.analyze(t, y, ExperimentConfig(pulse_count=2, pulse_width_s=0.5))
    check(not r2.ok,
          "  双脉冲单调上升：判为异常而不是硬凑 2 个峰",
          f"ok={r2.ok} 峰={len(r2.peaks)}")

    print("[D] 手动遮挡：低信噪比（噪声 0.15，温升 0.8）")
    t, y = form_a(n_pulse=1, rise=0.8, noise=0.15)
    run("  0.8 °C / σ0.15", t, y, 1, True, 1)

    print("[E] 手动遮挡：温升过小应判异常")
    t, y = form_a(n_pulse=1, rise=0.2, noise=0.10)
    r = ex.analyze(t, y, ExperimentConfig(pulse_count=1, pulse_width_s=0.5))
    check(not r.ok, "  0.2 °C 判异常", f"ΔT={r.delta_t:.2f}")

    print("[F] 真机静止数据（0.1 °C 量化 + σ≈0.105，模拟 Optris 实测）")    # 这一组对应本轮真机上踩到的坑：不平滑时 p99.5-p0.5 会稳定测出 0.70 °C，
    # 把完全静止的物体判成"有温升"。量化步长 0.1 °C 与真机一致。
    rng = np.random.default_rng(21)
    t = np.arange(int(6.0 * FS)) / FS
    y = np.round((22.13 + rng.normal(0, 0.105, t.size)) / 0.1) * 0.1
    r = ex.analyze(t, y, ExperimentConfig(pulse_count=1, pulse_width_s=0.5))
    check(not r.ok, "  静止 6 s 判为实验未开始",
          f"ΔT={r.delta_t:.2f} σ={r.noise_c:.3f}")

    print()
    if _fail:
        print(f"失败 {len(_fail)} 项：" + "、".join(_fail))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
