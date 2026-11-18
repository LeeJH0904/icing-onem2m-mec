"""M2 check: every V2X warning is justified, and was recorded in oneM2M and seen by the cloud.

Reads every alert the warning AE wrote to EdgeMN/icing/node{K}/alert and checks two things separately.

Warning decision (what this project controls) - for each alert:
  * the node's edge result at that measurement time was Frozen,
  * the vehicle distance it reported (MEC 013) is within the radius,
  * a MEC 030 publication was attempted,
  and the cloud can read the newest alert through the IN-CSE.

MEC 030 delivery (what the Sandbox answered) - counted per alert:
  * 204                 accepted by the Sandbox
  * 500, empty body     the hosted Sandbox's V2X message broker failed after the body passed every check
                        (shown by scripts/mec_probe.py --diag on 2026-11-14) - reported, not hidden
  * anything else       a real refusal (a 4xx with a reason, or a 500 with a reason) -> FAIL

Exit 0 when the warning decision is correct for every alert and no publication was really refused.
--strict additionally requires every publication to be 204 (use it once the Sandbox broker works).
"""
import argparse
import json
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from onem2m import CSE, parse_con  # noqa: E402


def all_cins(mn, path, page=150):
    out, ofst = [], 0
    while True:
        code, data = mn.retrieve(path, fu=1, ty=4, lim=page, ofst=ofst)
        batch = data.get("m2m:uril", []) if code == 200 and isinstance(data, dict) else []
        out += batch
        if len(batch) <= page:
            break
        ofst += page
    res = []
    for p in dict.fromkeys(out):
        c, d = mn.retrieve(p)
        if c == 200:
            res.append(parse_con(d["m2m:cin"]["con"]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mn", default="http://127.0.0.1:3001")
    ap.add_argument("--in", dest="in_", default="http://127.0.0.1:7599")
    ap.add_argument("--nodes", default="1,2,3")
    ap.add_argument("--stats", default="http://127.0.0.1:9810/stats")
    ap.add_argument("--strict", action="store_true", help="also require every MEC 030 publication to be 204")
    a = ap.parse_args()
    mn, dash = CSE(a.mn, "Cicing"), CSE(a.in_, "CDashboard")
    total = bad = 0
    delivery = {"204": 0, "broker500": 0, "refused": 0}
    for k in [int(n) for n in a.nodes.split(",")]:
        states = {r["ts"]: r["state"] for r in all_cins(mn, f"EdgeMN/icing/node{k}/clean")}
        alerts = all_cins(mn, f"EdgeMN/icing/node{k}/alert")
        for al in alerts:
            problems = []
            if states.get(al.get("measured")) != "Frozen":
                problems.append(f"node state at {al.get('measured')} was {states.get(al.get('measured'))}")
            if not (isinstance(al.get("distance_m"), (int, float)) and al["distance_m"] <= al.get("radius_m", 0)):
                problems.append(f"distance {al.get('distance_m')} > radius {al.get('radius_m')}")
            st, detail = al.get("publish_status"), (al.get("publish_detail") or "").strip()
            if st is None:
                problems.append("no MEC 030 publication attempted")
                kind = "-"
            elif st == 204:
                delivery["204"] += 1; kind = "accepted"
            elif st == 500 and not detail:
                delivery["broker500"] += 1; kind = "Sandbox broker 500"
            else:
                delivery["refused"] += 1; kind = f"REFUSED {detail[:80]}"
                problems.append(f"MEC 030 refused the publication: {st} {detail[:120]}")
            total += 1; bad += bool(problems)
            print(f"node{k} {al.get('t')} -> {al.get('ue')} {al.get('distance_m')} m  "
                  f"publish {st} ({kind})  {'OK' if not problems else 'PROBLEM: ' + '; '.join(problems)}")
        cloud = dash.latest(f"~/EdgeMN/EdgeMN/icing/node{k}/alert") if alerts else None
        if alerts:
            print(f"node{k}: {len(alerts)} alert(s); cloud (via IN) sees latest: {bool(cloud)}")
            bad += not cloud
    try:
        print("warning AE stats:", json.dumps(requests.get(a.stats, timeout=5).json()))
    except Exception:
        pass
    decision_ok = total > 0 and bad == 0
    print("M2 warning decision (Frozen + MEC 013 radius + oneM2M alert + cloud via IN):",
          "PASS" if decision_ok else "FAIL", f"({total} warning(s), {bad} problem(s))")
    print(f"MEC 030 delivery: {delivery['204']} accepted (204), {delivery['broker500']} Sandbox broker error "
          f"(500, empty body), {delivery['refused']} refused")
    if delivery["broker500"]:
        print("  -> DENM delivery NOT confirmed: the hosted Sandbox's V2X broker failed on bodies that pass all of "
              "its checks (see scripts/mec_probe.py --diag).")
    ok = decision_ok and (delivery["204"] == total if a.strict else True)
    if a.strict and not ok and decision_ok:
        print("--strict: not every publication was accepted (204)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
