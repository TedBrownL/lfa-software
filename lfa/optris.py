"""Optris CT 4M / CT4ML 串口协议封装。

协议来源：Optris "CT 4M commands_2024-03-A"（随 CompactPlus Connect 安装包分发，
本仓库 docs/ 内留有提取文本 ct4m_cmd.txt）。要点：

* 读取物体温度：发送单字节 ``0x01``，设备返回 2 字节大端：
      T[degC] = (b1*256 + b2 - 1000) / 10
  读命令不带校验和；多字节 SET 命令必须带校验和（全字节 XOR，不含 RS485 地址前缀）。
* 该协议无任何 CR/LF/ACK 冗余字节，因此可以高速连续轮询。

本模块只做"协议层"，不负责采集线程与时间戳（见 :mod:`lfa.acquisition`）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 常用命令字（十进制见协议文档 1 Basic Functions）
CMD_TEMP_PROCESS = b"\x01"   # 物体温度（Process），最常用
CMD_TEMP_INTERNAL = b"\x02"  # 内部/传感头温度
CMD_TEMP_BOX = b"\x03"       # Box 温度
CMD_TEMP_AVG = b"\x0A"       # 平均后温度


def checksum(payload: bytes) -> bytes:
    """返回 payload + 单字节 XOR 校验和（协议 5.0 节）。"""
    c = 0
    for b in payload:
        c ^= b
    return bytes(payload) + bytes([c])


def decode_temperature(raw: bytes) -> float:
    """把 2 字节原始读数解成摄氏温度。"""
    if len(raw) != 2:
        raise ValueError(f"需要 2 字节，收到 {len(raw)} 字节")
    word = (raw[0] << 8) | raw[1]
    return (word - 1000) / 10.0


@dataclass(frozen=True)
class DeviceInfo:
    """设备识别信息，用于在日志/CSV 中留痕。"""
    port: str
    baudrate: int
    serial_number: str
    firmware: str


class OptrisCT4M:
    """CT 4M / CT4ML 的最小可用客户端。

    只依赖 pyserial；``timeout`` 取小值以保证异常时线程能快速退出。
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.05):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser = None

    # ---------- 连接管理 ----------
    def open(self) -> None:
        import serial  # 延迟导入，便于纯离线测试

        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )
        self._ser.reset_input_buffer()

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # ---------- 底层读写 ----------
    def _request(self, cmd: bytes, nbytes: int) -> bytes:
        """发送读命令并读回 nbytes 字节；超时返回空/短数据。"""
        if self._ser is None:
            raise RuntimeError("串口未打开")
        self._ser.write(cmd)
        return self._ser.read(nbytes)

    def _short_reply_streak(self) -> None:
        """读超时后清空输入缓冲，避免下一次读到上一次的滞后数据。"""
        self._ser.reset_input_buffer()

    # ---------- 温度读取 ----------
    def read_temperature_word(self) -> bytes:
        """单次 0x01 读取，返回原始 2 字节（供高频采集循环直接使用）。"""
        return self._request(CMD_TEMP_PROCESS, 2)

    def read_temperature(self) -> float | None:
        raw = self.read_temperature_word()
        if len(raw) != 2:
            self._short_reply_streak()
            return None
        return decode_temperature(raw)

    # ---------- 信号处理设置 ----------
    # 注意：这里的"平均时间"是**时间常数**（一阶低通），不是滑动窗口。
    # 设备出厂默认 100 ms，对 LFA 温升前沿是致命的（会把上升沿抹平，使
    # 半升温时间偏大）。协议只提供"含平均"的 TProc 通道，CT 系列没有
    # CTratio 那种 TAct 无平均通道，因此唯一办法是把该值调到最小 1 ms。
    def read_averaging_time_ms(self) -> int | None:
        """读平均时间常数（ms）。协议：06 00 FF FF + 校验和。"""
        raw = self._request(checksum([0x06, 0x00, 0xFF, 0xFF]), 2)
        if len(raw) != 2:
            return None
        return (raw[0] << 8) | raw[1]

    def set_averaging_time_ms(self, ms: int) -> bool:
        """设平均时间常数（ms，1..65000）。协议：06 00 + 2 字节值 + 校验和。"""
        if not 1 <= ms <= 65000:
            raise ValueError("平均时间须在 1..65000 ms")
        self._request(checksum([0x06, 0x00, (ms >> 8) & 0xFF, ms & 0xFF]), 2)
        return self.read_averaging_time_ms() == ms

    def read_smart_averaging(self) -> int | None:
        """智能平均开关（边沿自适应）。0=关，1=开。"""
        raw = self._request(checksum([0x06, 0x01, 0xFF, 0xFF]), 2)
        if len(raw) != 2:
            return None
        return (raw[0] << 8) | raw[1]

    def read_hold_mode(self) -> int | None:
        """保持模式：0=关，1=峰值，2=谷值，3/4=高级峰/谷。

        LFA 必须为 0：任何保持都会让升温后的信号"粘"在峰值上，
        降温段完全失真。
        """
        raw = self._request(checksum([0x07, 0x00, 0xFF, 0xFF]), 2)
        if len(raw) != 2:
            return None
        return (raw[0] << 8) | raw[1]

    def set_hold_mode(self, mode: int) -> bool:
        """设置保持模式（0=关）。协议：07 00 + 2 字节值 + 校验和。"""
        self._request(checksum([0x07, 0x00, 0x00, mode & 0xFF]), 2)
        return self.read_hold_mode() == mode

    # ---------- 设备信息 ----------
    def read_serial_number(self) -> str:
        raw = self._request(b"\x0E", 4)
        if len(raw) != 4:
            return ""
        return raw.hex().upper()

    def read_firmware(self) -> str:
        raw = self._request(b"\x0F", 2)
        if len(raw) != 2:
            return ""
        return f"{raw[0]}.{raw[1]:02d}"

    def read_emissivity(self) -> float | None:
        """发射率，协议：读 ``04``（无参数）返回 2 字节，值/1000。"""
        raw = self._request(b"\x04", 2)
        if len(raw) != 2:
            return None
        return ((raw[0] << 8) | raw[1]) / 1000.0

    def identify(self) -> DeviceInfo:
        return DeviceInfo(
            port=self.port,
            baudrate=self.baudrate,
            serial_number=self.read_serial_number(),
            firmware=self.read_firmware(),
        )
