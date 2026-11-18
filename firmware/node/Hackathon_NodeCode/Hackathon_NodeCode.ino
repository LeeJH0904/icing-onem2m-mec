/* =====================================================================
 *  LoRa 환경 센서 노드 — 저전력 개정판 + DS18B20 노면온도(D9) 추가
 *  플랫폼 : Arduino Pro Mini 3.3V/8MHz + E22-900T22D + DHT22 + 74HC14 + DS18B20
 *
 *  적용된 방법론
 *   [M1] jitter 백오프를 delay() -> WDT 슬립 분산 (능동전류 제거)
 *   [M2] getFrequencyFloat() 정수나눗셈 버그만 수정
 *   [M3] 미사용 Serial 제거 + SoftwareSerial RX 리스닝 중지(PCINT 누설 차단)
 *   [M4] 미사용 GPIO INPUT_PULLUP / 상태LED핀 OUTPUT LOW (플로팅 누설 차단)
 *   [M7] 전원 LED 물리 제거 (하드웨어 — 코드 영향 없음)
 *
 * ===================================================================== */

#include <SoftwareSerial.h>
#include <DHT.h>
#include <LowPower.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// --- 핀 정의 ---
SoftwareSerial loRaSerial(2, 4);   // RX=2(미사용), TX=4
#define DHTPIN      5
#define DHTTYPE     DHT22
DHT dht(DHTPIN, DHTTYPE);

#define FREQ_PIN    3
#define LORA_M0     6
#define LORA_M1     7
#define LORA_AUX    8

// --- DS18B20 노면온도 센서 (1-Wire, 데이터=D9) ---
// HW 필수: VCC-D9 사이 4.7kOhm 풀업 저항 1개. 방수 프로브가 저항 동봉형이면 재사용(중복 금지).
// 주의: 노면온도 필드 추가로 패킷 9B -> 11B. 게이트웨이 struct('<BIhHh') / EXPECTED_LENGTH(=12) 동기 필수.
#define ONE_WIRE_BUS 9
#define DS18B20_RES  11          // 해상도(9~12bit). 11bit=0.125C, 변환 ~375ms
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature roadTemp(&oneWire);

// --- 설정 값 ---
const uint8_t NODE_ID       = 1;
const int     SLEEP_MINUTES = 1;

// --- 패킷 (11바이트, 게이트웨이 struct.unpack('<BIhHh') 와 일치) ---
struct __attribute__((packed)) LoRaPacket {
  uint8_t  node_id;        // 1B
  uint32_t freq_raw;       // 4B  실제주파수     x 100
  int16_t  temp_raw;       // 2B  공기온도(DHT22) x 10
  uint16_t hum_raw;        // 2B  공기습도(DHT22) x 10
  int16_t  road_temp_raw;  // 2B  노면온도(DS18B20) x 10  (에러 시 -9990 = -999.0)
};

// [M4] 미사용 핀 처리
void configureUnusedPins() {
  pinMode(13, OUTPUT); digitalWrite(13, LOW);   // 상태LED핀(존재 시) 소등 + 플로팅 방지
  const uint8_t unused[] = {0, 1, 10, 11, 12, A1, A2, A3, A4, A5, A6, A7};
  for (uint8_t i = 0; i < sizeof(unused); i++) pinMode(unused[i], INPUT_PULLUP);
}

void setup() {
  // [M3] Serial 미사용 → Serial.begin() 제거

  // LoRa 제어 핀 + 시작 시 딥슬립(M0=1,M1=1)
  pinMode(LORA_M0, OUTPUT);
  pinMode(LORA_M1, OUTPUT);
  pinMode(LORA_AUX, INPUT);
  digitalWrite(LORA_M0, HIGH);
  digitalWrite(LORA_M1, HIGH);

  // 주파수 입력 핀 (센서 레일은 VCC 상시 구동)
  pinMode(FREQ_PIN, INPUT);

  // 다중노드 백오프 분산용 시드 (A0 플로팅 노이즈 + NODE_ID)
  randomSeed(analogRead(A0) + NODE_ID);

  configureUnusedPins();             // [M4]
  pinMode(A0, INPUT_PULLUP);         // 시드 사용 후 A0도 풀업

  // [M3] LoRa는 TX 전용 → RX 리스닝 중지(PCINT/RX 잔류 누설·오웨이크 제거)
  loRaSerial.begin(9600);
  loRaSerial.stopListening();        // write()는 영향 없음

  dht.begin();

  // DS18B20 (외부전원, 상시 VCC 레일). 1-Wire 4.7kOhm 풀업은 하드웨어 필수.
  roadTemp.begin();
  roadTemp.setResolution(DS18B20_RES);
}

void loop() {
  // [1][M1] 랜덤 백오프 — delay() 대신 WDT 슬립으로 분산
  //   random(0,20)*250ms = 0~4750ms. 이 동안 LoRa 딥슬립 유지.
  uint8_t jitterChunks = random(0, 20);
  for (uint8_t i = 0; i < jitterChunks; i++) {
    LowPower.powerDown(SLEEP_250MS, ADC_OFF, BOD_OFF);
  }

  // [2] 센서 측정 (센서는 VCC 상시 인가 상태)
  float t    = dht.readTemperature();
  float h    = dht.readHumidity();
  float freq = getFrequencyFloat();
  roadTemp.requestTemperatures();               // DS18B20 변환 요청(블로킹 ~375ms)
  float rt   = roadTemp.getTempCByIndex(0);     // 노면온도(에러 시 DEVICE_DISCONNECTED_C = -127)

  // [3] 패킷 생성 (정수화)
  LoRaPacket packet;
  packet.node_id  = NODE_ID;
  packet.freq_raw = (uint32_t)(freq * 100.0f);
  if (isnan(t) || isnan(h)) {
    packet.temp_raw = -999;   // 게이트웨이: -99.9 → 에러 판정
    packet.hum_raw  = 999;    // 게이트웨이:  99.9 → 에러 판정
  } else {
    packet.temp_raw = (int16_t)(t * 10.0f);
    packet.hum_raw  = (uint16_t)(h * 10.0f);
  }
  // DS18B20 노면온도 (DHT와 독립적으로 에러 판정)
  if (rt == DEVICE_DISCONNECTED_C || rt < -55.0f || rt > 125.0f) {
    packet.road_temp_raw = -9990;               // 게이트웨이: -999.0 -> 에러 판정
  } else {
    packet.road_temp_raw = (int16_t)(rt * 10.0f);
  }

  // [4] LoRa Normal Mode (M0=0, M1=0)
  digitalWrite(LORA_M0, LOW);
  digitalWrite(LORA_M1, LOW);
  delay(10);

  // [5] LoRa 전송 (바이너리 11바이트)
  loRaSerial.write((uint8_t*)&packet, sizeof(packet));

  // [6] 송신 완료 대기 (M5 미선택 → 원본 busy-wait 유지)
  while (digitalRead(LORA_AUX) == LOW) { delay(1); }
  delay(20);

  // [7] LoRa Deep Sleep (M0=1, M1=1, ~2uA)
  digitalWrite(LORA_M0, HIGH);
  digitalWrite(LORA_M1, HIGH);
  delay(100);

  // [8] 아두이노 수면 (8s x 112 = 896s ≈ 14.93분)
  int sleepCycles = (SLEEP_MINUTES * 6) / 8;
  for (int i = 0; i < sleepCycles; i++) {
    LowPower.powerDown(SLEEP_8S, ADC_OFF, BOD_OFF);
  }
}

// 주파수 측정 (float 반환) — [M2] 정수나눗셈 버그만 수정
float getFrequencyFloat() {
  const int sampleCount = 10;
  unsigned long totalPeriod = 0;
  int validSamples = 0;
  for (int i = 0; i < sampleCount; i++) {
    unsigned long high = pulseIn(FREQ_PIN, HIGH, 10000);
    unsigned long low  = pulseIn(FREQ_PIN, LOW, 10000);
    if (high > 0 && low > 0) {
      totalPeriod += (high + low);
      validSamples++;
    }
  }
  if (validSamples > 0) {
    // [M2 FIX] 정수나눗셈 절삭 제거: 평균주기를 float로 계산 후 역수
    return 1000000.0f / ((float)totalPeriod / (float)validSamples);
  }
  return 0.0f;
}
