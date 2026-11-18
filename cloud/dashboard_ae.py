"""Cloud dashboard AE - road authority view, runs next to the Mobius4 IN-CSE.

  * Registers on the IN as AE "dashboard" (originator CDashboard).
  * Subscribes, through the IN, to every edge node's clean container on the MN
    (IN->MN forwarding; the MN sees the originator as /Mobius4/CDashboard and its ACP allows read+subscribe).
  * Each edge result it is notified about is copied into the IN's long-term store
    Mobius/icing/history/node{K}  - so the cloud keeps the history even if the edge is replaced.
  * Serves a small web page (default http://127.0.0.1:8080) with the latest state per node,
    a strip of recent states, and the model deployment status read from the IN.

usage: python cloud/dashboard_ae.py [--in http://127.0.0.1:7599] [--web 0.0.0.0:8080] [--listen 0.0.0.0:8081]
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from onem2m import CSE, parse_con  # noqa: E402

STRIP = 64


class Dashboard:
    def __init__(self, a):
        self.a = a
        self.cse = CSE(a.in_, "CDashboard")
        self.nodes = [int(n) for n in a.nodes.split(",")]
        self.latest = {k: None for k in self.nodes}
        self.strip = {k: deque(maxlen=STRIP) for k in self.nodes}
        self.counts = {k: {"Dry": 0, "Wet": 0, "Frozen": 0} for k in self.nodes}
        self.archived = {k: 0 for k in self.nodes}
        self.events = deque(maxlen=40)
        self.deploy = {}
        self.warnings = deque(maxlen=20)        # V2X warnings from the edge alert containers (M2)
        self.lock = threading.Lock()

    def edge(self, k):
        return f"~{self.a.mn_csi}/{self.a.mn_cb}/icing/node{k}/clean"

    def setup(self):
        code, _ = self.cse.create(self.a.in_cb, "ae", {"rn": "dashboard", "api": "Ndashboard", "rr": True,
                                                       "srv": ["3"], "poa": [self.a.public]})
        print("register AE dashboard on IN ->", code, flush=True)
        for k in self.nodes:
            self.cse.delete(f"{self.edge(k)}/sub-dashboard")
            code, data = self.cse.subscribe(self.edge(k), "sub-dashboard", f"{self.a.public}/notify/{k}", net=(3,))
            print(f"subscribe node{k} clean on the edge (via IN) ->", code, "" if code == 201 else data, flush=True)
            alert = f"~{self.a.mn_csi}/{self.a.mn_cb}/icing/node{k}/alert"
            self.cse.delete(f"{alert}/sub-dashboard")
            code, data = self.cse.subscribe(alert, "sub-dashboard", f"{self.a.public}/alert/{k}", net=(3,))
            print(f"subscribe node{k} alert on the edge (via IN) ->", code, "" if code == 201 else data, flush=True)
            last = self.cse.latest(self.edge(k))
            if isinstance(last, dict):
                self.latest[k] = last

    def on_result(self, k, res):
        if not isinstance(res, dict):
            return
        with self.lock:
            self.latest[k] = res
            self.strip[k].append(res.get("state"))
            if res.get("state") in self.counts[k]:
                self.counts[k][res["state"]] += 1
            prev = self.strip[k][-2] if len(self.strip[k]) > 1 else None
            if prev and prev != res.get("state"):
                self.events.appendleft({"t": time.strftime("%H:%M:%S"), "node": k, "ts": res.get("ts"),
                                        "from": prev, "to": res.get("state"), "conf": res.get("confidence")})
        code, _ = self.cse.create_cin(f"{self.a.in_cb}/icing/history/node{k}", dict(res, archived_by="dashboard"))
        if code == 201:
            with self.lock:
                self.archived[k] += 1

    def on_alert(self, k, alert):
        if isinstance(alert, dict):
            with self.lock:
                self.warnings.appendleft(dict(alert, node=k, t=str(alert.get("t", ""))[11:19]))

    def poll_deployments(self):
        while True:
            try:
                code, data = self.cse.retrieve(f"{self.a.in_cb}/icing/edge-deploy")
                lst = data.get("m2m:mdp", {}) if code == 200 else {}
                deps = {}
                for k in self.nodes:
                    c, d = self.cse.retrieve(f"{self.a.in_cb}/icing/edge-deploy/node{k}-rf")
                    if c == 200:
                        deps[f"node{k}-rf"] = {0: "deployed", 1: "running", 2: "stopped"}.get(d["m2m:dpm"].get("mds"), "?")
                with self.lock:
                    self.deploy = {"running": lst.get("nrm"), "stopped": lst.get("nsm"),
                                   "deployed": lst.get("ndm"), "items": deps}
            except Exception as e:  # keep the page alive if the IN restarts
                print("deployment poll:", e, flush=True)
            time.sleep(3)

    def state(self):
        with self.lock:
            return {"nodes": {k: {"latest": self.latest[k], "strip": list(self.strip[k]), "counts": self.counts[k],
                                  "archived": self.archived[k]} for k in self.nodes},
                    "events": list(self.events), "deploy": self.deploy, "warnings": list(self.warnings),
                    "time": time.strftime("%H:%M:%S")}


def PAGE():
    """Read the page on every request (a file object's .read() would return "" after the first load)."""
    with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def notify_handler(app):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            self.send_response(200); self.send_header("X-M2M-RSC", "2000")
            self.send_header("Content-Length", "0"); self.end_headers()
            sgn = body.get("m2m:sgn", {})
            if sgn.get("vrq") or sgn.get("sud"):
                return
            cin = ((sgn.get("nev") or {}).get("rep") or {}).get("m2m:cin")
            if cin and self.path.startswith("/notify/"):
                app.on_result(int(self.path.split("/")[2]), parse_con(cin.get("con")))
            elif cin and self.path.startswith("/alert/"):
                app.on_alert(int(self.path.split("/")[2]), parse_con(cin.get("con")))

        def log_message(self, *a):
            pass
    return H


def web_handler(app):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, ctype, raw):
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                return self._send(200, "application/json", json.dumps(app.state()).encode())
            if self.path in ("/", "/index.html"):
                return self._send(200, "text/html; charset=utf-8", PAGE().encode())
            self._send(404, "text/plain", b"not found")

        def log_message(self, *a):
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_", default="http://127.0.0.1:7599")
    ap.add_argument("--in-cb", default="Mobius")
    ap.add_argument("--mn-csi", default="/EdgeMN")
    ap.add_argument("--mn-cb", default="EdgeMN")
    ap.add_argument("--nodes", default="1,2,3")
    ap.add_argument("--listen", default="0.0.0.0:8081", help="where the edge MN-CSE delivers notifications")
    ap.add_argument("--public", default="http://127.0.0.1:8081", help="URL the MN-CSE uses to notify this AE")
    ap.add_argument("--web", default="0.0.0.0:8080")
    a = ap.parse_args()

    app = Dashboard(a)
    h, p = a.listen.rsplit(":", 1)
    threading.Thread(target=ThreadingHTTPServer((h, int(p)), notify_handler(app)).serve_forever, daemon=True).start()
    app.setup()
    threading.Thread(target=app.poll_deployments, daemon=True).start()
    h, p = a.web.rsplit(":", 1)
    print(f"dashboard on http://{a.web}/", flush=True)
    ThreadingHTTPServer((h, int(p)), web_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
