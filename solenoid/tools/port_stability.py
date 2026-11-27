"""区分「端口死于空闲」还是「端口死于继电器动作」。

背景：2026-11-26 真机上 `--pull 100` 成功过一次（实测脉宽 100.02 ms），
紧接着下一次打开就报 error 31，且设备没有重新枚举
（LastArrival 早于动作时刻）——说明 CH340 是"死在原地"而不是重启。

两种可能必须分开：
  A. 空闲/时间因素：USB 选择性挂起、驱动 bug，跟电磁铁无关；
  B. 动作因素：继电器吸合让 5V 轨塌陷 / 地线干扰，把 CH340 打死。

本脚本按阶段做，每阶段后都探一次端口是否还活着：

  阶段 1  只 PING，持续 20 s，不动作          → 测 A
  阶段 2  一次 PULL                          → 测 B
  阶段 3  再 PING 10 s                       → 看动作后是否还能活

哪个阶段后死掉，原因就落在哪个阶段。全程不执行危险操作。
"""

import sys

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
BAUD = 115200


def try_open(tag: str) -> serial.Serial | None:
    try:
        s = serial.Serial(PORT, BAUD, timeout=0.5)
        print(f"  [{tag}] 打开成功")
        return s
    except Exception as exc:
        print(f"  [{tag}] 打不开: {str(exc)[:110]}")
        return None


def ping(s: serial.Serial, n: int, gap_s: float, label: str) -> bool:
    """连发 n 次 PING，每次间隔 gap_s。返回是否全程成功。"""
    import time

    ok_count = 0
    for i in range(n):
        try:
            s.write(b"PING\n")
            s.flush()
            line = s.readline().decode("ascii", "replace").strip()
            if line.startswith("PONG"):
                ok_count += 1
                print(f"    {label} #{i + 1:<2} {line}")
            else:
                print(f"    {label} #{i + 1:<2} 异常回复: {line!r}")
                return False
        except Exception as exc:
            print(f"    {label} #{i + 1:<2} 写失败: {str(exc)[:100]}")
            return False
        time.sleep(gap_s)
    return ok_count == n


def main() -> int:
    print("=" * 62)
    print(f"端口稳定性分阶段测试  {PORT}")
    print("=" * 62)

    s = try_open("起始")
    if s is None:
        print("\n端口现在就打不开，先重插 USB 再跑本脚本。")
        return 2

    # ---- 阶段 1：只通信，不动作 ----
    print("\n阶段 1：只 PING 20 秒，不做任何动作（测空闲稳定性）")
    alive = ping(s, 20, 1.0, "PING")
    s.close()
    if not alive:
        print("\n>> 结论：不动作也会死。问题在 USB 空闲/驱动层，与电磁铁无关。")
        return 1
    print("  → 20 秒纯通信稳定，排除空闲挂起。")

    # ---- 阶段 2：一次动作 ----
    print("\n阶段 2：执行一次 PULL 100 ms")
    s = try_open("阶段2前")
    if s is None:
        print("\n>> 结论：仅仅关掉再打开就死了，跟动作无关，是驱动层问题。")
        return 1
    try:
        s.write(b"PULL 100\n")
        s.flush()
        for _ in range(10):
            line = s.readline().decode("ascii", "replace").strip()
            if line:
                print(f"    {line}")
            if line.startswith("DONE"):
                break
    except Exception as exc:
        print(f"    动作中写失败: {str(exc)[:110]}")
        return 1
    print("  PULL 已发出")
    s.close()

    # ---- 阶段 3：动作之后还能活吗 ----
    print("\n阶段 3：动作后再 PING 10 秒")
    image = None
    for attempt in range(3):
        image = try_open(f"阶段3-{attempt + 1}")
        if image:
            break
        import time
        time.sleep(1.0)
    if image is None:
        print("\n>> 结论：**动作之后端口就死了**。")
        print("   死因指向继电器/电磁铁的电气干扰，不是空闲挂起。")
        print("   优先查：")
        print("     1. 电磁铁是否从 UNO 的 5V 引脚取电？（必须改独立电源）")
        print("     2. 12V 电源负极是否接到了 UNO 的 GND？（必须断开，保持隔离）")
        print("     3. 继电器线圈两端有没有并联续流二极管？")
        return 1

    alive = ping(image, 10, 1.0, "PING")
    image.close()
    if alive:
        print("\n>> 结论：动作一次也能存活。可能需要连续多次动作才会触发。")
        return 0
    print("\n>> 结论：动作后通信中断。同上，查电气干扰。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
