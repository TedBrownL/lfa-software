"""COM 口底层探测：不依赖 SetCommState，逐项测试 USB 管道是否真的通。

为什么要单独做这个：`port_diag.py` 已经确认 CreateFileW 与 GetCommState
成功、SetCommState 与 WriteFile 都报 error 31。error 31 = ERROR_GEN_FAILURE，
属于"设备在但不干活"。本脚本继续验证：读、清错误、调制解调器状态、
以及改波特率后重试，从而区分"管道死了"和"某个 API 不支持"。
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
        ("DCBlength", wt.DWORD),
        ("BaudRate", wt.DWORD),
        ("flags", wt.DWORD),
        ("wReserved", wt.WORD),
        ("XonLim", wt.WORD),
        ("XoffLim", wt.WORD),
        ("ByteSize", wt.BYTE),
        ("Parity", wt.BYTE),
        ("StopBits", wt.BYTE),
        ("XonChar", ctypes.c_char),
        ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char),
        ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char),
        ("wReserved1", wt.WORD),
    ]


class COMMSTATUS(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("fCtsHold", wt.DWORD, 1),
        ("fDsrHold", wt.DWORD, 1),
        ("fRlsdHold", wt.DWORD, 1),
        ("fXoffHold", wt.DWORD, 1),
        ("fXoffSent", wt.DWORD, 1),
        ("fEof", wt.DWORD, 1),
        ("fTxim", wt.DWORD, 1),
        ("fReserved", wt.DWORD, 25),
        ("cbInQue", wt.DWORD),
        ("cbOutQue", wt.DWORD),
    ]


def err() -> int:
    return ctypes.get_last_error()


def main() -> int:
    path = f"\\\\.\\{PORT}"
    h = k32.CreateFileW(
        ctypes.c_wchar_p(path),
        GENERIC_READ | GENERIC_WRITE,
        0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
    )
    if h == -1 or h is None:
        print(f"CreateFile 失败 err={err()}")
        return 1
    HANDLE = wt.HANDLE(h)

    # 1. 清空收发缓冲
    ok = k32.PurgeComm(HANDLE, 0x0001 | 0x0002 | 0x0004 | 0x0008)
    print(f"[1] PurgeComm          ok={bool(ok)} err={0 if ok else err()}")

    # 2. 读设备错误状态（能读到说明管道至少有一半活着）
    st = COMMSTATUS()
    st.cbSize = ctypes.sizeof(COMMSTATUS)
    ok = k32.ClearCommError(HANDLE, None, ctypes.byref(st))
    print(f"[2] ClearCommError     ok={bool(ok)} err={0 if ok else err()} in={st.cbInQue} out={st.cbOutQue}")

    # 3. 调制解调器线状态
    flags = wt.DWORD(0)
    ok = k32.GetCommModemStatus(HANDLE, ctypes.byref(flags))
    print(f"[3] GetCommModemStatus ok={bool(ok)} err={0 if ok else err()} flags=0x{flags.value:04X}")

    # 4. 直接写，不先 SetCommState
    written = wt.DWORD(0)
    ok = k32.WriteFile(HANDLE, b"PING\n", 5, ctypes.byref(written), None)
    print(f"[4] WriteFile(no cfg)  ok={bool(ok)} written={written.value} err={0 if ok else err()}")

    # 5. 尝试 DTR/RTS
    for name, fn in (("SETDTR", 0x0004), ("CLRDTR", 0x0005), ("SETRTS", 0x0006), ("CLRRTS", 0x0007)):
        ok = k32.EscapeCommFunction(HANDLE, fn)
        print(f"[5] {name:<8}       ok={bool(ok)} err={0 if ok else err()}")

    # 6. 逐个波特率试 SetCommState
    print("[6] 逐波特率尝试 SetCommState")
    for baud in (9600, 57600, 115200, 230400):
        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        got = k32.GetCommState(HANDLE, ctypes.byref(dcb))
        if not got:
            print(f"    {baud:>7}: GetCommState 就失败 err={err()}")
            continue
        dcb.BaudRate = baud
        dcb.ByteSize = 8
        dcb.Parity = 0
        dcb.StopBits = 0
        dcb.flags = 0x0001  # fBinary
        ok = k32.SetCommState(HANDLE, ctypes.byref(dcb))
        print(f"    {baud:>7}: ok={bool(ok)} err={0 if ok else err()}")

    # 7. 读：非阻塞看有没有字节
    rbuf = ctypes.create_string_buffer(256)
    read = wt.DWORD(0)
    ok = k32.ReadFile(HANDLE, rbuf, 256, ctypes.byref(read), None)
    print(f"[7] ReadFile           ok={bool(ok)} read={read.value} err={0 if ok else err()}")

    k32.CloseHandle(HANDLE)
    print("[8] 关闭句柄")
    return 0


if __name__ == "__main__":
    sys.exit(main())
