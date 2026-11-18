"""Raspberry Pi 5 LoRa gateway - oneM2M edition (M3).

Reads the 12-byte LoRa packet (payload 11B + RSSI 1B) from the serial port and writes each reading as a
contentInstance into the edge MN-CSE (tinyIoT), exactly where the virtual node writes during M1/M2:

    POST {MN}/{CSE_BASE}/icing/node{K}/raw
    X-M2M-Origin: Cicing   X-M2M-RVI: 3   Content-Type: application/json;ty=4
    body {"m2m:cin": {"cnf": "application/json:0", "con": "<json string>"}}

The `con` matches what edge/edge_ai_ae.py + edge/model/features.py consume:
    {ts, node_id, freq, air_temp, air_hum, road_temp, provenance, src, rssi_dbm}
Sensor errors map to null (-> NaN downstream). provenance="field" distinguishes real packets from the
synthetic "synthetic" rows, so every /clean result can be traced to its input.

It is self-contained: only `requests` is required to POST; `pyserial` and `RPi.GPIO` are imported lazily and are
needed only for a real radio run, so --selftest and --calib work on any machine.

Network: the Pi must reach the MN-CSE over HTTP. In the verified setup the pipeline runs on a Linux server and
the Pi reaches it over Tailscale (the server's 100.x address); a shared LAN works the same way.
See docs/M3_FIELD_SETUP.md.

Usage (on the Pi, with the pipeline up on the server):
    python3 gateway_onem2m.py --mn http://<server-ip>:3001            # read radio, POST every reading
    python3 gateway_onem2m.py --mn http://<server-ip>:3001 --once     # stop after the first accepted packet (M3 demo)
    python3 gateway_onem2m.py --calib                                 # print freq per node for f_dry, no POST
    python3 gateway_onem2m.py --raw                                   # dump raw serial bytes (reception debugging)
    python3 gateway_onem2m.py --mn http://<server-ip>:3001 --selftest --node 1 --freq 0 --road-temp -2  # no radio
"""
import argparse
import json
import struct
import sys
import time
import uuid
from datetime import datetime

import requests

# --- packet v2 (must match firmware/node/Hackathon_NodeCode/Hackathon_NodeCode.ino) ---
PACKET_FORMAT = "<BIhHh"          # uint8 node_id, uint32 freq*100, int16 air_temp*10, uint16 air_hum*10, int16 road_temp*10
PAYLOAD_LEN = 11
EXPECTED_LEN = PAYLOAD_LEN + 1    # + RSSI byte
TEMP_ERR_RAW, HUM_ERR_RAW, ROAD_ERR_RAW = -999, 999, -9990
M0, M1 = 22, 27                   # LoRa mode pins (BCM), normal mode = LOW/LOW


def decode(payload):
    """11-byte payload -> con dict (without ts). Sensor errors -> None."""
    node_id, freq_raw, temp_raw, hum_raw, road_raw = struct.unpack(PACKET_FORMAT, payload)
    dht_err = temp_raw == TEMP_ERR_RAW or hum_raw == HUM_ERR_RAW
    return {
        "node_id": int(node_id),
        "freq": freq_raw / 100.0,
        "air_temp": None if dht_err else temp_raw / 10.0,
        "air_hum": None if dht_err else hum_raw / 10.0,
        "road_temp": None if road_raw == ROAD_ERR_RAW else road_raw / 10.0,
    }


def post_raw(mn, cse_base, origin, con, timeout=10):
    """Write one reading as a cin into EdgeMN/icing/node{K}/raw. Returns (status, created-rn-or-text)."""
    url = f"{mn.rstrip('/')}/{cse_base}/icing/node{con['node_id']}/raw"
    headers = {"X-M2M-Origin": origin, "X-M2M-RI": uuid.uuid4().hex[:12], "X-M2M-RVI": "3",
               "Content-Type": "application/json;ty=4", "Accept": "application/json"}
    body = {"m2m:cin": {"cnf": "application/json:0", "con": json.dumps(con, ensure_ascii=False)}}
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return None, f"cannot reach MN at {url}: {e}"
    try:
        rn = r.json()["m2m:cin"]["rn"]
    except Exception:
        rn = (r.text or "")[:160]
    return r.status_code, rn


def make_con(reading, provenance="field"):
    return {"ts": datetime.now().isoformat(timespec="seconds"), **reading,
            "provenance": provenance, "src": "pi5-gateway"}


def open_serial(port, baud):
    """Lazy: only needed for a real radio run."""
    import serial  # pyserial
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup([M0, M1], GPIO.OUT)
        GPIO.output(M0, GPIO.LOW)
        GPIO.output(M1, GPIO.LOW)
    except Exception as e:                      # not on a Pi / no GPIO: keep going, the UART may still work
        print(f"[warn] GPIO setup skipped: {e}", flush=True)
    return serial.Serial(port, baud, timeout=1)


def run_radio(a):
    ser = open_serial(a.serial, a.baud)
    print(f"=== LoRa gateway -> {a.mn}/{a.cse_base}/icing/node*/raw  (Ctrl-C to stop) ===", flush=True)
    print(f"opened {a.serial} @ {a.baud}; waiting for {EXPECTED_LEN}-byte packets "
          f"(use --raw if nothing appears - a packet shorter than {EXPECTED_LEN} B never triggers here)", flush=True)
    sent = 0
    last_beat = time.time()
    try:
        while True:
            waiting = ser.in_waiting
            if time.time() - last_beat >= 5:        # heartbeat so silence is never ambiguous
                print(f"[{time.strftime('%H:%M:%S')}] waiting... ({waiting} B buffered, sent {sent})", flush=True)
                last_beat = time.time()
            if waiting >= EXPECTED_LEN:
                time.sleep(0.05)
                raw = ser.read(ser.in_waiting)
                if len(raw) < EXPECTED_LEN:
                    continue
                packet = raw[-EXPECTED_LEN:]
                payload, rssi_byte = packet[:-1], packet[-1]
                try:
                    reading = decode(payload)
                except struct.error as e:
                    print(f"[parse error] {e}", flush=True)
                    continue
                reading["rssi_dbm"] = -(256 - rssi_byte)
                if a.calib:
                    print(f"node {reading['node_id']}  freq {reading['freq']:.2f} Hz  "
                          f"road {reading['road_temp']}  rssi {reading['rssi_dbm']}", flush=True)
                    continue
                con = make_con(reading, a.provenance)
                st, rn = post_raw(a.mn, a.cse_base, a.origin, con)
                ok = st == 201
                print(f"node {con['node_id']}  freq {con['freq']:.2f}  road {con['road_temp']}  "
                      f"-> raw cin {st} {rn if ok else '('+str(rn)+')'}", flush=True)
                if ok:
                    sent += 1
                    if a.once:
                        print(f"--once: first packet accepted ({rn}); stopping.", flush=True)
                        return 0
            time.sleep(0.1)
    except KeyboardInterrupt:
        print(f"\nstopped (sent={sent})", flush=True)
        return 0
    finally:
        try:
            ser.close()
        except Exception:
            pass


def run_raw(a):
    """Dump raw bytes from the serial port (no parsing, no POST) to localize 'no packets':
    bytes here = the radio/UART work and the problem is parsing/config; nothing here = node, wiring,
    LoRa mode, or wrong serial device."""
    ser = open_serial(a.serial, a.baud)
    print(f"=== raw sniff on {a.serial} @ {a.baud} (Ctrl-C to stop) ===", flush=True)
    seen = 0
    try:
        while True:
            n = ser.in_waiting
            if n:
                data = ser.read(n)
                seen += len(data)
                print(f"[{time.strftime('%H:%M:%S')}] {len(data)}B  hex={data.hex()}  "
                      f"(total {seen})", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        print(f"\nstopped (total {seen} bytes)", flush=True)
        if seen == 0:
            print("no bytes at all -> node not transmitting, wiring, LoRa not in normal mode, "
                  "or wrong serial device (try --serial /dev/serial0; check ls -l /dev/serial*)", flush=True)
        return 0
    finally:
        try:
            ser.close()
        except Exception:
            pass


def run_selftest(a):
    """No radio: build one con from the --node/--freq/... values and POST it. Proves the MN path."""
    reading = {"node_id": a.node, "freq": a.freq, "air_temp": a.air_temp,
               "air_hum": a.air_hum, "road_temp": a.road_temp, "rssi_dbm": -60}
    con = make_con(reading, a.provenance)
    print("selftest con:", json.dumps(con, ensure_ascii=False), flush=True)
    st, rn = post_raw(a.mn, a.cse_base, a.origin, con)
    print(f"-> raw cin {st} {rn}", flush=True)
    return 0 if st == 201 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mn", default="http://127.0.0.1:3001", help="edge MN-CSE base URL")
    ap.add_argument("--cse-base", default="EdgeMN")
    ap.add_argument("--origin", default="Cicing")
    ap.add_argument("--serial", default="/dev/ttyAMA0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--provenance", default="field")
    ap.add_argument("--once", action="store_true", help="stop after the first accepted packet (M3 demo)")
    ap.add_argument("--calib", action="store_true", help="print freq per node for f_dry; do not POST")
    ap.add_argument("--raw", action="store_true", help="dump raw serial bytes (no parse/POST) to debug reception")
    ap.add_argument("--selftest", action="store_true", help="POST one synthetic reading without the radio")
    ap.add_argument("--node", type=int, default=1)
    ap.add_argument("--freq", type=float, default=0.0)
    ap.add_argument("--air-temp", dest="air_temp", type=float, default=1.0)
    ap.add_argument("--air-hum", dest="air_hum", type=float, default=90.0)
    ap.add_argument("--road-temp", dest="road_temp", type=float, default=-2.0)
    a = ap.parse_args()
    if a.selftest:
        return run_selftest(a)
    if a.raw:
        return run_raw(a)
    return run_radio(a)


if __name__ == "__main__":
    sys.exit(main())
