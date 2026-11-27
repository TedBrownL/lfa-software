"""固件模拟器：用 Python 复刻 solenoid_firmware.ino 的串口协议。

为什么需要它
    本机 COM9 目前打不开（USB 层 error 31），在物理重插之前没法做真实通信。
    但脚本的协议解析、时序测算、异常分支都可以先在这台模拟器上跑通——
    模拟器把 `.ino` 的状态机与串口协议逐条移植过来，
    两边行为不一致就是 bug，能提前发现。

它实现 pyserial 的最小接口（write / readline / flush / reset_input_buffer /
is_open / close），所以直接把 ``serial.Serial`` 替换掉即可：

    import serial, firmware_sim
    serial.Serial = firmware_sim.FakeSerial

时间尺度
    真实的脉宽是靠 ``micros()`` 计时的；这里用墙钟模拟。
    为了测试跑得快，可以把脉宽按 ``time_scale`` 缩放，
    但**上报的 dur_us 仍是原始毫秒数**，这样上层逻辑不必区分真假。
"""

from __future__ import annotations

import threading
import time

FW_MAJOR, FW_MINOR = 1, 0


class FakeSerial:
    """假装自己是 /dev/ttyACM 或 COMx。"""

    # 类属性便于测试里断言"有没有真的开过口"
    opened = 0

    def __init__(self, port, baudrate=115200, timeout=0.2, **_kw):
        FakeSerial.opened += 1
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True

        self._lock = threading.Lock()
        self._out = b""
        self._inbox: list[bytes] = []

        # 固件状态
        self.relay_on = False
        self.active_low = False
        self.default_ms = 100
        self.max_ms = 3000
        self.MAX_ON_HARD_MS = 10000
        self.count = 0
        self.mode = "IDLE"
        self.t_drive = 0.0
        self.on_us = 0
        self.train_left = 0
        self.train_on = 0
        self.train_off = 0
        self.arm_fire = 0.0

        # 用独立线程跑状态机，避免上层忘记 service()
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._service_loop, daemon=True)
        self._th.start()

        # 上电后固件会主动打 READY
        self._emit(
            f"\nREADY SOLENOID v{FW_MAJOR}.{FW_MINOR} max_ms={self.max_ms} "
            f"polarity=ACTIVE_HIGH t_us={self._us():.0f}"
        )

    # -- pyserial 接口 ----------------------------------------------------

    def reset_input_buffer(self):
        with self._lock:
            self._inbox.clear()

    def flush(self):
        pass

    def close(self):
        self.is_open = False
        self._stop.set()

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise OSError("port closed")
        text = data.decode("ascii", "replace")
        for line in text.split("\n"):
            line = line.strip()
            if line:
                self._handle(line)
        return len(data)

    def readline(self) -> bytes:
        deadline = time.perf_counter() + self.timeout
        while time.perf_counter() < deadline:
            with self._lock:
                if self._inbox:
                    return self._inbox.pop(0)
            time.sleep(0.001)
        return b""

    # -- 内部 -------------------------------------------------------------

    def _us(self) -> float:
        return time.perf_counter() * 1e6

    def _emit(self, line: str) -> None:
        with self._lock:
            self._inbox.append((line.rstrip("\r\n") + "\r\n").encode("ascii", "replace"))

    def _drive(self, on: bool) -> None:
        self.relay_on = on

    def _start_drive(self, dur_us: int) -> None:
        self.t_drive = time.perf_counter()
        self.on_us = dur_us
        self._drive(True)
        self.count += 1

    def _finish_pulse(self) -> None:
        now = self._us()
        self._drive(False)
        self._emit(
            f"DONE PULSE on_us={self.t_drive * 1e6:.0f} off_us={now:.0f} "
            f"dur_us={now - self.t_drive * 1e6:.0f} idx={self.count}"
        )

    def _service_loop(self) -> None:
        while not self._stop.is_set():
            now = time.perf_counter()
            if self.mode == "PULSE":
                if now - self.t_drive >= self.on_us / 1e6:
                    self._finish_pulse()
                    self.mode = "IDLE"
            elif self.mode == "ARMED":
                if now >= self.arm_fire:
                    self._start_drive(self.on_us)
                    self._emit(f"FIRE t_us={self.t_drive * 1e6:.0f} on_us={self.on_us}")
                    self.mode = "PULSE"
            elif self.mode == "TRAIN":
                if self.relay_on:
                    if now - self.t_drive >= self.train_on / 1e6:
                        self._finish_pulse()
                        self._drive(False)
                        self.t_phase = time.perf_counter()
                else:
                    if now - getattr(self, "t_phase", now) >= self.train_off / 1e6:
                        self.train_left -= 1
                        if self.train_left <= 0:
                            self._emit("DONE TRAIN")
                            self.mode = "IDLE"
                        else:
                            self._start_drive(self.train_on)
            # 热保护
            if self.relay_on and (now - self.t_drive) * 1000 >= self.max_ms:
                self._drive(False)
                self._emit(f"ABORT TIMEOUT t_off_us={self._us():.0f}")
                self.mode = "IDLE"
                self.train_left = 0
            time.sleep(0.0002)

    def _parse_on(self, arg: str | None) -> int | None:
        if not arg:
            ms = self.default_ms
        else:
            try:
                ms = int(arg)
            except ValueError:
                return None
        if ms <= 0:
            ms = 1
        if ms > self.max_ms:
            self._emit(f"ERR too_long 请求 {ms} ms 超过上限 {self.max_ms} ms（可用 CFG MAXON 调整）")
            return None
        return ms

    def _state(self) -> str:
        return (
            f"STATE relay={'ON' if self.relay_on else 'OFF'} mode={self.mode} "
            f"polarity={'ACTIVE_LOW' if self.active_low else 'ACTIVE_HIGH'} "
            f"default_ms={self.default_ms} max_ms={self.max_ms} "
            f"count={self.count} t_us={self._us():.0f}"
        )

    def _handle(self, line: str) -> None:
        parts = line.split()
        cmd = parts[0].upper()
        args = parts[1:]

        if cmd == "PING":
            self._emit(f"PONG t_us={self._us():.0f}")
        elif cmd == "ID":
            self._emit(
                f"SOLENOID v{FW_MAJOR}.{FW_MINOR} pin=7 max_ms={self.max_ms} "
                f"polarity={'ACTIVE_LOW' if self.active_low else 'ACTIVE_HIGH'}"
            )
        elif cmd == "STATE":
            self._emit(self._state())
        elif cmd in ("PULL", "PULSE"):
            ms = self._parse_on(args[0] if args else None)
            if ms is None:
                return
            if self.mode != "IDLE":
                self._emit("ERR busy")
                return
            self._emit(f"ACK PULL on_us={ms * 1000} t_us={self._us():.0f}")
            self._start_drive(ms * 1000)
            self.mode = "PULSE"
        elif cmd == "HOLD":
            if self.mode != "IDLE":
                self._emit("ERR busy")
                return
            self._emit(f"ACK HOLD t_us={self._us():.0f}")
            self._start_drive(self.max_ms * 1000)
            self.mode = "PULSE"
        elif cmd in ("RETRACT", "STOP"):
            if self.relay_on:
                now = self._us()
                self._drive(False)
                self._emit(
                    f"RETRACT t_off_us={now:.0f} held_us={now - self.t_drive * 1e6:.0f}"
                )
            else:
                self._emit("RETRACT 已经断电")
            self.mode = "IDLE"
            self.train_left = 0
        elif cmd == "TRAIN":
            if self.mode != "IDLE":
                self._emit("ERR busy")
                return
            if len(args) < 3:
                self._emit("ERR usage: TRAIN <n> <on_ms> <off_ms>")
                return
            try:
                n, on_ms, off_ms = int(args[0]), int(args[1]), int(args[2])
            except ValueError:
                self._emit("ERR bad args")
                return
            if not 1 <= n <= 255:
                self._emit("ERR n 需在 1..255")
                return
            if on_ms <= 0:
                on_ms = 1
            if on_ms > self.max_ms:
                self._emit("ERR too_long")
                return
            self.train_left = n
            self.train_on = on_ms * 1000
            self.train_off = off_ms * 1000
            self._emit(
                f"ACK TRAIN n={n} on_ms={on_ms} off_ms={off_ms} t_us={self._us():.0f}"
            )
            self._start_drive(self.train_on)
            self.t_phase = time.perf_counter()
            self.mode = "TRAIN"
        elif cmd == "ARM":
            if self.mode != "IDLE":
                self._emit("ERR busy")
                return
            delay_ms = int(args[0]) if args else 0
            on_ms = self._parse_on(args[1] if len(args) > 1 else None)
            if on_ms is None:
                return
            self.arm_fire = time.perf_counter() + delay_ms / 1000.0
            self.on_us = on_ms * 1000
            self._emit(
                f"ARMED delay_ms={delay_ms} on_ms={on_ms} t_us={self._us():.0f}"
            )
            self.mode = "ARMED"
        elif cmd == "CFG":
            sub = args[0].upper() if args else ""
            val = args[1] if len(args) > 1 else ""
            if sub == "POLARITY":
                if val.upper() in ("ACTIVE_LOW", "LOW"):
                    self.active_low = True
                elif val.upper() in ("ACTIVE_HIGH", "HIGH"):
                    self.active_low = False
                else:
                    self._emit("ERR usage: CFG POLARITY ACTIVE_LOW|ACTIVE_HIGH")
                    return
                self._emit(f"OK polarity={'ACTIVE_LOW' if self.active_low else 'ACTIVE_HIGH'}")
            elif sub == "MAXON":
                try:
                    v = int(val)
                except ValueError:
                    self._emit(f"ERR maxon 需在 10..{self.MAX_ON_HARD_MS}")
                    return
                if not 10 <= v <= self.MAX_ON_HARD_MS:
                    self._emit(f"ERR maxon 需在 10..{self.MAX_ON_HARD_MS}")
                    return
                self.max_ms = v
                self._emit(f"OK max_ms={self.max_ms}")
            elif sub == "DEFAULT":
                try:
                    v = int(val)
                except ValueError:
                    self._emit("ERR default 超出 1..max_ms")
                    return
                if not 1 <= v <= self.max_ms:
                    self._emit("ERR default 超出 1..max_ms")
                    return
                self.default_ms = v
                self._emit(f"OK default_ms={self.default_ms}")
            else:
                self._emit("ERR usage: CFG POLARITY|MAXON|DEFAULT ...")
        else:
            self._emit(f"ERR unknown: {cmd}")
