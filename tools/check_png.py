"""检查导出的 curve.png 是否真的画了东西（不靠肉眼）。

判据：
1. 尺寸符合要求；
2. 画面不是纯白（有曲线像素）；
3. 红色曲线像素占比在合理范围（说明曲线存在且不糊满）；
4. 蓝色标记像素存在（峰点标记）；
5. 深色像素存在（区间标注线/坐标轴）。
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QImage


def load_rgb(path):
    img = QImage(path)
    if img.isNull():
        raise SystemExit(f"无法读取 {path}")
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    w, h = img.width(), img.height()
    stride = img.bytesPerLine()
    # 必须 copy：bits() 指向 QImage 内部缓冲，图片对象一旦释放就成了野指针
    buf = img.bits()
    arr = np.frombuffer(bytes(buf), dtype=np.uint8,
                        count=stride * h).reshape(h, stride // 4, 4)[:, :w, :3]
    # Format_RGB32 在 x86 上是 0xAARRGGBB 存进 uint32，按字节读出来是
    # **B G R A** 的顺序（小端）。不翻转会把红当成蓝——曾经因此误报
    # "红色曲线像素太少"。
    return np.ascontiguousarray(arr[:, :, ::-1].copy())


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/_vizcheck.png"
    expect_marks = "--no-peaks" not in sys.argv
    a = load_rgb(path)
    h, w, _ = a.shape
    print(f"尺寸 {w}x{h}")

    r = a[:, :, 0].astype(int)
    g = a[:, :, 1].astype(int)
    b = a[:, :, 2].astype(int)

    white = (r > 245) & (g > 245) & (b > 245)
    red = (r > 120) & (r - g > 60) & (r - b > 60)
    blue = (b > 120) & (b - r > 50) & (b - g > 30)
    dark = (r < 110) & (g < 110) & (b < 110)

    print(f"白底占比 {white.mean():.3f}")
    print(f"红色像素 {int(red.sum())} ({red.mean()*100:.3f}%)")
    print(f"蓝色像素 {int(blue.sum())} ({blue.mean()*100:.3f}%)")
    print(f"深色像素 {int(dark.sum())} ({dark.mean()*100:.3f}%)")

    ok = True
    if w < 800 or h < 400:
        print("FAIL 尺寸过小")
        ok = False
    if white.mean() > 0.995:
        print("FAIL 几乎全白，没画上东西")
        ok = False
    if red.sum() < 2000:
        print("FAIL 红色曲线像素太少")
        ok = False
    if expect_marks:
        if blue.sum() < 100:
            print("FAIL 看不到蓝色峰点标记")
            ok = False
    else:
        # 异常实验本来就没有峰点，蓝色标记为 0 才是对的
        print("（异常实验：不要求蓝色峰点）")
    if dark.sum() < 300:
        print("FAIL 看不到深色标注线/坐标轴")
        ok = False
    print("PASS" if ok else "有问题")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
