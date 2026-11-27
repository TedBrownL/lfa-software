"""Measure achievable sample rate of the two acquisition schemes."""
import time
import statistics
import serial


def poll_bench(port, baud, duration=2.0):
    ser = serial.Serial(port, baud, timeout=0.05)
    ser.reset_input_buffer()
    stamps, temps = [], []
    t_end = time.perf_counter() + duration
    ser.write(b"\x01")
    while time.perf_counter() < t_end:
        d = ser.read(2)
        if len(d) == 2:
            stamps.append(time.perf_counter())
            temps.append((d[0] * 256 + d[1] - 1000) / 10.0)
            ser.write(b"\x01")
    ser.close()
    return stamps, temps


def cks(b):
    c = 0
    for x in b:
        c ^= x
    return bytes(b) + bytes([c])


def burst_bench(port, baud, interval_ms=1, duration=2.0):
    ser = serial.Serial(port, baud, timeout=0.05)
    ser.reset_input_buffer()
    pattern = [0x02, 0x00] * 8          # Target Act only, rest = end
    ser.write(cks([0x51] + pattern))
    time.sleep(0.05)
    ser.write(cks([0x52, 0x01, (interval_ms >> 8) & 0xFF, interval_ms & 0xFF]))
    stamps, temps = [], []
    t_end = time.perf_counter() + duration
    buf = b""
    while time.perf_counter() < t_end:
        chunk = ser.read(64)
        if not chunk:
            continue
        now = time.perf_counter()
        buf += chunk
        # frames are pairs of 2-byte big-endian temps, length unknown -> report raw
        while len(buf) >= 2:
            v = (buf[0] << 8) | buf[1]
            buf = buf[2:]
            if 0 <= v <= 65535:
                stamps.append(now)
                temps.append((v - 1000) / 10.0)
    try:
        ser.write(cks([0x52, 0, 0, 0]))
    finally:
        ser.close()
    return stamps, temps


def stats(name, stamps, temps):
    if len(stamps) < 3:
        print(f"{name}: too few samples ({len(stamps)})")
        return
    d = [b - a for a, b in zip(stamps, stamps[1:])]
    print(f"{name}: n={len(stamps)} rate={len(stamps)/(stamps[-1]-stamps[0]):.1f} Hz "
          f"dt mean={statistics.mean(d)*1e3:.3f} ms sd={statistics.pstdev(d)*1e3:.3f} ms "
          f"min={min(d)*1e3:.3f} max={max(d)*1e3:.3f} ms")
    print(f"   T range: {min(temps):.1f} .. {max(temps):.1f} C   first={temps[:6]}")


if __name__ == "__main__":
    s, t = poll_bench("COM10", 115200)
    stats("poll@115200", s, t)
    s, t = poll_bench("COM10", 921600)
    stats("poll@921600", s, t)
    s, t = burst_bench("COM10", 921600, 1)
    stats("burst1ms@921600", s, t)
