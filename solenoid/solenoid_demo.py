"""推拉电磁铁 demo —— 独立测试脚本（尚未集成到 LFA 软件）

对应需求：已验证"UNO R3 --继电器-- 推拉电磁铁"接线与 Arduino IDE 上的控制，
本脚本的目标是把同一套控制搬到这台电脑上，并且**测出时间精度**，
因为后面要和红外测温仪的数据对齐时间零点。

固件侧为什么这么设计（先看 firmware/solenoid_firmware.ino 的注释）：
  * 脉宽由 UNO 自己用 micros() 计时，上位机只发"拉多久"，不靠掐表关断；
  * 每次动作回报板载时间戳（t_us），上位机据此刻算自己的时钟偏差。

本脚本做的四件事：
  1. 连上串口、认固件版本（握手失败会明确说原因，不静默）；
  2. PULL 单次推拉，量"命令发出 → 电磁铁动作"的端到端延迟；
  3. TRAIN 连发 N 次，量周期抖动（这决定多脉冲实验的时间准确度）；
  4. --fire-at 在指定墙钟时刻开火，模拟"软件时间零点"的用法。

用法（默认 COM9，可省略）::

    python solenoid_demo.py                 # 交互菜单，先看清楚再动手
    python solenoid_demo.py --port COM9 --pull 100
    python solenoid_demo.py --train 5 100 900
    python solenoid_demo.py --latency 10 50
    python solenoid_demo.py --watch            # 只打印板子主动发来的行
    python solenoid_demo.py --settle           # 发 RETRACT，确保挡片复位

安全提示
    推拉电磁铁是短时工作制。脉宽越大、连发越密，线圈越热。
    固件有 max_ms 上限（默认 3000），但**别拿上限当常用值**，
    挡片单次只需要 0.1 s 量级。
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from dataclasses import dataclass

try:
    import serial
except ImportError:
    sys.exit("缺少 pyserial，请用 D:/MINICONDA/envs/casimir/python.exe 运行")


DEFAULT_BAUD = 115200
DEFAULT_PORT = "COM9"


# --------------------------------------------------------------------------
# 串口封装：一行一条消息，带超时，失败要说清原因
# --------------------------------------------------------------------------


@dataclass
class Reply:
    """板子的一行回复。"""

    raw: str
    kind: str                 # 第一个词：READY / PONG / ACK / DONE / ERR / ...
    fields: dict[str, str]    # key=value 形式的字段
    t_recv: float             # 上位机收到这一行的 time.perf_counter()

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.fields.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.fields.get(key, default))
        except (TypeError, ValueError):
            return default


class Solenoid:
    """一块运行 solenoid_firmware 的 UNO。"""

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD, verbose: bool = False):
        self.port = port
        self.verbose = verbose
        self.baud = baud
        self.ser = None
        self.firmware = "?"
        self.max_ms = 3000

    # -- 生命周期 ---------------------------------------------------------

    def open(self) -> None:
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.2)
        except serial.SerialException as exc:
            raise RuntimeError(self._explain_open_error(exc)) from exc
        # CH340 打开口会让 UNO 复位，固件启动后会打一行 READY，等它
        self.ser.reset_input_buffer()
        time.sleep(1.6)
        self.ser.reset_input_buffer()

    def _explain_open_error(self, exc: Exception) -> str:
        msg = str(exc)
        hint = [
            f"打不开 {self.port}：{msg}",
            "",
            "按下面顺序排查：",
            "  1. 板子上电了没？USB 线是数据线（不是只充电的）？",
            "  2. 是不是 Arduino IDE 的串口监视器还开着？那个会独占串口。",
            "  3. 设备管理器里端口号变了？用 --port 指定，或先跑 list_ports。",
        ]
        if "31" in msg or "没有发挥作用" in msg:
            hint += [
                "",
                "特别地，错误 31 是本机的老毛病：驱动在、能打开句柄，",
                "但底层 USB 传输失败。**拔掉 USB 重插**基本都能解决；",
                "若反复出现，换一根线 / 换一个 USB 口再试。",
            ]
        return "\n".join(hint)

    def close(self) -> None:
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(b"RETRACT\n")
                self.ser.flush()
            except Exception:
                pass
            self.ser.close()
        self.ser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- 收发 -------------------------------------------------------------

    def send(self, text: str) -> float:
        """发一行，返回发出瞬间的 perf_counter()。"""
        if not self.ser:
            raise RuntimeError("串口没打开")
        self.ser.write((text + "\n").encode("ascii"))
        self.ser.flush()
        t_send = time.perf_counter()
        if self.verbose:
            print(f"  >> {text}")
        return t_send

    def read_line(self, timeout: float = 1.0) -> Reply | None:
        """读一行，超时返回 None。"""
        deadline = time.perf_counter() + timeout
        buf = b""
        while time.perf_counter() < deadline:
            chunk = self.ser.readline()
            if chunk:
                buf = chunk
                break
        if not buf:
            return None
        raw = buf.decode("ascii", "replace").strip()
        if not raw:
            return None
        parts = raw.split()
        kind = parts[0]
        fields: dict[str, str] = {}
        for token in parts[1:]:
            if "=" in token:
                k, v = token.split("=", 1)
                fields[k] = v
        if self.verbose:
            print(f"  << {raw}")
        return Reply(raw=raw, kind=kind, fields=fields, t_recv=time.perf_counter())

    def wait_for(self, kinds: set[str], timeout: float = 2.0) -> Reply | None:
        """等一个特定种类的回复，其它行（如 READY）忽略。"""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            r = self.read_line(timeout=max(0.02, deadline - time.perf_counter()))
            if r is None:
                break
            if r.kind in kinds:
                return r
        return None

    # -- 协议动作 ---------------------------------------------------------

    def handshake(self) -> str:
        """确认对面真的是电磁铁固件，而不是别的东西。"""
        self.send("ID")
        r = self.wait_for({"SOLENOID"}, timeout=1.5)
        if r is None:
            # 板子可能还在复位后的 READY 之前，再等一次
            r = self.wait_for({"SOLENOID"}, timeout=2.0)
        if r is None:
            self.send("PING")
            if self.wait_for({"PONG"}, timeout=1.5) is None:
                raise RuntimeError(
                    f"{self.port} 有响应但不是电磁铁固件。\n"
                    "烧写后需要重新插拔/等 2 秒让它复位完成。\n"
                    "若确认已烧写，检查波特率是否为 115200。"
                )
        # 固件回 "SOLENOID v1.0 pin=7 ..."，版本号不是 key=value 形式，
        # 正则单独抠，否则会一路读成 "?"
        m = re.search(r"\bv(\d+\.\d+)", r.raw)
        self.firmware = m.group(1) if m else "?"
        self.max_ms = r.get_int("max_ms", 3000)
        return self.firmware

    def state(self) -> Reply | None:
        self.send("STATE")
        return self.wait_for({"STATE"}, timeout=1.5)

    def pull(self, on_ms: int) -> tuple[Reply | None, Reply | None]:
        """单次推拉。返回 (ACK, DONE)。"""
        self.send(f"PULL {on_ms}")
        ack = self.wait_for({"ACK", "ERR"}, timeout=1.5)
        if ack is None or ack.kind == "ERR":
            return ack, None
        done = self.wait_for({"DONE", "ABORT"}, timeout=on_ms / 1000.0 + 2.0)
        return ack, done

    def train(self, n: int, on_ms: int, off_ms: int) -> tuple[Reply | None, list[Reply]]:
        """连发。返回 (ACK, 全部 DONE PULSE 列表)。

        收到第 n 个 ``DONE PULSE`` 之后还要再等 ``DONE TRAIN``：最后的间隙
        （off）阶段板子仍处 TRAIN 态，此时发下一条命令会被判 busy。
        这是真机上一定会踩的坑，所以这里必须等干净了才返回。
        """
        self.send(f"TRAIN {n} {on_ms} {off_ms}")
        ack = self.wait_for({"ACK", "ERR"}, timeout=1.5)
        if ack is None or ack.kind == "ERR":
            return ack, []

        dones: list[Reply] = []
        deadline = time.perf_counter() + n * (on_ms + off_ms) / 1000.0 + 3.0
        while time.perf_counter() < deadline:
            r = self.read_line(timeout=1.0)
            if r is None:
                continue
            if r.kind == "DONE":
                if "idx" in r.fields:          # DONE PULSE ...
                    dones.append(r)
                elif "TRAIN" in r.fields or r.raw.endswith("DONE TRAIN"):
                    return ack, dones
            elif r.kind in {"ABORT", "ERR"}:
                print(f"  !! 提前终止：{r.raw}")
                return ack, dones
        return ack, dones

    def retract(self) -> Reply | None:
        self.send("RETRACT")
        return self.wait_for({"RETRACT"}, timeout=1.5)


# --------------------------------------------------------------------------
# 演示动作
# --------------------------------------------------------------------------


def cmd_info(sol: Solenoid) -> None:
    fw = sol.handshake()
    print(f"固件版本 : {fw}")
    st = sol.state()
    if st:
        for k, v in st.fields.items():
            print(f"  {k:<12}= {v}")
    else:
        print("  取状态失败")
    print()
    print("极性说明：polarity=ACTIVE_HIGH 表示继电器高电平吸合。")
    print("若实测是反的（平时吸合、发命令才松开），运行：")
    print(f"  python {sys.argv[0]} --port {sol.port} --polarity low")


def cmd_pull(sol: Solenoid, on_ms: int, settle_ms: int = 0) -> bool:
    if settle_ms:
        time.sleep(settle_ms / 1000.0)
    t_send = sol.send(f"PULL {on_ms}")
    t_ack = None
    deadline = time.perf_counter() + 2.0
    ack = None
    while time.perf_counter() < deadline:
        r = sol.read_line(timeout=0.5)
        if r is None:
            continue
        if r.kind == "ACK":
            ack = r
            t_ack = r.t_recv
            break
        if r.kind == "ERR":
            print(f"板子拒绝：{r.raw}")
            return False
    if ack is None:
        print("没收到 ACK")
        return False

    done = sol.wait_for({"DONE", "ABORT"}, timeout=on_ms / 1000.0 + 2.0)
    if done is None or done.kind != "DONE":
        print(f"没收到 DONE（{done.raw if done else '超时'}）")
        return False

    # 延迟分解：命令从 PC 出门到板子开始驱动，主要花在 USB 转串口上
    rtt_s = (t_ack - t_send) * 1000.0
    print(
        f"PULL {on_ms} ms | 命令→ACK {rtt_s:6.1f} ms | "
        f"板载实测脉宽 {done.get_int('dur_us') / 1000.0:7.2f} ms "
        f"(请求 {on_ms} ms，误差 {done.get_int('dur_us') / 1000.0 - on_ms:+.2f} ms)"
    )
    return True


def cmd_latency(sol: Solenoid, n: int, on_ms: int) -> None:
    """量"发送命令 → 板子 ACK"的延迟，这基本就是软件零点的不确定度。"""
    print(f"测 {n} 次，脉宽 {on_ms} ms（每次间隔 1.5 s，避免线圈过热）\n")
    rtts: list[float] = []
    durs: list[float] = []
    for i in range(n):
        t_send = sol.send(f"PULL {on_ms}")
        ack = sol.wait_for({"ACK", "ERR"}, timeout=2.0)
        if ack is None or ack.kind == "ERR":
            print(f"  第 {i + 1} 次失败：{ack.raw if ack else '超时'}")
            continue
        rtt = (ack.t_recv - t_send) * 1000.0
        done = sol.wait_for({"DONE", "ABORT"}, timeout=on_ms / 1000.0 + 2.0)
        if done:
            dur = done.get_int("dur_us") / 1000.0
            durs.append(dur)
            print(f"  第 {i + 1:2d} 次  RTT {rtt:6.2f} ms   实测脉宽 {dur:7.2f} ms")
        else:
            print(f"  第 {i + 1:2d} 次  RTT {rtt:6.2f} ms   没收到 DONE")
        rtts.append(rtt)
        time.sleep(1.5)

    print()
    if rtts:
        print(f"命令→ACK 延迟：均值 {statistics.mean(rtts):.2f} ms  "
              f"标准差 {statistics.pstdev(rtts):.2f} ms  "
              f"范围 {min(rtts):.2f} ~ {max(rtts):.2f} ms")
        print("  → 这就是『软件发命令』与『电磁铁实际动作』之间的大致偏差。")
        print("    若要更准，用 --fire-at 或固件的 ARM 命令，让板子定时开火。")
    if durs:
        print(f"脉宽误差：均值 {statistics.mean(durs) - on_ms:+.2f} ms  "
              f"标准差 {statistics.pstdev(durs):.2f} ms  "
              f"({statistics.pstdev(durs) / on_ms * 100:.2f}% 相对抖动)")


def cmd_train(sol: Solenoid, n: int, on_ms: int, off_ms: int) -> None:
    period_ms = on_ms + off_ms
    print(f"连发 {n} 次，脉宽 {on_ms} ms，间隔 {off_ms} ms（周期 {period_ms} ms）\n")
    ack, dones = sol.train(n, on_ms, off_ms)
    if ack is None or ack.kind == "ERR":
        print(f"板子拒绝：{ack.raw if ack else '超时'}")
        return
    print(f"收到 {len(dones)}/{n} 个 DONE")
    if not dones:
        return

    t_on = [d.get_int("on_us") / 1000.0 for d in dones]
    t_off = [d.get_int("off_us") / 1000.0 for d in dones]
    print(f"脉宽     ：均值 {statistics.mean(t_on):7.2f} ms  标准差 {statistics.pstdev(t_on):.2f} ms")
    if len(t_on) > 1:
        starts = [d.get_int("on_us") for d in dones]
        periods = [(starts[i + 1] - starts[i]) / 1000.0 for i in range(len(starts) - 1)]
        print(f"实测周期 ：均值 {statistics.mean(periods):7.2f} ms  标准差 {statistics.pstdev(periods):.2f} ms")
        print(f"           （目标 {period_ms} ms，偏差 {statistics.mean(periods) - period_ms:+.2f} ms）")
        print("  → 周期抖动来自固件里 micros() 轮询 + 串口打印，量级在 1 ms 内。")
        print("    对 LFA 够用，因为峰位是靠温度曲线认的，不靠这个周期反推。")
    print(f"绝对时间 ：首个 t_on={t_on[0]:.2f} ms  末个 t_off={t_off[-1]:.2f} ms（板载时钟）")


def cmd_fire_at(sol: Solenoid, delay_s: float, on_ms: int) -> None:
    """在 PC 墙钟的某个整点时刻开火，验证零点对齐用法。

    这里演示的是"上位机掐表"式，精度受 USB 延迟影响（约 1~10 ms）。
    正式做实验时建议用固件的 ARM 命令：把延迟交给板子，误差能压到 1 ms 内。
    """
    t_now = time.perf_counter()
    t_fire = t_now + delay_s
    print(f"将在 {delay_s:.3f} s 后开火（脉宽 {on_ms} ms）")
    while True:
        remain = t_fire - time.perf_counter()
        if remain <= 0:
            break
        if remain > 0.002:
            time.sleep(min(remain - 0.001, 0.01))
        else:
            time.sleep(0)
    t_send = sol.send(f"PULL {on_ms}")
    ack = sol.wait_for({"ACK"}, timeout=2.0)
    if ack:
        print(f"  计划开火时刻 t={t_fire:.6f}")
        print(f"  实际发出时刻 t={t_send:.6f}（偏差 {(t_send - t_fire) * 1000:+.3f} ms）")
        print(f"  收到 ACK     t={ack.t_recv:.6f}")
    sol.wait_for({"DONE"}, timeout=on_ms / 1000.0 + 2.0)


def cmd_watch(sol: Solenoid, seconds: float) -> None:
    """只监听，不主动发命令。用来确认板子是否在正常工作/是否乱发数据。"""
    print(f"监听 {seconds:g} s（Ctrl+C 结束）…… 现在可以手动按板子复位键")
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        r = sol.read_line(timeout=0.5)
        if r:
            print(f"  [{time.perf_counter() - t0:7.3f}s] {r.raw}")


def cmd_settle(sol: Solenoid) -> None:
    r = sol.retract()
    print(r.raw if r else "没收到回复（挡片可能本来就是断开的）")


def cmd_polarity(sol: Solenoid, low: bool) -> None:
    sol.send(f"CFG POLARITY {'ACTIVE_LOW' if low else 'ACTIVE_HIGH'}")
    r = sol.wait_for({"OK", "ERR"}, timeout=1.5)
    print(r.raw if r else "没收到回复")


def cmd_menu(sol: Solenoid) -> None:
    print("交互模式。动作前请确认挡片周围无人、光路上没有不该挡的东西。\n")
    while True:
        print("  [i] 查状态   [p] 推拉一次   [t] 连发   [l] 测延迟")
        print("  [r] 复位断电 [w] 监听     [q] 退出")
        try:
            choice = input("选择 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "q":
            return
        if choice == "i":
            cmd_info(sol)
        elif choice == "p":
            try:
                ms = int(input("  脉宽 ms（默认 100）> ") or 100)
            except ValueError:
                ms = 100
            cmd_pull(sol, ms)
        elif choice == "t":
            try:
                n = int(input("  次数（默认 3）> ") or 3)
                on = int(input("  脉宽 ms（默认 100）> ") or 100)
                off = int(input("  间隔 ms（默认 900）> ") or 900)
            except ValueError:
                n, on, off = 3, 100, 900
            cmd_train(sol, n, on, off)
        elif choice == "l":
            try:
                n = int(input("  次数（默认 5）> ") or 5)
            except ValueError:
                n = 5
            cmd_latency(sol, n, 100)
        elif choice == "r":
            cmd_settle(sol)
        elif choice == "w":
            cmd_watch(sol, 5.0)
        print()


def list_ports() -> None:
    from serial.tools import list_ports as lp

    print("当前串口：")
    found = False
    for p in lp.comports():
        found = True
        print(f"  {p.device:<8} {p.description}")
        if p.vid == 0x1A86:
            print("           ^ 这是 CH340，很可能就是 UNO")
    if not found:
        print("  一个都没有——板子没插好 / 驱动没装")


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="推拉电磁铁 demo（UNO + 继电器）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--port", default=DEFAULT_PORT, help=f"串口，默认 {DEFAULT_PORT}")
    ap.add_argument("--list", action="store_true", help="列出串口后退出")
    ap.add_argument("--info", action="store_true", help="握手并打印板子状态")
    ap.add_argument("--pull", type=int, metavar="MS", help="单次推拉，指定通电毫秒")
    ap.add_argument("--train", nargs=3, type=int, metavar=("N", "ON_MS", "OFF_MS"), help="连发")
    ap.add_argument("--latency", nargs="?", const=5, type=int, metavar="N", help="测命令延迟（默认 5 次）")
    ap.add_argument("--fire-at", type=float, metavar="DELAY_S", help="延迟若干秒后开火")
    ap.add_argument("--watch", nargs="?", const=10.0, type=float, metavar="S", help="只监听（默认 10 s）")
    ap.add_argument("--settle", action="store_true", help="发 RETRACT 复位断电")
    ap.add_argument("--polarity", choices=["high", "low"], help="改继电器有效电平并写入板子")
    ap.add_argument("--gap", type=float, default=1.5, help="--latency 每次之间的间隔秒，默认 1.5")
    ap.add_argument("-v", "--verbose", action="store_true", help="回显所有串口收发")
    args = ap.parse_args()

    if args.list:
        list_ports()
        return 0

    any_action = any([
        args.info, args.pull is not None, args.train, args.latency is not None,
        args.fire_at is not None, args.watch is not None, args.settle, args.polarity,
    ])

    try:
        with Solenoid(args.port, verbose=args.verbose) as sol:
            if any_action:
                fw = sol.handshake()
                print(f"已连接 {args.port}，固件 {fw}\n")

            if args.polarity:
                cmd_polarity(sol, args.polarity == "low")
            if args.settle:
                cmd_settle(sol)
            if args.info:
                cmd_info(sol)
            if args.pull is not None:
                ok = cmd_pull(sol, args.pull)
                if not ok:
                    return 1
            if args.train:
                cmd_train(sol, *args.train)
            if args.latency is not None:
                cmd_latency(sol, args.latency, 100)
            if args.fire_at is not None:
                cmd_fire_at(sol, args.fire_at, 100)
            if args.watch is not None:
                cmd_watch(sol, args.watch)
            if not any_action:
                cmd_menu(sol)
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
