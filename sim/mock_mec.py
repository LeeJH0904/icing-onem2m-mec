"""Local stand-in for the ETSI MEC Sandbox - FOR OFFLINE TESTS ONLY.

It implements only what the hosted Sandbox was observed to do in pre-verification (2026-10-14..17), so the
warning AE can be tested without Sandbox access. It is not a MEC platform and proves nothing about the
real Sandbox; the demo and the submission use the hosted Sandbox.

  MEC 013  GET {prefix}/location/v2|v3/queries/users?address=     one vehicle 10.100.0.1 driving a loop in Monaco
           GET {prefix}/location/v2|v3/queries/distance?address=&latitude=&longitude=
  MEC 030  POST/GET {prefix}/vis/v2/subscriptions, DELETE .../subscriptions/{id}
           POST {prefix}/vis/v2/publish_v2x_message   checks in the Sandbox handler's order: 400 (JSON detail)
                                                      for missing msgPropertiesValues / stdOrganization / msgContent,
                                                      an unknown msgType (only lower-case enum names, e.g. 'denm'),
                                                      or no subscription; then the broker stage, where any error is
                                                      a bare 500 with an EMPTY body (not "hexadump", bad hex, or
                                                      --broker-fail); else 204 + V2xMsgNotification to subscribers
  test     GET /mock/published   (publications received)     GET /mock/vehicle   (current position)

usage: python sim/mock_mec.py [--port 8090] [--speed 25]   ->  bases http://127.0.0.1:8090/sbx/mep1/location/v2
                                                                     http://127.0.0.1:8090/sbx/mep1/vis/v2
"""
import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

# a loop over Monaco roads that passes the three node positions in edge/mec_config.json
ROUTE = [(43.7405, 7.4295), (43.7375, 7.4230), (43.7345, 7.4165), (43.7320, 7.4150),
         (43.7300, 7.4140), (43.7330, 7.4155), (43.7370, 7.4220), (43.7405, 7.4295)]
VEHICLE = "10.100.0.1"
# msgType names accepted in a publication: lower-case names of the MsgType enum
# (go-apps/meep-vis/server/model_msg_type.go); 'denm' confirmed on the hosted Sandbox, 'DENM' and '1' refused
MSG_TYPES = {"denm", "cam", "poi", "spatem", "mapem", "ivim", "ev_rsr", "tistpgtransaction", "srem", "ssem",
             "evcsn", "saem", "rtcmem", "cpm", "imzm", "vam", "dsm", "mim", "mvm", "mcm"}


def haversine(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


class World:
    def __init__(self, speed, broker_fail=False):
        self.speed, self.t0, self.broker_fail = speed, time.time(), broker_fail
        self.legs = [haversine(ROUTE[i], ROUTE[(i + 1) % len(ROUTE)]) for i in range(len(ROUTE))]
        self.subs, self.next_id, self.published = {}, 1, []
        self.lock = threading.Lock()

    def position(self):
        s = (time.time() - self.t0) * self.speed % sum(self.legs)
        for i, leg in enumerate(self.legs):
            if s <= leg:
                f, a, b = s / leg, ROUTE[i], ROUTE[(i + 1) % len(ROUTE)]
                return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
            s -= leg
        return ROUTE[0]


def handler(world):
    class H(BaseHTTPRequestHandler):
        def _json(self, code, obj=None, headers=None):
            raw = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def do_GET(self):
            u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
            ts = {"seconds": int(time.time()), "nanoSeconds": 0}
            if u.path.endswith("/queries/users"):
                if q.get("address") not in (None, VEHICLE):
                    return self._json(200, {"userList": {"resourceURL": self.path}})
                lat, lon = world.position()
                return self._json(200, {"userList": {"resourceURL": self.path, "user": [{
                    "address": VEHICLE, "accessPointId": "4g-macro-cell-3", "zoneId": "zone01", "timestamp": ts,
                    "resourceURL": self.path, "locationInfo": {"latitude": [lat], "longitude": [lon], "shape": 2}}]}})
            if u.path.endswith("/queries/distance"):
                if q.get("address") != VEHICLE:
                    return self._json(404, {"title": "unknown address"})
                d = haversine(world.position(), (float(q["latitude"]), float(q["longitude"])))
                return self._json(200, {"terminalDistance": {"distance": int(round(d)), "timestamp": ts}})
            if u.path.endswith("/vis/v2/subscriptions"):
                with world.lock:
                    return self._json(200, {"subscription": [{"href": f"{u.path}/{i}", "subscriptionType": "V2xMsgSubscription"}
                                                             for i in world.subs]})
            if u.path == "/mock/published":
                with world.lock:
                    return self._json(200, world.published)
            if u.path == "/mock/vehicle":
                return self._json(200, dict(zip(("lat", "lon"), world.position())))
            self._json(404, {"title": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"title": "bad json"})
            if u.path.endswith("/vis/v2/subscriptions"):
                mt = (body.get("filterCriteria") or {}).get("msgType")
                if mt is None:
                    return self._json(400, {"detail": "Mandatory attribute MsgType is missing"})
                if not isinstance(mt, list) or not all(isinstance(x, str) and x.islower() for x in mt):
                    return self._json(500, {"detail": "MsgType parameter should be between 1 and 13"})
                with world.lock:
                    sid = world.next_id; world.next_id += 1; world.subs[sid] = body
                href = f"http://{self.headers.get('Host')}{u.path}/{sid}"
                return self._json(201, dict(body, _links={"self": {"href": href}}), {"Location": href})
            if u.path.endswith("/vis/v2/publish_v2x_message"):
                # same order and messages as the Sandbox handler (observed with mec_probe.py --diag)
                props = body.get("msgPropertiesValues")
                if not isinstance(props, dict):
                    return self._json(400, {"detail": "Mandatory attribute MsgPropertiesValues is missing in the request body.", "status": 400})
                if not props.get("stdOrganization"):
                    return self._json(400, {"detail": "Mandatory attribute StdOrganization is missing in the request body.", "status": 400})
                if props.get("msgType") not in MSG_TYPES:     # 'DENM', '1', unknown names -> 400 on the Sandbox
                    return self._json(400, {"detail": "Mandatory attribute MsgType is missing in the request body.", "status": 400})
                if not body.get("msgContent"):
                    return self._json(400, {"detail": "Mandatory attribute MsgContent is missing in the request body.", "status": 400})
                with world.lock:
                    subs = list(world.subs.values())
                if not subs:
                    return self._json(400, {"detail": "No subscription ongoing, discard it.", "status": 400})
                # broker stage: the Sandbox answers any error here as a bare 500 with an EMPTY body
                geo = ((props.get("locationInfo") or {}).get("geoArea") or {})
                if world.broker_fail or body.get("msgRepresentationFormat") != "hexadump" or not all(
                        isinstance(geo.get(k), (int, float)) for k in ("latitude", "longitude")):
                    return self._json(500)
                try:
                    bytes.fromhex(body.get("msgContent"))
                except ValueError:
                    return self._json(500)
                with world.lock:
                    world.published.append(dict(body, _received=time.time()))
                for s in subs:                              # deliver like a broker would (best effort)
                    note = {"notificationType": "V2xMsgNotification", "timeStamp": {"seconds": int(time.time())},
                            "stdOrganization": props.get("stdOrganization"), "msgType": props.get("msgType"),
                            "msgRepresentationFormat": body.get("msgRepresentationFormat"), "msgContent": body.get("msgContent")}
                    threading.Thread(target=lambda url=s.get("callbackReference"): requests.post(url, json=note, timeout=3)
                                     if url else None, daemon=True).start()
                return self._json(204)
            self._json(404, {"title": "not found"})

        def do_DELETE(self):
            u = urlparse(self.path)
            if "/vis/v2/subscriptions/" in u.path:
                sid = int(u.path.rsplit("/", 1)[-1])
                with world.lock:
                    found = world.subs.pop(sid, None) is not None
                return self._json(204 if found else 404)
            self._json(404, {"title": "not found"})

        def log_message(self, *a):
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--speed", type=float, default=25.0, help="vehicle speed in m/s")
    ap.add_argument("--broker-fail", action="store_true",
                    help="answer valid publications with 500 + empty body, like the hosted Sandbox on 2026-11-14")
    a = ap.parse_args()
    w = World(a.speed, a.broker_fail)
    print(f"mock MEC on :{a.port} (loop {sum(w.legs):.0f} m, {sum(w.legs) / a.speed:.0f} s per lap)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), handler(w)).serve_forever()


if __name__ == "__main__":
    main()
