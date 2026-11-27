/*
 * 推拉电磁铁（激光挡片）控制固件
 * ---------------------------------------------------------------------------
 * 硬件：Arduino UNO R3 改进板 --(数字口 D7)-- 继电器模块 -- 推拉电磁铁
 * 作用：把"激光能不能打到样品"变成电脑可编程的开关，
 *       从而用机械遮挡造出 LFA 需要的激光脉冲（1064 nm 激光器本身是连续的）。
 *
 * 关键约定
 *   1) 单作用电磁铁：通电推出（挡片移开、光通过），断电由弹簧回位（挡光）。
 *      若你的模块是低电平吸合，用 `CFG POLARITY ACTIVE_LOW` 切换，
 *      或用 `--polarity low` 传参给上位机，不必重烧固件。
 *   2) 通电时长由**板子自己**计时（micros），不是上位机发 STOP 来关。
 *      串口有毫秒级抖动，靠上位机掐表会让脉宽不准。
 *   3) 每个动作都回报板载时间戳（micros），上位机据此对齐自己的时钟。
 *      这是"时间零点"能对上的前提，也是本固件存在的核心理由。
 *   4) 通电有时长上限（MAX_ON_MS），超限直接拒绝。推拉电磁铁是短时工作制，
 *      长时间通电会烧线圈；挡片本来也只需要 0.1 s 量级。
 *
 * 串口：115200 8N1，命令一行一条（\n 结尾），回复也是一行一条。
 * 完整命令表见 solenoid/README.md。
 */

// ------------------------------- 硬件配置 ---------------------------------

const uint8_t PIN_RELAY = 7;    // 继电器 IN
const uint8_t PIN_LED   = 13;   // 板载 LED，跟随电磁铁状态，方便肉眼确认

const uint8_t FW_MAJOR = 1;
const uint8_t FW_MINOR = 0;

// 继电器有效电平。true = 低电平吸合（多数蓝色继电器模块是这种）。
// 烧写后也可用 CFG POLARITY 改，无需重新编译。
bool relayActiveLow = false;

// 不指定时长时用的默认值（ms）
unsigned long defaultOnMs = 100;

// 安全上限（ms）。超过这个值的请求一律拒绝，不做静默截断——
// 静默改脉宽会让实验数据在不知情的情况下失真。
unsigned long maxOnMs = 3000;
const unsigned long MAX_ON_HARD_MS = 10000;   // 即使 CFG 也不能超过它

// ------------------------------- 运行时状态 -------------------------------

enum Mode : uint8_t {
  M_IDLE,     // 空闲，电磁铁断电
  M_PULSE,    // 单次通电中
  M_TRAIN,    // 连发中
  M_ARMED     // 已装填，等待预定时刻开火
};

Mode mode = M_IDLE;

unsigned long tDriveUs = 0;    // 本次通电开始的 micros()
unsigned long onUs     = 0;    // 本次应通电的时长（us）
unsigned long tPhaseUs = 0;    // TRAIN 当前阶段起点
bool          relayOn  = false;

// TRAIN 参数
int           trainLeft = 0;
unsigned long trainOnUs = 0;
unsigned long trainOffUs = 0;
int           trainIndex = 0;

// ARM 参数
unsigned long armFireUs = 0;

unsigned long pulseCount = 0;  // 累计通电次数，便于核对"发了几个脉冲"

char lineBuf[64];
uint8_t lineLen = 0;

// ------------------------------- 基础动作 ---------------------------------

void drive(bool on) {
  if (relayActiveLow) {
    digitalWrite(PIN_RELAY, on ? LOW : HIGH);
  } else {
    digitalWrite(PIN_RELAY, on ? HIGH : LOW);
  }
  digitalWrite(PIN_LED, on ? HIGH : LOW);
  relayOn = on;
}

// 立即断电，并终止所有正在进行的动作
void abortAll(const __FlashStringHelper *reason) {
  bool wasOn = relayOn;
  drive(false);
  if (mode != M_IDLE) {
    Serial.print(F("ABORT "));
    Serial.print(reason);
    if (wasOn) {
      Serial.print(F(" t_off_us="));
      Serial.print(micros());
    }
    Serial.println();
  }
  mode = M_IDLE;
  trainLeft = 0;
}

void startDrive(unsigned long durUs) {
  tDriveUs = micros();
  onUs = durUs;
  drive(true);
  pulseCount++;
}

void finishPulse() {
  unsigned long tOff = micros();
  drive(false);
  Serial.print(F("DONE PULSE on_us="));
  Serial.print(tDriveUs);
  Serial.print(F(" off_us="));
  Serial.print(tOff);
  Serial.print(F(" dur_us="));
  Serial.print(tOff - tDriveUs);
  Serial.print(F(" idx="));
  Serial.print(pulseCount);
  Serial.println();
}

// ------------------------------- 状态机 -----------------------------------

void service() {
  unsigned long now = micros();

  switch (mode) {
    case M_PULSE:
      if (now - tDriveUs >= onUs) {
        finishPulse();
        mode = M_IDLE;
      }
      break;

    case M_ARMED:
      // 溢出安全的时间比较：(long)(now - armFireUs) >= 0 等价于 now >= armFireUs，
      // 且 micros() 约 71 分钟回绕一次也不会误判
      if ((long)(now - armFireUs) >= 0) {
        startDrive(onUs);
        Serial.print(F("FIRE t_us="));
        Serial.print(tDriveUs);
        Serial.print(F(" on_us="));
        Serial.println(onUs);
        mode = M_PULSE;
      }
      break;

    case M_TRAIN:
      if (relayOn) {
        if (now - tDriveUs >= trainOnUs) {
          finishPulse();
          drive(false);
          tPhaseUs = micros();
        }
      } else {
        if (now - tPhaseUs >= trainOffUs) {
          trainIndex++;
          if (trainIndex >= trainLeft) {
            Serial.println(F("DONE TRAIN"));
            mode = M_IDLE;
          } else {
            startDrive(trainOnUs);
          }
        }
      }
      break;

    default:
      break;
  }

  // 硬性热保护：任何情况下通电超过 maxOnMs 就断电
  if (relayOn && (now - tDriveUs) >= maxOnMs * 1000UL) {
    abortAll(F("TIMEOUT"));
  }
}

// ------------------------------- 串口解析 ---------------------------------

void replyState() {
  Serial.print(F("STATE relay="));
  Serial.print(relayOn ? F("ON") : F("OFF"));
  Serial.print(F(" mode="));
  switch (mode) {
    case M_IDLE:  Serial.print(F("IDLE"));  break;
    case M_PULSE: Serial.print(F("PULSE")); break;
    case M_TRAIN: Serial.print(F("TRAIN")); break;
    case M_ARMED: Serial.print(F("ARMED")); break;
  }
  Serial.print(F(" polarity="));
  Serial.print(relayActiveLow ? F("ACTIVE_LOW") : F("ACTIVE_HIGH"));
  Serial.print(F(" default_ms="));
  Serial.print(defaultOnMs);
  Serial.print(F(" max_ms="));
  Serial.print(maxOnMs);
  Serial.print(F(" count="));
  Serial.print(pulseCount);
  Serial.print(F(" t_us="));
  Serial.println(micros());
}

void replyId() {
  Serial.print(F("SOLENOID v"));
  Serial.print(FW_MAJOR);
  Serial.print('.');
  Serial.print(FW_MINOR);
  Serial.print(F(" pin="));
  Serial.print(PIN_RELAY);
  Serial.print(F(" max_ms="));
  Serial.print(maxOnMs);
  Serial.print(F(" polarity="));
  Serial.println(relayActiveLow ? F("ACTIVE_LOW") : F("ACTIVE_HIGH"));
}

// 把 "ms" 换算成 us，并做安全校验。返回 false 表示拒绝。
bool parseOnMs(const char *s, unsigned long *outUs) {
  unsigned long ms;
  if (s == NULL || *s == '\0') {
    ms = defaultOnMs;
  } else {
    char *end = NULL;
    ms = strtoul(s, &end, 10);
    if (end == s) return false;
  }
  if (ms == 0) ms = 1;
  if (ms > maxOnMs) {
    Serial.print(F("ERR too_long 请求 "));
    Serial.print(ms);
    Serial.print(F(" ms 超过上限 "));
    Serial.print(maxOnMs);
    Serial.println(F(" ms（可用 CFG MAXON 调整）"));
    return false;
  }
  *outUs = ms * 1000UL;
  return true;
}

void handleLine(char *line) {
  // 去掉行尾空白与 \r
  char *p = line;
  while (*p == ' ' || *p == '\t') p++;
  char *endp = p + strlen(p);
  while (endp > p && (endp[-1] == '\r' || endp[-1] == ' ' || endp[-1] == '\t')) *--endp = '\0';

  if (*p == '\0') return;

  // 转大写便于比较（只影响命令字，参数里的路径不存在）
  char cmd[16];
  uint8_t i = 0;
  while (*p && *p != ' ' && i < sizeof(cmd) - 1) {
    cmd[i++] = (char)toupper((unsigned char)*p++);
  }
  cmd[i] = '\0';
  while (*p == ' ') p++;
  char *arg1 = p;
  char *arg2 = NULL;
  char *arg3 = NULL;
  if (*p) {
    char *sp = strchr(p, ' ');
    if (sp) {
      *sp = '\0';
      arg2 = sp + 1;
      while (*arg2 == ' ') arg2++;
      sp = strchr(arg2, ' ');
      if (sp) {
        *sp = '\0';
        arg3 = sp + 1;
        while (*arg3 == ' ') arg3++;
      }
    }
  }

  if (strcmp(cmd, "PING") == 0) {
    Serial.print(F("PONG t_us="));
    Serial.println(micros());

  } else if (strcmp(cmd, "ID") == 0) {
    replyId();

  } else if (strcmp(cmd, "STATE") == 0) {
    replyState();

  } else if (strcmp(cmd, "PULL") == 0 || strcmp(cmd, "PULSE") == 0) {
    unsigned long durUs;
    if (!parseOnMs(arg1, &durUs)) return;
    if (mode != M_IDLE) { Serial.println(F("ERR busy")); return; }
    Serial.print(F("ACK PULL on_us="));
    Serial.print(durUs);
    Serial.print(F(" t_us="));
    Serial.println(micros());
    startDrive(durUs);
    mode = M_PULSE;

  } else if (strcmp(cmd, "HOLD") == 0) {
    // 保持推出，直到 RETRACT/STOP 或到达热保护上限
    if (mode != M_IDLE) { Serial.println(F("ERR busy")); return; }
    Serial.print(F("ACK HOLD t_us="));
    Serial.println(micros());
    startDrive(maxOnMs * 1000UL);
    mode = M_PULSE;

  } else if (strcmp(cmd, "RETRACT") == 0 || strcmp(cmd, "STOP") == 0) {
    if (relayOn) {
      unsigned long tOff = micros();
      drive(false);
      Serial.print(F("RETRACT t_off_us="));
      Serial.print(tOff);
      Serial.print(F(" held_us="));
      Serial.println(tOff - tDriveUs);
    } else {
      Serial.println(F("RETRACT 已经断电"));
    }
    mode = M_IDLE;
    trainLeft = 0;

  } else if (strcmp(cmd, "TRAIN") == 0) {
    // TRAIN <次数> <通电ms> <间隔ms>
    if (mode != M_IDLE) { Serial.println(F("ERR busy")); return; }
    if (!arg1 || !arg2 || !arg3) { Serial.println(F("ERR usage: TRAIN <n> <on_ms> <off_ms>")); return; }
    long n = strtol(arg1, NULL, 10);
    if (n < 1 || n > 255) { Serial.println(F("ERR n 需在 1..255")); return; }
    unsigned long onMsVal = strtoul(arg2, NULL, 10);
    unsigned long offMsVal = strtoul(arg3, NULL, 10);
    if (onMsVal == 0) onMsVal = 1;
    if (onMsVal > maxOnMs) { Serial.println(F("ERR too_long")); return; }
    trainLeft = (int)n;
    trainOnUs = onMsVal * 1000UL;
    trainOffUs = offMsVal * 1000UL;
    trainIndex = 0;
    Serial.print(F("ACK TRAIN n="));
    Serial.print(n);
    Serial.print(F(" on_ms="));
    Serial.print(onMsVal);
    Serial.print(F(" off_ms="));
    Serial.print(offMsVal);
    Serial.print(F(" t_us="));
    Serial.println(micros());
    startDrive(trainOnUs);
    mode = M_TRAIN;

  } else if (strcmp(cmd, "ARM") == 0) {
    // ARM <延迟ms> <通电ms>
    // 收到后先回 ARMED，等延迟到点再真正通电并回 FIRE。
    // 上位机用"发送时刻 + 已知延迟"就能算出零点，不必赌串口延迟。
    if (mode != M_IDLE) { Serial.println(F("ERR busy")); return; }
    unsigned long delayMs = arg1 ? strtoul(arg1, NULL, 10) : 0;
    unsigned long durUs;
    if (!parseOnMs(arg2, &durUs)) return;
    armFireUs = micros() + delayMs * 1000UL;
    // 复用 M_ARMED 之前，把这次要用的脉宽先存好
    onUs = durUs;
    Serial.print(F("ARMED delay_ms="));
    Serial.print(delayMs);
    Serial.print(F(" on_ms="));
    Serial.print(durUs / 1000UL);
    Serial.print(F(" t_us="));
    Serial.println(micros());
    mode = M_ARMED;

  } else if (strcmp(cmd, "CFG") == 0) {
    char sub[16]; uint8_t k = 0;
    while (arg1 && *arg1 && *arg1 != ' ' && k < sizeof(sub) - 1) sub[k++] = (char)toupper((unsigned char)*arg1++);
    sub[k] = '\0';
    while (arg1 && *arg1 == ' ') arg1++;

    if (strcmp(sub, "POLARITY") == 0) {
      if (arg1 && (strcmp(arg1, "ACTIVE_LOW") == 0 || strcmp(arg1, "LOW") == 0)) {
        relayActiveLow = true;
      } else if (arg1 && (strcmp(arg1, "ACTIVE_HIGH") == 0 || strcmp(arg1, "HIGH") == 0)) {
        relayActiveLow = false;
      } else {
        Serial.println(F("ERR usage: CFG POLARITY ACTIVE_LOW|ACTIVE_HIGH"));
        return;
      }
      drive(relayOn);   // 立刻按新极性重写引脚，保持当前通断状态
      Serial.print(F("OK polarity="));
      Serial.println(relayActiveLow ? F("ACTIVE_LOW") : F("ACTIVE_HIGH"));

    } else if (strcmp(sub, "MAXON") == 0) {
      unsigned long v = arg1 ? strtoul(arg1, NULL, 10) : 0;
      if (v < 10 || v > MAX_ON_HARD_MS) {
        Serial.print(F("ERR maxon 需在 10.."));
        Serial.println(MAX_ON_HARD_MS);
        return;
      }
      maxOnMs = v;
      Serial.print(F("OK max_ms="));
      Serial.println(maxOnMs);

    } else if (strcmp(sub, "DEFAULT") == 0) {
      unsigned long v = arg1 ? strtoul(arg1, NULL, 10) : 0;
      if (v < 1 || v > maxOnMs) { Serial.println(F("ERR default 超出 1..max_ms")); return; }
      defaultOnMs = v;
      Serial.print(F("OK default_ms="));
      Serial.println(defaultOnMs);

    } else {
      Serial.println(F("ERR usage: CFG POLARITY|MAXON|DEFAULT ..."));
    }

  } else {
    Serial.print(F("ERR unknown: "));
    Serial.println(cmd);
  }
}

// ------------------------------- 入口 -------------------------------------

void setup() {
  // 第一件事就把继电器钉死到"断电"电平，避免上电瞬间挡片乱动
  digitalWrite(PIN_RELAY, relayActiveLow ? HIGH : LOW);
  pinMode(PIN_RELAY, OUTPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  Serial.begin(115200);
  delay(120);   // CH340 打开串口后芯片会复位，等一下让上位机连上

  Serial.println();
  Serial.print(F("READY SOLENOID v"));
  Serial.print(FW_MAJOR);
  Serial.print('.');
  Serial.print(FW_MINOR);
  Serial.print(F(" max_ms="));
  Serial.print(maxOnMs);
  Serial.print(F(" polarity="));
  Serial.print(relayActiveLow ? F("ACTIVE_LOW") : F("ACTIVE_HIGH"));
  Serial.print(F(" t_us="));
  Serial.println(micros());
}

void loop() {
  // 收串口
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      lineLen = 0;
      handleLine(lineBuf);
    } else if (c != '\r') {
      if (lineLen < sizeof(lineBuf) - 1) lineBuf[lineLen++] = c;
      else lineLen = 0;     // 超长行直接丢弃，防止缓冲被撑爆
    }
  }
  service();
}
