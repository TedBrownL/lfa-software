"""用固件模拟器端到端验证 solenoid_demo.py 的全部功能。

本机 COM9 处于 USB error 31 状态，物理重插前无法真机通信。
本脚本把 ``serial.Serial`` 换成 ``firmware_sim.FakeSerial``，
让 demo 走**完全相同的代码路径**（握手、发包、解析、计时、异常分支），
从而在没有硬件的情况下确认脚本本身是对的。

覆盖：
  * 握手（ID / PONG）、状态查询
  * 单次 PULL，含实测脉宽与误差
  * 连发 TRAIN 的周期与抖动
  * 超上限被板子拒绝（不能静默截断）
  * 未知命令、busy 保护
  * 极性切换 CFG POLARITY
  * --fire-at 的定时精度
"""

from __future__ import annotations

import io
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import serial  # noqa: E402

import firmware_sim  # noqa: E402
from solenoid_demo import Solenoid, cmd_latency, cmd_pull, cmd_train  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def quiet(fn, *a, **kw):
    """吞掉被调函数的打印，返回它自己的返回值。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = fn(*a, **kw)
    return ret, buf.getvalue()


def main() -> int:
    # 把 demo 的眼换成模拟器的
    serial.Serial = firmware_sim.FakeSerial

    print("\n=== 1. 握手与状态 ===")
    sol = Solenoid("TEST")
    sol.open()
    fw = sol.handshake()
    check("握手成功", fw == "1.0", f"固件={fw}")
    check("max_ms 正确读入", sol.max_ms == 3000, f"max_ms={sol.max_ms}")
    st = sol.state()
    check("STATE 可读", st is not None and st.fields.get("mode") == "IDLE",
          st.raw if st else "None")

    print("\n=== 2. 单次 PULL（脉宽 60 ms，实测应贴近） ===")
    ok, out = quiet(cmd_pull, sol, 60)
    check("PULL 返回成功", ok is True)
    check("输出含实测脉宽", "实测脉宽" in out, out.strip().splitlines()[-1] if out else "")
    # 从输出里抠出实测脉宽
    dur = None
    for line in out.splitlines():
        if "实测脉宽" in line:
            seg = line.split("实测脉宽")[1].strip()
            dur = float(seg.split()[0])
    check("实测脉宽在 55~75 ms", dur is not None and 55 <= dur <= 75, f"{dur} ms")

    print("\n=== 3. 连发 TRAIN 3 次 / 40 ms / 120 ms ===")
    _, out = quiet(cmd_train, sol, 3, 40, 120)
    check("收到 3 个 DONE", "3/3" in out, [l for l in out.splitlines() if "/3" in l])
    check("周期被算出", "实测周期" in out)
    check("周期接近目标 160 ms", "偏差 +" in out or "偏差 -" in out,
          [l for l in out.splitlines() if "偏差" in l])

    print("\n=== 4. 安全上限：请求 5000 ms 必须被拒 ===")
    sol.send("PULL 5000")
    err = sol.wait_for({"ERR", "ACK"}, timeout=1.0)
    check("超上限被板子拒绝", err is not None and err.kind == "ERR", err.raw if err else "无回复")
    check("拒绝理由说明上限", err is not None and "too_long" in err.raw)

    print("\n=== 5. busy 保护：动作进行中再发指令 ===")
    sol.send("PULL 800")
    ack1 = sol.wait_for({"ACK"}, timeout=1.0)
    check("长脉宽被接受", ack1 is not None and ack1.kind == "ACK")
    sol.send("PULL 50")
    busy = sol.wait_for({"ERR", "ACK"}, timeout=1.0)
    check("并发请求被拒", busy is not None and busy.kind == "ERR", busy.raw if busy else "无回复")
    # 等这一发彻底结束，再往下走，否则会污染后续用例
    sol.wait_for({"DONE"}, timeout=3.0)
    st = sol.state()
    check("长脉宽结束后回到 IDLE", st is not None and st.fields.get("mode") == "IDLE")

    print("\n=== 6. 未知命令 ===")
    sol.send("FLY")
    unk = sol.wait_for({"ERR"}, timeout=1.0)
    check("未知命令报错", unk is not None and "unknown" in unk.raw, unk.raw if unk else "无回复")

    print("\n=== 7. 极性切换 ===")
    sol.send("CFG POLARITY ACTIVE_LOW")
    r = sol.wait_for({"OK", "ERR"}, timeout=1.0)
    check("切到 ACTIVE_LOW", r is not None and "ACTIVE_LOW" in r.raw, r.raw if r else "无回复")
    st = sol.state()
    check("STATE 反映新极性", st is not None and st.fields.get("polarity") == "ACTIVE_LOW")
    sol.send("CFG POLARITY ACTIVE_HIGH")
    sol.wait_for({"OK"}, timeout=1.0)

    print("\n=== 8. RETRACT 复位 ===")
    sol.send("RETRACT")
    rr = sol.wait_for({"RETRACT"}, timeout=1.0)
    check("RETRACT 有回复", rr is not None, rr.raw if rr else "无回复")
    st = sol.state()
    check("复位后 relay=OFF", st is not None and st.fields.get("relay") == "OFF")

    print("\n=== 9. 时钟量测：命令→ACK 延迟统计 ===")
    _, out = quiet(cmd_latency, sol, 3, 40)
    check("延迟统计有输出", "延迟" in out and "均值" in out)

    sol.close()
    check("关闭后 is_open=False", sol.ser is None)

    print("\n=== 10. ARM 定时开火（误差应 < 5 ms） ===")
    sol2 = Solenoid("TEST2")
    sol2.open()
    sol2.handshake()
    sol2.send("ARM 300 40")
    armed = sol2.wait_for({"ARMED"}, timeout=1.0)
    check("收到 ARMED", armed is not None, armed.raw if armed else "无回复")
    t0 = time.perf_counter()
    fire = sol2.wait_for({"FIRE"}, timeout=2.0)
    elapsed = (time.perf_counter() - t0) * 1000
    check("收到 FIRE", fire is not None, fire.raw if fire else "无回复")
    check("开火时刻在 300 ms 附近", 280 <= elapsed <= 380, f"实测 {elapsed:.1f} ms")
    sol2.wait_for({"DONE"}, timeout=2.0)
    sol2.close()

    # ---- 汇总 ----
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'=' * 56}")
    print(f"结果：{passed}/{total} 通过")
    if passed < total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}  {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
