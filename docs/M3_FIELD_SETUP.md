# M3 — real hardware through the pipeline

**Goal (reduced scope):** a real LoRa packet reaches the oneM2M pipeline — node → Pi 5 gateway → a
`contentInstance` on the edge MN-CSE → edge AI inference → `/clean` → the cloud IN-CSE.

**Verified end to end with real hardware (2026-11-15):** a real LoRa packet from the node reached the pipeline —
node → LoRa → Pi gateway (`gateway_onem2m.py`) → Tailscale → home-server tinyIoT MN → edge AI (Frozen) →
`EdgeMN/icing/node1/clean` → Mobius4 IN → dashboard. (`--selftest` posts a gateway-shaped reading without the
radio to exercise the same path from any machine.)

```
LoRa node ──LoRa──► Pi 5 gateway (gateway_onem2m.py) ──HTTP cin POST over LAN/Tailscale──► tinyIoT MN :3001
    → edge AI AE → EdgeMN/icing/nodeK/clean → Mobius4 IN → dashboard   (reaching here = M3 met)
```

The gateway (`firmware/gateway_onem2m.py`) is self-contained: only `requests` is needed to POST; `pyserial` and `RPi.GPIO` are imported lazily, so
`--selftest` and `--calib` run on any machine.

## A. Node (hardware)

1. Arduino IDE libraries: DHT, Adafruit Unified Sensor, LowPower, **OneWire**, **DallasTemperature**.
   Board: Arduino Pro Mini (ATmega328P) **3.3 V / 8 MHz**.
2. Flash `firmware/node/Hackathon_NodeCode/Hackathon_NodeCode.ino`.
3. DS18B20 wiring: DATA → **D9**, with a **4.7 kΩ pull-up between VCC and D9** (if the waterproof probe is the
   "with resistor" type, reuse its resistor — do not add a second one).
4. Power the node; confirm transmission in step B.

## B. Raspberry Pi 5 (gateway)

5. Enable UART (`/dev/ttyAMA0`); `pip3 install pyserial requests RPi.GPIO`.
6. With the LoRa receiver connected, confirm packets arrive — print-only, no POST:
   ```bash
   python3 gateway_onem2m.py --calib
   ```
   Each node prints `node K  freq … Hz  road …  rssi …`. If this shows your nodes, the radio path is good.

## C. Network — Pi → the pipeline host

tinyIoT binds to `0.0.0.0` (all interfaces — confirmed in `httpd.c`), so on a **native Linux host** the MN is
reachable at the host's IP on port 3001 with no port-proxy. Run the pipeline (`docs/SETUP.md`) on that host and
point the gateway at it. Two reachable options:

7. **Same LAN:** put the Pi and the host on the same router. Use the host's LAN IP
   (`ip -4 addr show | grep inet`); the Pi posts to `http://<host-LAN-IP>:3001`.
8. **Tailscale (verified setup):** the pipeline runs on a home server; both the server and the Pi join the same
   tailnet (`curl -fsSL https://tailscale.com/install.sh | sh` then `sudo tailscale up`), and the gateway uses
   the server's Tailscale IP (`tailscale ip -4`, a `100.x`). Works across any physical network, no port-proxy.
9. **Reachability test from the Pi** (replace the IP):
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://<host-IP>:3001/EdgeMN \
     -H "X-M2M-Origin: Cicing" -H "X-M2M-RI: t" -H "X-M2M-RVI: 3"
   ```
   Any HTTP code (even a oneM2M error before `demo_up` runs) means TCP + HTTP reach the MN. No response = wrong
   IP, host firewall (allow TCP 3001, e.g. `ufw allow 3001/tcp`), or the host not reachable.

> Not recommended: running the MN inside **WSL2 behind a Windows Mobile Hotspot**. The hotspot is an ICS-NAT
> adapter that mirrored-WSL does not expose services on, so the port never listens on `192.168.137.1` (even a
> `netsh portproxy` + IP-Helper restart was needed and still fragile). A native Linux host (or at least the
> host's real LAN adapter, not the hotspot) avoids this entirely.

## D. f_dry (dry-surface baseline frequency)

The model normalises frequency as `freq / f_dry`, so each node needs its dry baseline in
`edge/model/node_config.json` (`f_dry_ref_hz` per node — currently the synthetic defaults).

10. With the node on a **dry** surface, run `python3 gateway_onem2m.py --calib`, read the node's stable
    frequency, and put it in `edge/model/node_config.json`:
    ```json
    "1": { "f_dry_ref_hz": <measured Hz>, "source": "field measurement 2026-11-xx" }
    ```
    For a one-packet demo an approximate value is acceptable; the state still comes out, only `freq_norm`
    shifts.

## E. Run and verify (one real packet)

11. **On the pipeline host:** bring the pipeline up (see `docs/SETUP.md`): Mobius4, then tinyIoT, then
    ```bash
    bash scripts/demo_up.sh
    ```
12. **Pi:** run the gateway against the MN; `--once` stops after the first accepted packet (good for a demo):
    ```bash
    python3 gateway_onem2m.py --mn http://<host-IP>:3001 --once
    ```
    Expect `node K  freq …  road …  -> raw cin 201 <rn>`.
13. **Verify the result** (on the pipeline host):
    ```bash
    curl -s "http://127.0.0.1:3001/EdgeMN/icing/nodeK/clean/la" \
      -H "X-M2M-Origin: Cicing" -H "X-M2M-RI: v" -H "X-M2M-RVI: 3" -H "Accept: application/json"
    ```
    A `m2m:cin` whose `con` has `state` (Dry/Wet/Frozen), the same `ts`, and `provenance":"edge-inference"`
    means the real packet went node → MN → inference → `/clean`. The dashboard (`http://127.0.0.1:8080/`)
    shows it live, and the IN keeps a copy at `Mobius/icing/history/nodeK`.

Real packets carry `provenance":"field"` into `/raw` (synthetic rows carry `"synthetic"`), so field and
simulated inputs are distinguishable all the way through.

## Without the radio (fallback)

`--selftest` posts one reading built from `--node/--freq/--air-temp/--air-hum/--road-temp` without the serial
port, so the MN path can be checked from any machine (this is how the gateway was verified):
```bash
python3 gateway_onem2m.py --mn http://127.0.0.1:3001 --selftest --node 1 --freq 0 --road-temp -2
```
