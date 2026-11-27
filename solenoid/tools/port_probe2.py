"""COM 口非阻塞探测：区分「USB 管道彻底死了」和「某个 API 不支持」。

上一版 port_probe.py 挂在阻塞式 ReadFile 上，这里先把读写超时设成
"立即返回"，保证脚本一定能跑完并给出结论。

依次试：PurgeComm / ClearCommError / GetCommModemStatus / WriteFile /
EscapeCommFunction / SetupComm / SetCommTimeouts / SetCommState。
哪一步最先失败，就是故障的分界线。
"""

import ctypes
import ctypes.wintypes as wt
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class DCB(ctypes.Structure):
    _fields_ = [
        ("DCBlength", wt.DWORD), ("BaudRate", wt.DWORD), ("flags", wt.DWORD),
        ("wReserved", wt.WORD), ("XonLim", wt.WORD), ("XoffLim", wt.WORD),
        ("ByteSize", wt.BYTE), ("Parity", wt.BYTE), ("StopBits", wt.BYTE),
        ("XonChar", ctypes.c_char), ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char), ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char), ("wReserved1", wt.WORD),
    ]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [
        ("ReadIntervalTimeout", wt.DWORD),
        ("ReadTotalTimeoutMultiplier", wt.DWORD),
        ("ReadTotalTimeoutConstant", wt.DWORD),
        ("WriteTotalTimeoutMultiplier", wt.DWORD),
        ("WriteTotalTimeoutConstant", wt.DWORD),
    ]


class COMMSTATUS(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("flags", wt.DWORD),
        ("cbInQue", wt.DWORD), ("cbOutQue", wt.DWORD),
    ]


def e() -> int:
    return ctypes.get_last_error()


def step(name: str, ok) -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name:<24} err={0 if ok else e()}")


def main() -> int:
    path = f"\\\\.\\{PORT}"
    h = k32.CreateFileW(
        ctypes.c_wchar_p(path), GENERIC_READ | GENERIC_WRITE, 0, None,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
    )
    if h == -1 or h is None:
        print(f"CreateFile 就失败 err={e()}")
        return 1
    H = wt.HANDLE(h)
    print(f"开句柄成功，开始逐项探测 {PORT}\n")

    step("PurgeComm", bool(k32.PurgeComm(H, 0x000F)))

    st = COMMSTATUS()
    st.cbSize = ctypes.sizeof(COMMSTATUS)
    step("ClearCommError", bool(k32.ClearCommError(H, None, ctypes.byref(st))))
    print(f"       -> inQueue={st.cbInQue} outQueue={st.cbOutQue}")

    flags = wt.DWORD(0)
    step("GetCommModemStatus", bool(k32.GetCommModemStatus(H, ctypes.byref(flags))))
    print(f"       -> flags=0x{flags.value:04X}")

    # 先设非阻塞超时，后面读才不会挂住
    to = COMMTIMEOUTS()
    to.ReadIntervalTimeout = 0xFFFFFFFF
    to.ReadTotalTimeoutMultiplier = 0
    to.ReadTotalTimeoutConstant = 0
    to.WriteTotalTimeoutMultiplier = 0
    to.WriteTotalTimeoutConstant = 100
    step("SetCommTimeouts", bool(k32.SetCommTimeouts(H, ctypes.byref(to))))

    step("SetupComm(4096,4096)", bool(k32.SetupComm(H, 4096, 4096)))

    written = wt.DWORD(0)
    step("WriteFile", bool(k32.WriteFile(H, b"PING\n", 5, ctypes.byref(written), None)))

    buf = ctypes.create_string_buffer(256)
    read = wt.DWORD(0)
    ok = k32.ReadFile(H, buf, 256, ctypes.byref(read), None)
    step("ReadFile(非阻塞)", bool(ok))
    print(f"       -> 读到 {read.value} 字节")

    for name, code in (("SETDTR", 4), ("CLRDTR", 5), ("SETRTS", 6), ("CLRRTS", 7)):
        step(f"EscapeCommFunction {name}", bool(k32.EscapeCommFunction(H, code)))

    # 读当前 DCB，看驱动报的波特率
    dcb = DCB()
    dcb.DCBlength = ctypes.sizeof(DCB)
    if k32.GetCommState(H, ctypes.byref(dcb)):
        print(f"\n  GetCommState: baud={dcb.BaudRate} size={dcb.ByteSize} "
              f"parity={dcb.Parity} stop={dcb.StopBits} flags=0x{dcb.flags:08X}")
        dcb.BaudRate = 115200
        dcb.ByteSize = 8
        dcb.Parity = 0
        dcb.StopBits = 0
        dcb.flags = 0x0001
        step("SetCommState", bool(k32.SetCommState(H, ctypes.byref(dcb))))
    else:
        step("GetCommState", False)

    k32.CloseHandle(H)
    print("\n句柄已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
