"""Edge inference AE - runs on the MEC host next to the tinyIoT MN-CSE.

What it does
  1. Registers on the MN-CSE as AE "edge-ai" (originator CEdgeAI).
  2. Subscribes, through the MN, to the cloud's <modelDeploymentList> on the Mobius4 IN-CSE
     (MN->IN forwarding; the IN sees the originator as /EdgeMN/CEdgeAI).
  3. For every <modelDeployment> aimed at this MN:
       - reads the <mlModel> (via the MN), downloads the file from its `mmu` URL and checks the SHA-256
         recorded in the model's labels,
       - subscribes to the deployment itself to follow run/stop (mds),
       - subscribes locally to the deployment's input resource (inr = node raw container).
  4. Each new raw contentInstance -> per-node reorder buffer -> StreamingClassifier
     (features + RandomForest + safety rule + Wet confirmation) -> result cin in the output resource (our).

The CSE only stores and tracks the model; inference happens here, at the edge.
Notifications are not guaranteed to arrive in order (pre-verification: 22/60 rows survived a
burst without reordering), so rows wait REORDER_HOLD seconds in a per-deployment heap and are
processed in measurement-time order; anything older than what was already processed is counted as late.

usage: python edge/edge_ai_ae.py [--mn http://127.0.0.1:3001] [--listen 0.0.0.0:9800] [--public http://127.0.0.1:9800]
"""
import argparse
import hashlib
import heapq
import itertools
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
sys.path.insert(0, os.path.join(HERE, "model"))
from onem2m import CSE, parse_con, created_or_exists  # noqa: E402

REORDER_HOLD = 0.6          # seconds a row waits for earlier rows that may still be in flight
CACHE_DIR = os.path.join(HERE, ".model_cache")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Deployment:
    def __init__(self, ri, rn, moid, inr, our, mds):
        self.ri, self.rn, self.moid, self.inr, self.our, self.mds = ri, rn, moid, inr, our, mds
        self.clf = None
        self.model_tag = None
        self.heap, self.seq = [], itertools.count()
        self.last_ts = None
        self.stats = {"received": 0, "inferred": 0, "written": 0, "late": 0, "skipped_not_running": 0,
                      "errors": 0}

    @property
    def running(self):
        return self.mds == 1


class EdgeAI:
    def __init__(self, a):
        self.a = a
        self.mn = CSE(a.mn, "CEdgeAI")
        self.in_prefix = f"~{a.in_csi}"                 # SP-relative prefix for IN resources, e.g. ~/Mobius4
        self.deploy_list = f"{self.in_prefix}/{a.in_cb}/icing/edge-deploy"
        self.my_csi = a.mn_csi                          # /EdgeMN
        self.deps = {}                                  # dpm ri -> Deployment
        self.models = {}                                # sha256 -> StreamingClassifier
        self.lock = threading.Lock()
        self.model_lock = threading.Lock()              # deployments notified together share one download

    # ---------------------------------------------------------------- setup
    def register(self):
        code, data = self.mn.create(self.a.mn_cb, "ae", {
            "rn": "edge-ai", "api": "Nedgeai", "rr": True, "srv": ["3"], "poa": [self.a.public]})
        log("register AE edge-ai on MN ->", code if created_or_exists(code, data) else f"{code} {data}")

    def subscribe_deployments(self):
        sub = f"{self.deploy_list}/sub-edge-ai"
        self.mn.delete(sub)                             # drop a subscription left by an earlier run
        code, data = self.mn.subscribe(self.deploy_list, "sub-edge-ai", f"{self.a.public}/deploy", net=(3,))
        log("subscribe IN modelDeploymentList (via MN) ->", code, "" if code == 201 else data)
        # deployments created before this AE started.
        # Discovery (fu=1&ty=104) does not survive MN->IN forwarding: tinyIoT drops `ty` and adds `fo=1`,
        # and Mobius4 v4.24.5 refuses `fo` ("fc => fo is not allowed"). So ask for the deployments of the
        # nodes this edge serves by name; new deployments still arrive through the subscription above.
        code, data = self.mn.retrieve(self.deploy_list, fu=1, ty=104)
        if code == 200 and isinstance(data, dict):
            paths = [f"{self.in_prefix}/{p}" for p in data.get("m2m:uril", [])]
        else:
            paths = [f"{self.deploy_list}/node{k}-rf" for k in self.a.nodes.split(",")]
        for path in paths:
            c, d = self.mn.retrieve(path)
            if c == 200 and isinstance(d, dict) and "m2m:dpm" in d:
                threading.Thread(target=self.on_deployment, args=(d["m2m:dpm"],), daemon=True).start()

    # ---------------------------------------------------------------- deployments
    def local_path(self, rid):
        """'/EdgeMN/EdgeMN/icing/node1/raw' -> 'EdgeMN/icing/node1/raw' (only for resources on this MN)."""
        if rid.startswith(self.my_csi + "/"):
            return rid[len(self.my_csi) + 1:]
        if rid.startswith(self.a.mn_cb + "/"):
            return rid
        return None

    def load_model(self, moid):
        with self.model_lock:
            return self._load_model(moid)

    def _load_model(self, moid):
        code, data = self.mn.retrieve(f"{self.in_prefix}/{moid}")
        if code != 200 or "m2m:mmd" not in data:
            raise RuntimeError(f"cannot read mlModel {moid}: {code} {data}")
        mmd = data["m2m:mmd"]
        want = next((l.split(":", 1)[1] for l in mmd.get("lbl", []) if l.startswith("sha256:")), None)
        if want in self.models:
            return self.models[want], f"{mmd['rn']}@{mmd['vr']}"
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, f"{mmd['rn']}-{mmd['vr']}.joblib")
        r = requests.get(mmd["mmu"], timeout=60)
        r.raise_for_status()
        got = hashlib.sha256(r.content).hexdigest()
        if want and got != want:
            raise RuntimeError(f"model integrity check failed: sha256 {got[:16]} != {want[:16]} (mmu={mmd['mmu']})")
        with open(path, "wb") as f:
            f.write(r.content)
        from inference import StreamingClassifier
        clf = StreamingClassifier(model_path=path)
        self.models[got] = clf
        log(f"model {mmd['rn']} v{mmd['vr']} downloaded {len(r.content):,} B from {mmd['mmu']}, sha256 ok={want == got}")
        return clf, f"{mmd['rn']}@{mmd['vr']}"

    def on_deployment(self, dpm):
        ri = dpm.get("ri")
        with self.lock:
            dep = self.deps.get(ri)
            if dep:                                      # known deployment: follow run/stop
                if dpm.get("mds") is not None and dpm["mds"] != dep.mds:
                    log(f"deployment {dep.rn}: mds {dep.mds} -> {dpm['mds']} ({'running' if dpm['mds'] == 1 else 'stopped'})")
                    dep.mds = dpm["mds"]
                return
            inr, our = self.local_path(dpm.get("inr", "")), self.local_path(dpm.get("our", ""))
            if not inr or not our:
                log(f"deployment {dpm.get('rn')}: inr/our not on this MN ({dpm.get('inr')}) - ignored")
                return
            dep = Deployment(ri, dpm.get("rn"), dpm["moid"], inr, our, dpm.get("mds", 0))
            self.deps[ri] = dep
        log(f"new deployment {dep.rn}: model {dep.moid}  {inr} -> {our}  mds={dep.mds}")
        dep.clf, dep.model_tag = self.load_model(dep.moid)
        # follow run/stop on the deployment itself (lives on the IN; structured path so the old sub can be deleted)
        dpm_path = f"{self.deploy_list}/{dep.rn}"
        self.mn.delete(f"{dpm_path}/sub-edge-ai")
        code, _ = self.mn.subscribe(dpm_path, "sub-edge-ai", f"{self.a.public}/deploy", net=(1,))
        log(f"  watch {dep.rn} run/stop ->", code)
        # input resource on this MN
        sub_rn = f"sub-edge-ai-{dep.rn}"
        self.mn.delete(f"{inr}/{sub_rn}")
        code, data = self.mn.subscribe(inr, sub_rn, f"{self.a.public}/infer/{ri}", net=(3,))
        log(f"  subscribe input {inr} ->", code, "" if code == 201 else data)
        # catch up with a state change that happened while we were loading
        c, d = self.mn.retrieve(dpm_path)
        if c == 200 and "m2m:dpm" in d:
            self.on_deployment(d["m2m:dpm"])

    # ---------------------------------------------------------------- inference
    def on_input(self, ri, con):
        dep = self.deps.get(ri)
        if not dep or not isinstance(con, dict) or "ts" not in con:
            return
        with self.lock:
            dep.stats["received"] += 1
            heapq.heappush(dep.heap, (str(con["ts"]), next(dep.seq), time.time(), con))

    def worker(self):
        while True:
            time.sleep(0.05)
            now = time.time()
            for dep in list(self.deps.values()):
                while True:
                    with self.lock:
                        if not dep.heap or now - dep.heap[0][2] < REORDER_HOLD:
                            break
                        ts, _, _, con = heapq.heappop(dep.heap)
                    self.process(dep, ts, con)

    def process(self, dep, ts, con):
        if dep.last_ts is not None and ts <= dep.last_ts:
            dep.stats["late"] += 1                       # arrived after a newer row was already used
            return
        if not dep.running:
            dep.stats["skipped_not_running"] += 1
            return
        try:
            out = dep.clf.update(con)
            dep.last_ts = ts
            dep.stats["inferred"] += 1
            res = {"ts": con["ts"], "node_id": con.get("node_id"), "state": out["state"],
                   "confidence": out["confidence"], "proba": out["proba"], "raw_state": out["raw_state"],
                   "safety_applied": out["safety_applied"], "held_by_wet_confirm": out["held_by_wet_confirm"],
                   "road_temp": con.get("road_temp"), "freq": con.get("freq"),
                   "model": dep.model_tag, "deployment": dep.rn,
                   "provenance": "edge-inference", "input_provenance": con.get("provenance", "unknown")}
            code, data = self.mn.create_cin(dep.our, res)
            if code == 201:
                dep.stats["written"] += 1
            else:
                dep.stats["errors"] += 1
                log(f"write {dep.our} -> {code} {data}")
            if out["state"] == "Frozen" and self.a.verbose:
                log(f"{dep.rn} {con['ts']} FROZEN conf={out['confidence']}")
        except Exception:
            dep.stats["errors"] += 1
            traceback.print_exc()

    def stats(self):
        return {d.rn: dict(d.stats, running=d.running, model=d.model_tag, pending=len(d.heap))
                for d in self.deps.values()}


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def _reply(self, code=200, body=None):
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("X-M2M-RSC", "2000")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path.startswith("/stats"):
                return self._reply(200, app.stats())
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._reply(400)
            sgn = body.get("m2m:sgn", {})
            self._reply(200)                             # answer first; notifications must not block the CSE
            if sgn.get("vrq") or sgn.get("sud"):         # subscription verification / deletion notice
                return
            rep = (sgn.get("nev") or {}).get("rep") or {}
            try:
                if self.path.startswith("/deploy") and "m2m:dpm" in rep:
                    threading.Thread(target=app.on_deployment, args=(rep["m2m:dpm"],), daemon=True).start()
                elif self.path.startswith("/infer/") and "m2m:cin" in rep:
                    app.on_input(self.path.split("/")[2], parse_con(rep["m2m:cin"].get("con")))
            except Exception:
                traceback.print_exc()

        def log_message(self, *a):
            pass
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mn", default="http://127.0.0.1:3001")
    ap.add_argument("--mn-csi", default="/EdgeMN")
    ap.add_argument("--mn-cb", default="EdgeMN")
    ap.add_argument("--in-csi", default="/Mobius4")
    ap.add_argument("--in-cb", default="Mobius")
    ap.add_argument("--listen", default="0.0.0.0:9800")
    ap.add_argument("--public", default="http://127.0.0.1:9800", help="URL the CSEs use to notify this AE")
    ap.add_argument("--nodes", default="1,2,3", help="nodes this edge serves (used to find existing deployments)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    app = EdgeAI(a)
    host, port = a.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=app.worker, daemon=True).start()
    log(f"edge AE listening on {a.listen}, public {a.public}")
    app.register()
    app.subscribe_deployments()
    last = None
    while True:
        time.sleep(10)
        s = app.stats()
        if s != last:
            log("stats", json.dumps(s))
            last = s


if __name__ == "__main__":
    main()
