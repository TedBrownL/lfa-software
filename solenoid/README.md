# 电磁铁（激光挡片）控制

UNO R3 → 继电器模块 → 推拉电磁铁。电脑通过 USB 串口发命令，
让挡片周期性地让开光路，从而给 CW 激光造出 LFA 需要的脉冲。

```
UNO R3 改进板  --D7-->  继电器 IN
                       继电器 COM/NO  --→  推拉电磁铁线圈（建议独立 5V/12V 电源）
                                电磁铁推杆  --→  遮光挡片
```

## 目录

| 文件 | 作用 |
|---|---|
| `firmware/solenoid_firmware/solenoid_firmware.ino` | 固件。脉宽由板子自己计时，每次动作回报板载时间戳 |
| `solenoid_demo.py` | 独立 demo：握手、单次推拉、连发、测延迟、定时开火 |
| `tools/firmware_sim.py` | 固件模拟器（复刻串口协议，无硬件时验证脚本用） |
| `tools/test_demo_sim.py` | 在模拟器上端到端跑 demo 的全部功能 |
| `tools/port_diag.py` | 串口诊断：定位 `SetCommState`/`WriteFile` 的失败点 |
| `tools/port_probe.py` | 更底层探测：PurgeComm / ClearCommError / 调制解调器状态 |
| `tools/port_probe2.py` | 非阻塞探测，第一版会挂在 ReadFile 上，用这个 |
| `tools/port_stability.py` | 分阶段测端口稳定性，区分「空闲死」和「动作死」 |
| `tools/ch340_power_check.py` | 查 CH340 的 USB 电源管理注册表项（只读） |

## 烧写

本机没装 Arduino IDE，用 D 盘独立安装的 arduino-cli（不占 C 盘）：

```bash
CLI=/d/Experiment/SOFTWARE/arduino-cli/arduino-cli.exe
CFG="D:\\Experiment\\SOFTWARE\\arduino-cli\\data\\arduino-cli.yaml"
BUILD="D:\\Experiment\\ChuangII\\LFA_Software\\solenoid\\firmware\\_build"

cd /d/Experiment/ChuangII/LFA_Software/solenoid/firmware
"$CLI" --config-file "$CFG" compile --fqbn arduino:avr:uno --build-path "$BUILD" solenoid_firmware
"$CLI" --config-file "$CFG" upload -p COM9 --fqbn arduino:avr:uno --build-path "$BUILD" solenoid_firmware
```

编译结果：7846 B / 32256 B（24%），全局变量 400 B / 2048 B（19%）。

## 用法

```bash
PY=D:/MINICONDA/envs/casimir/python.exe

$PY solenoid_demo.py --list                  # 列串口，认 CH340
$PY solenoid_demo.py --info                  # 握手 + 打印板子状态
$PY solenoid_demo.py --pull 100              # 单次推拉 100 ms
$PY solenoid_demo.py --train 5 100 900       # 连发 5 次，通 100 ms 断 900 ms
$PY solenoid_demo.py --latency 10            # 测 10 次"命令→动作"延迟
$PY solenoid_demo.py --settle                # 发 RETRACT 确保挡片复位
$PY solenoid_demo.py --polarity low          # 若继电器是低电平吸合
$PY solenoid_demo.py                         # 不给参数：交互菜单
```

## 串口协议

115200 8N1，一行一条命令（`\n` 结尾），回复也一行一条。

| 命令 | 回复 | 说明 |
|---|---|---|
| `PING` | `PONG t_us=` | 探活 |
| `ID` | `SOLENOID v1.0 pin=7 max_ms=3000 polarity=...` | 认固件 |
| `STATE` | `STATE relay=... mode=... count=... t_us=` | 查状态 |
| `PULL <ms>` | `ACK PULL on_us= t_us=` → `DONE PULSE on_us= off_us= dur_us= idx=` | 单次推拉 |
| `HOLD` | `ACK HOLD t_us=` | 保持推出，直到 `RETRACT` 或热保护 |
| `RETRACT` / `STOP` | `RETRACT t_off_us= held_us=` | 立即断电 |
| `TRAIN <n> <on_ms> <off_ms>` | `ACK TRAIN ...` → n×(DONE PULSE) → `DONE TRAIN` | 连发 |
| `ARM <delay_ms> <on_ms>` | `ARMED delay_ms= t_us=` → `FIRE t_us=` | 定时开火 |
| `CFG POLARITY ACTIVE_LOW\|ACTIVE_HIGH` | `OK polarity=` | 改有效电平，不用重烧 |
| `CFG MAXON <ms>` | `OK max_ms=` | 改通电上限（10..10000） |
| `CFG DEFAULT <ms>` | `OK default_ms=` | 改默认脉宽 |

失败一律回 `ERR <原因>`，不会静默改参数。

## 三个设计要点

**1. 脉宽由板子计时，不由上位机掐表。**
USB 转串口的往返抖动在 1~10 ms，靠"发 PULL 再发 RETRACT"控制 100 ms
的脉宽会引入 10% 级误差。固件收到 `PULL 100` 后自己用 `micros()` 关断。

**2. 每次动作回报板载时间戳。**
`ACK`/`DONE`/`FIRE` 都带 `t_us=`（UNO 的 micros 时钟）。
上位机用它和自己的 `perf_counter` 做线性拟合，就能把"板子什么时候真的推了"
换算到测温数据的时间轴上——这是时间零点对齐的基础。

**3. 通电有上限，且拒绝而非截断。**
推拉电磁铁是短时工作制。请求超过 `max_ms`（默认 3000 ms）直接回
`ERR too_long`。静默截断会让实验数据在不知情的情况下失真。
另外 `service()` 里有一道硬保护：无论处于什么状态，通电超时立刻断电。

## 已知坑

**TRAIN 结束后不能马上发下一条命令。** 最后一个间隙（off）阶段板子仍是
TRAIN 态，此时发 `PULL` 会收到 `ERR busy`。脚本里已经等 `DONE TRAIN`
再返回；集成进 LFA 时也要照这个来。

**继电器极性。** 多数蓝色继电器模块是低电平吸合。若实测"平时吸合、
发命令才松开"，用 `--polarity low` 切，或 `CFG POLARITY ACTIVE_LOW`。

**电磁铁供电。** 不要从 UNO 的 5V 引脚取电驱动电磁铁——线圈冲击电流会让
USB 掉电、CH340 掉线。用独立电源。

**负载侧不要和 UNO 共地。** 继电器触点侧本身就是电气隔离的，这正是它比
MOSFET 方案省事的地方。把 12V 负极接到 UNO 的 GND，电磁铁那几安培电流
就会从这条地线走，直接干扰 UNO 和 USB。

**继电器 IN 必须接 D7。** 固件里 `PIN_RELAY = 7` 是写死的。接在别的数字口
时，`PULL` 仍会回 `DONE`（那只是单片机翻转了 D7），但继电器根本不会动。

## 无硬件时的验证

```bash
$PY tools/test_demo_sim.py     # 24/24 通过
```

模拟器 `firmware_sim.FakeSerial` 复刻了 `.ino` 的状态机与协议，
把这个类赋给 `serial.Serial` 就能让 demo 走完全相同的代码路径。
