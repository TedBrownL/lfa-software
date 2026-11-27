"""查 CH340 的 USB 电源管理与驱动配置项。

Windows 会把 USB 串口设备"挂起省电"（Enhanced Power Management /
Selective Suspend），唤醒失败后就表现为：句柄能开、GetCommState 能读，
但一往外发数据就 ERROR_GEN_FAILURE(31)。这正是当前 COM9 的症状。
本脚本只读，不改任何设置。
"""

import winreg

BASE = r"SYSTEM\CurrentControlSet\Enum\USB\VID_1A86&PID_7523"
KEYS_OF_INTEREST = (
    "enhanced", "suspend", "selective", "power", "idle", "wake",
)


def dump_values(root: str, label: str) -> None:
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
    except OSError:
        return
    try:
        count = winreg.QueryInfoKey(key)[1]
        shown = 0
        for i in range(count):
            name, value, _kind = winreg.EnumValue(key, i)
            if any(s in name.lower() for s in KEYS_OF_INTEREST):
                print(f"    [{label}] {name} = {value}")
                shown += 1
        if shown == 0:
            print(f"    [{label}] 无电源相关项")
    finally:
        winreg.CloseKey(key)


def main() -> int:
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BASE)
    except OSError as exc:
        print(f"打不开 {BASE}: {exc}")
        return 1

    try:
        n_sub = winreg.QueryInfoKey(base)[0]
        instances = [winreg.EnumKey(base, i) for i in range(n_sub)]
    finally:
        winreg.CloseKey(base)

    for inst in instances:
        print(f"\n实例 {inst}")
        dump_values(f"{BASE}\\{inst}", "设备")
        dump_values(f"{BASE}\\{inst}\\Device Parameters", "参数")


if __name__ == "__main__":
    raise SystemExit(main())
