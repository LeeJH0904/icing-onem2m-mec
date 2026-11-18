# Sensor node firmware

`node/Hackathon_NodeCode/Hackathon_NodeCode.ino` — Arduino Pro Mini 3.3 V / 8 MHz, E22-900T22D (SX1262) LoRa, 74HC14 RC oscillator (road conductivity → frequency, no ADC), DHT22 (air), DS18B20 (road surface, 1-Wire on D9 + 4.7 kΩ pull-up).

Packet v2, little-endian, 11 bytes (`<BIhHh`) + 1 RSSI byte appended by the receiving module:
`node_id u8 · freq_x100 u32 · air_temp_x10 i16 · air_hum_x10 u16 · road_temp_x10 i16`.
Sensor-error sentinels: air temp −999, humidity 999, road temp −9990.

Libraries: OneWire, DallasTemperature, DHT sensor library (+ Adafruit Unified Sensor), Low-Power (Rocket Scream).
`gateway_onem2m.py` is the Raspberry Pi 5 gateway (oneM2M IPE) that turns these packets into contentInstances on the edge MN-CSE; see `docs/M3_FIELD_SETUP.md`.
