"""Round-trip latency of the 0x01 temperature poll (write->2 bytes read)."""
import time, statistics, serial

ser = serial.Serial("COM10", 115200, timeout=0.05)
ser.reset_input_buffer()
lat, stale = [], set()
for _ in range(4000):
    t0 = time.perf_counter_ns()
    ser.write(b"\x01")
    d = ser.read(2)
    t1 = time.perf_counter_ns()
    if len(d) == 2:
        lat.append((t1 - t0) / 1e3)
ser.close()
lat.sort()
n = len(lat)
print(f"n={n}  mean={statistics.mean(lat):.1f} us  sd={statistics.pstdev(lat):.1f} us")
print(f"p1={lat[n//100]:.1f} p25={lat[n//4]:.1f} p50={lat[n//2]:.1f} "
      f"p75={lat[3*n//4]:.1f} p99={lat[99*n//100]:.1f} max={lat[-1]:.1f} us")
