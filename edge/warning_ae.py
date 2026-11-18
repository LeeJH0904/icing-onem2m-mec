"""Warning AE - runs on the MEC host next to the tinyIoT MN-CSE (milestone M2).

  1. Registers on the MN-CSE as AE "warning" (originator CWarning) and subscribes to every node's
     clean container (the edge inference results).
  2. Keeps each node's latest state. While a node is Frozen it asks MEC 013 how far each vehicle is
     from the node; when a vehicle is within radius_m it publishes one DENM through MEC 030
     (cause: adverse road adhesion / black ice) and records the warning in EdgeMN/icing/node{K}/alert.
     A vehicle is warned once per approach: it is warned again only after it has left the radius
     or the node has stopped being Frozen.
  3. Keeps one MEC 030 V2X subscription open while running - the Sandbox refuses to publish
     without one - and deletes it on exit.

The DENM content is a small JSON summary (cause/sub-cause codes, position, confidence), encoded as
the template's msgEncodeFormat. It is NOT an ASN.1-encoded ETSI EN 302 637-3 DENM.

usage:
  python edge/warning_ae.py --loc-base https://try-mec.etsi.org/<sandbox>/mep1/location/v2 \
                            --vis-base https://try-mec.etsi.org/<sandbox>/mep1/vis/v2
  (or set MEC_LOC_BASE / MEC_VIS_BASE)
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from onem2m import CSE, parse_con, created_or_exists  # noqa: E402
from mec_client import MecClient  # noqa: E402

# ETSI TS 102 894-2 (CDD): causeCode 6 = adverseWeatherCondition-Adhesion, subCauseCode 6 = blackIceOnRoad
CAUSE, SUB_CAUSE = 6, 6


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class WarningAE:
    def __init__(self, a, cfg):
        self.a, self.cfg = a, cfg
        self.mn = CSE(a.mn, "CWarning")
        self.mec = MecClient(a.loc_base, a.vis_base, a.template)
        self.nodes = {int(k): v for k, v in cfg["nodes"].items()}
        self.state = {k: None for k in self.nodes}          # latest edge result per node
        self.warned = {}                                    # (node, ue) -> True while inside radius
        self.lock = threading.Lock()
        self.sub_id = None
        self.stats = {"results": 0, "distance_checks": 0, "denm_published": 0, "denm_failed": 0,
                      "v2x_notifications_received": 0, "mec_errors": 0}

    # ---------------- setup ----------------
    def setup(self):
        code, data = self.mn.create(self.a.mn_cb, "ae", {"rn": "warning", "api": "Nwarning", "rr": True,
                                                         "srv": ["3"], "poa": [self.a.public]})
        log("register AE warning on MN ->", code if created_or_exists(code, data) else f"{code} {data}")
        for k in self.nodes:
            path = f"{self.a.mn_cb}/icing/node{k}/clean"
            self.mn.delete(f"{path}/sub-warning")
            code, data = self.mn.subscribe(path, "sub-warning", f"{self.a.public}/notify/{k}", net=(3,))
            log(f"subscribe node{k}/clean ->", code, "" if code == 201 else data)
            last = self.mn.latest(path)
            if isinstance(last, dict):
                self.state[k] = last
        self.sub_id = self.mec.subscribe_v2x(self.a.v2x_callback)
        log(f"MEC 030 V2X subscription {self.sub_id} (callback {self.a.v2x_callback})")

    def close(self):
        if self.sub_id:
            log("delete MEC 030 subscription", self.sub_id, "->", self.mec.delete_subscription(self.sub_id))

    # ---------------- logic ----------------
    def on_result(self, k, res):
        if not isinstance(res, dict) or k not in self.nodes:
            return
        with self.lock:
            self.stats["results"] += 1
            prev = (self.state.get(k) or {}).get("state")
            self.state[k] = res
            if res.get("state") != "Frozen":
                for key in [key for key in self.warned if key[0] == k]:
                    self.warned.pop(key)                     # a new freeze episode warns again
        if res.get("state") == "Frozen" and prev != "Frozen":
            log(f"node{k} became Frozen (measured {res.get('ts')}, conf {res.get('confidence')})")
            self.check_node(k)

    def check_node(self, k):
        node = self.nodes[k]
        res = self.state.get(k) or {}
        for ue in self.cfg["vehicles"]:
            try:
                d = self.mec.distance(ue, node["lat"], node["lon"])
                self.stats["distance_checks"] += 1
            except Exception as e:
                self.stats["mec_errors"] += 1
                log(f"MEC 013 distance {ue} node{k} failed: {e}")
                continue
            inside = d <= self.cfg["radius_m"]
            with self.lock:
                already = self.warned.get((k, ue), False)
                if not inside:
                    self.warned.pop((k, ue), None)           # left the radius: warn again on next approach
                elif not already:
                    self.warned[(k, ue)] = True
            if inside and not already:
                self.warn(k, ue, d, res)

    def warn(self, k, ue, d, res):
        node = self.nodes[k]
        info = {"type": "icing-warning", "causeCode": CAUSE, "subCauseCode": SUB_CAUSE,
                "node": f"node{k}", "lat": node["lat"], "lon": node["lon"], "radius_m": self.cfg["radius_m"],
                "state": res.get("state"), "confidence": res.get("confidence"), "measured": res.get("ts"),
                "input_provenance": res.get("input_provenance")}
        code, text = self.mec.publish_denm(node["lat"], node["lon"], info)
        ok = code == 204
        detail = (text or "").strip()
        # 500 with an empty body = the hosted Sandbox's V2X message broker failed after the body passed
        # every check (scripts/mec_probe.py --diag shows this); it is not a request-body problem.
        broker_500 = code == 500 and not detail
        self.stats["denm_published" if ok else "denm_failed"] += 1
        if broker_500:
            self.stats["denm_sandbox_broker_500"] = self.stats.get("denm_sandbox_broker_500", 0) + 1
        log(f"DENM node{k} -> {ue} at {d} m: MEC 030 publish {code}" + ("" if ok else f" {detail or '(empty body)'}"))
        result = ("accepted (204)" if ok else
                  "Sandbox V2X broker error (500, empty body)" if broker_500 else f"refused ({code})")
        alert = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "node_id": k, "ue": ue, "distance_m": d,
                 "radius_m": self.cfg["radius_m"], "lat": node["lat"], "lon": node["lon"],
                 "measured": res.get("ts"), "state": res.get("state"), "confidence": res.get("confidence"),
                 "publish_status": code, "publish_detail": detail[:200], "result": result,
                 "cause": CAUSE, "subCause": SUB_CAUSE, "provenance": "mec-warning",
                 "input_provenance": res.get("input_provenance")}
        c, data = self.mn.create_cin(f"{self.a.mn_cb}/icing/node{k}/alert", alert)
        if c != 201:
            log(f"write alert node{k} -> {c} {data}")

    def loop(self):
        """Vehicles move between measurements, so Frozen nodes are re-checked on a timer, not only on new data."""
        while True:
            time.sleep(self.cfg.get("check_interval_s", 2.0))
            for k in [k for k, r in self.state.items() if (r or {}).get("state") == "Frozen"]:
                try:
                    self.check_node(k)
                except Exception:
                    traceback.print_exc()


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/stats"):
                raw = json.dumps(dict(app.stats, frozen=[k for k, r in app.state.items()
                                                         if (r or {}).get("state") == "Frozen"],
                                      warned=[f"node{k}->{u}" for (k, u) in app.warned])).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            else:
                self.send_response(404); self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            self.send_response(200 if self.path.startswith("/notify/") else 204)
            self.send_header("X-M2M-RSC", "2000"); self.send_header("Content-Length", "0"); self.end_headers()
            if self.path.startswith("/v2x"):                 # MEC 030 V2xMsgNotification (needs a reachable URL)
                app.stats["v2x_notifications_received"] += 1
                log("V2X notification received:", json.dumps(body)[:160])
                return
            sgn = body.get("m2m:sgn", {})
            if sgn.get("vrq") or sgn.get("sud"):
                return
            cin = ((sgn.get("nev") or {}).get("rep") or {}).get("m2m:cin")
            if cin and self.path.startswith("/notify/"):
                k = int(self.path.split("/")[2])
                threading.Thread(target=app.on_result, args=(k, parse_con(cin.get("con"))), daemon=True).start()

        def log_message(self, *a):
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mn", default="http://127.0.0.1:3001")
    ap.add_argument("--mn-cb", default="EdgeMN")
    ap.add_argument("--loc-base", default=os.environ.get("MEC_LOC_BASE"), help="MEC 013 base URL")
    ap.add_argument("--vis-base", default=os.environ.get("MEC_VIS_BASE"), help="MEC 030 base URL")
    ap.add_argument("--config", default=os.path.join(HERE, "mec_config.json"))
    ap.add_argument("--template", default=os.path.join(HERE, "denm_template.json"))
    ap.add_argument("--listen", default="0.0.0.0:9810")
    ap.add_argument("--public", default="http://127.0.0.1:9810", help="URL the MN-CSE uses to notify this AE")
    ap.add_argument("--v2x-callback", default="http://127.0.0.1:9810/v2x",
                    help="callbackReference of the MEC 030 subscription (must be reachable by the MEC platform "
                         "to actually receive DENMs; publishing works either way)")
    a = ap.parse_args()
    if not a.loc_base or not a.vis_base:
        ap.error("set --loc-base/--vis-base (or MEC_LOC_BASE/MEC_VIS_BASE)")
    cfg = json.load(open(a.config, encoding="utf-8"))

    app = WarningAE(a, cfg)
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # demo_down.sh -> still delete the V2X subscription
    h, p = a.listen.rsplit(":", 1)
    threading.Thread(target=ThreadingHTTPServer((h, int(p)), make_handler(app)).serve_forever, daemon=True).start()
    log(f"warning AE listening on {a.listen}; MEC 013 {a.loc_base}; MEC 030 {a.vis_base}; radius {cfg['radius_m']} m")
    app.setup()
    threading.Thread(target=app.loop, daemon=True).start()
    try:
        last = None
        while True:
            time.sleep(10)
            if app.stats != last:
                log("stats", json.dumps(app.stats)); last = dict(app.stats)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()


if __name__ == "__main__":
    main()
