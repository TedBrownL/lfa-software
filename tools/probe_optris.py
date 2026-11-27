"""Probe Optris CT 4M/CT4ML on a virtual COM port using the binary protocol.

Sends the "READ Temp. - Process" command (0x01) and tries to parse the 2-byte
reply:  T = (b1*256 + b2 - 1000) / 10  [degC]
"""
import sys, time
import serial
import serial.tools.list_ports as lp


def try_port(port, baud):
    try:
        ser = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                            timeout=0.5, write_timeout=0.5)
    except Exception as e:
        return f"open failed: {e}"
    try:
        ser.reset_input_buffer()
        ser.write(b"\x01")
        data = ser.read(2)
        if len(data) == 2:
            raw = data[0] * 256 + data[1]
            return f"OK raw=0x{raw:04X} T={(raw - 1000) / 10.0:.1f} C"
        return f"no/short reply: {data!r} (in_waiting={ser.in_waiting})"
    finally:
        ser.close()


if __name__ == "__main__":
    ports = sys.argv[1:] or [p.device for p in lp.comports()
                             if "BTHENUM" not in (p.hwid or "")]
    for port in ports:
        for baud in (115200, 921600):
            print(f"{port} @{baud}: {try_port(port, baud)}", flush=True)
            time.sleep(0.2)
