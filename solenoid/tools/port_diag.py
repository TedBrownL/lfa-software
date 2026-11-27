"""诊断 COM 口：精确定位 pyserial / 原生 API 在哪一步报 error 31。

用途：烧写 UNO 时报 "cannot set com-state for \\\\.\\COM9"，需要判断
是驱动、线缆、还是端口被别的进程占着不放。
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
        ("fBinary", wt.DWORD, 1),
        ("fParity", wt.DWORD, 1),
        ("fOutxCtsFlow", wt.DWORD, 1),
        ("fOutxDsrFlow", wt.DWORD, 1),
        ("fDtrControl", wt.DWORD, 2),
        ("fDsrSensitivity", wt.DWORD, 1),
        ("fTXContinueOnXoff", wt.DWORD, 1),
        ("fOutX", wt.DWORD, 1),
        ("fInX", wt.DWORD, 1),
        ("fErrorChar", wt.DWORD, 1),
        ("fNull", wt.DWORD, 1),
        ("fRtsControl", wt.DWORD, 2),
        ("fAbortOnError", wt.DWORD, 1),
        ("fDummy2", wt.DWORD, 17),
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


def main() -> int:
    path = f"\\\\.\\{PORT}"
    print(f"[1] CreateFileW({path})")
    h = k32.CreateFileW(
        ctypes.c_wchar_p(path),
        GENERIC_READ | GENERIC_WRITE,
        0,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if h == -1 or h is None:
        print(f"    失败 err={ctypes.get_last_error()}")
        return 1
    print(f"    成功 handle={h}")

    print("[2] GetCommState")
    dcb = DCB()
    dcb.DCBlength = ctypes.sizeof(DCB)
    if not k32.GetCommState(wt.HANDLE(h), ctypes.byref(dcb)):
        err = ctypes.get_last_error()
        print(f"    失败 err={err}  <- 常见元凶")
    else:
        print(f"    成功 baud={dcb.BaudRate} bytesize={dcb.ByteSize} parity={dcb.Parity} stop={dcb.StopBits}")

    print("[3] SetCommState(115200 8N1, 关流控)")
    dcb.BaudRate = 115200
    dcb.ByteSize = 8
    dcb.Parity = 0
    dcb.StopBits = 0
    dcb.fBinary = 1
    dcb.fParity = 0
    dcb.fOutxCtsFlow = 0
    dcb.fOutxDsrFlow = 0
    dcb.fDtrControl = 1
    dcb.fDsrSensitivity = 0
    dcb.fOutX = 0
    dcb.fInX = 0
    dcb.fRtsControl = 1
    dcb.fAbortOnError = 0
    if not k32.SetCommState(wt.HANDLE(h), ctypes.byref(dcb)):
        err = ctypes.get_last_error()
        print(f"    失败 err={err}")
    else:
        print("    成功")

    print("[4] 往口里写一行 PING")
    dcb.fDtrControl = 1
    k32.SetCommState(wt.HANDLE(h), ctypes.byref(dcb))
    buf = b"PING\n"
    written = wt.DWORD(0)
    ok = k32.WriteFile(wt.HANDLE(h), buf, len(buf), ctypes.byref(written), None)
    print(f"    ok={ok} written={written.value} err={ctypes.get_last_error() if not ok else 0}")

    k32.CloseHandle(wt.HANDLE(h))
    print("[5] 已关闭")

    print("\n[6] 改用 pyserial 试")
    try:
        import serial

        s = serial.Serial(PORT, 115200, timeout=1)
        s.write(b"PING\n")
        print("    成功，回复:", s.readline())
        s.close()
    except Exception as exc:
        print(f"    失败 {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
