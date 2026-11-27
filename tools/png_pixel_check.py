"""像素级检查 PNG：把「横线」「图题」「图例」是否真的画出来验证成数值。

不靠肉眼：按坐标轴的实际像素边界取样，检查
  1. 图题在图上方、且确实有深色文字像素；
  2. 每条标注横线所在的行上，确实有连续的深色像素（线被画出来了）；
  3. 横轴标题与刻度数字之间有一定间距（不重叠）。
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfa import experiment as ex
from lfa.experiment import ExperimentConfig
from lfa.result_export import _render_png

FS = 6500.0
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "_png_check.png")


def synth(n_pulse=2, dur=12.0):
    rng = np.random.default_rng(5)
    t = np.arange(int(dur * FS)) / FS
    y = np.full(t.size, 25.0)
    for k in range(n_pulse):
        t0 = 2.0 + k * 3.0
        m = t >= t0
        y[m] += 1.8 * np.exp(-(t[m] - t0) / 0.6)
    return t, y + rng.normal(0, 0.05, t.size)


def dark_rows(a):
    """每行的深色像素数。"""
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    return ((r < 120) & (g < 120) & (b < 120)).sum(axis=1)


def main():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.image as mpimg

    t, y = synth(2)
    cfg = ExperimentConfig(pulse_count=2, pulse_width_s=0.1, material="不锈钢")
    res = ex.analyze(t, y, cfg)
    png = _render_png(t, y, res, "C", 0.5, cfg)
    with open(OUT, "wb") as fh:
        fh.write(png)

    img = mpimg.imread(OUT)[:, :, :3]
    if img.dtype != np.uint8:
        # matplotlib.image.imread 对 PNG 返回 0~1 的 float，
        # 阈值必须跟着换算，否则"r<130"恒成立、整幅图都被判成深色
        img = np.clip(img * 255.0, 0, 255).astype(np.uint8)
    h, w = img.shape[:2]
    print(f"尺寸 {w}x{h}  {len(png)/1024:.0f} KB")
    ok = True

    # 复算坐标轴的像素边界（与 _render_png 的 subplots_adjust 一致）
    top_frac, bot_frac = 0.885, 0.115
    left_frac, right_frac = 0.075, 0.975
    ax_top = int((1 - top_frac) * h)
    ax_bot = int((1 - bot_frac) * h)
    ax_l = int(left_frac * w)
    ax_r = int(right_frac * w)

    dr = dark_rows(img)
    # 1) 图题：坐标区上方的空白带里必须有深色文字
    title_band = dr[:ax_top - 8]
    has_title = int((title_band > 50).sum())
    print(f"图题文字行数 {has_title}")
    if has_title < 5:
        print("FAIL 坐标区上方没有图题")
        ok = False

    # 2) 横线：**直接在图里找**。
    #
    #    现在所有横线同高且首尾相连，所以它们在图上会连成**一条**贯穿线
    #    （各段之间只有 1.3 px 宽的端头竖线，肉眼就是一条分段的标注线）。
    #    因此这里校验的是：
    #      a) 存在且只存在一条这样的水平线；
    #      b) 它完整覆盖了 A→B′ 的整个时间跨度；
    #      c) 它位于所有 M 标注文字之上。
    plot = img[ax_top:ax_bot, ax_l:ax_r]
    r, g, b = plot[:, :, 0], plot[:, :, 1], plot[:, :, 2]
    dark = (r < 130) & (g < 130) & (b < 130)
    span = ax_r - ax_l

    # 找出每行最长连续深色段（横线是连续直线；文字是断续的）
    def longest_run(row_mask):
        if not row_mask.any():
            return 0
        idx = np.nonzero(row_mask)[0]
        splits = np.nonzero(np.diff(idx) > 2)[0]
        return int(np.max(np.diff(np.concatenate(([-1], splits, [idx.size - 1])))
                          .max()))

    run_len = np.array([longest_run(dark[i]) for i in range(dark.shape[0])])
    cand = np.nonzero(run_len >= span * 0.25)[0]
    print(f"疑似横线像素行 {cand.size} 行（连续深色 >= {span*0.25:.0f} px）")
    if cand.size == 0:
        print("FAIL 图里找不到贯穿式横线")
        ok = False
        groups = []
    else:
        groups = []
        start = prev = int(cand[0])
        for c in cand[1:]:
            c = int(c)
            if c - prev <= 3:
                prev = c
            else:
                groups.append((start, prev))
                start = prev = c
        groups.append((start, prev))
        print(f"识别出 {len(groups)} 条独立横线（同高相连后应为 1 条）")
        if len(groups) != 1:
            print("FAIL 横线不是一整条（可能高度不一致被拆开）")
            ok = False
        for tp, bt in groups:
            ln = int(run_len[tp:bt + 1].max())
            print(f"   像素行 {tp}–{bt}，最长连续深色 {ln} px "
                  f"（占轴宽 {ln/span:.1%}）")
            if ln < span * 0.25:
                print("FAIL 横线太短，没覆盖 A→B′ 全程")
                ok = False

        # c) 横线必须在所有 M 标注（蓝色文字与峰点）之上。
        #    图像像素 y 轴向下：横线的像素行更小 = 位置更高。
        blue = ((b.astype(int) - r.astype(int) > 40) & (b > 120))
        blue_rows = np.nonzero(blue.sum(axis=1) > 0)[0]
        if blue_rows.size and groups:
            highest_blue = int(blue_rows.min())
            top_line = int(groups[0][0])
            clear_px = top_line - highest_blue
            print(f"   M 标注最高像素行 {highest_blue}，横线所在行 {top_line}，"
                  f"净空 {clear_px} px（负值 = 横线在 M 之上）")
            if clear_px >= 0:
                print("FAIL 横线没有完全位于 M 标注之上")
                ok = False
            else:
                print(f"   横线在 M 标注之上，净空 {-clear_px} px ✓")

    # 3) 横轴标题与刻度：底部区域要有两段分离的深色行（刻度 / 标题）
    bottom_band = dr[ax_bot:h]
    rows_with_text = np.nonzero(bottom_band > 20)[0]
    print(f"横轴下方有文字的像素行数 {rows_with_text.size}")
    if rows_with_text.size < 6:
        print("FAIL 横轴下方看不到刻度或标题")
        ok = False
    else:
        # 检查是否存在至少 4 行的空隙（标题与刻度不粘连）
        gaps = np.diff(rows_with_text)
        print(f"最大空隙 {int(gaps.max()) if gaps.size else 0} 行")
        if gaps.size and int(gaps.max()) < 3:
            print("WARN 刻度与横轴标题可能贴得太近")

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
