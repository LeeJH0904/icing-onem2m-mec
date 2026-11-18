"""Create the oneM2M resource trees on both CSEs (run once, after both CSEs are up).

Edge  - tinyIoT MN-CSE  (CSE-ID /EdgeMN, CSEBase EdgeMN)
  EdgeMN/icing                 AE    originator Cicing (field side; the Pi 5 gateway IPE / virtual node)
  EdgeMN/icing/acp-icing       ACP   who may do what on the node data
  EdgeMN/icing/node{K}/raw     cnt   measurements from the gateway IPE
  EdgeMN/icing/node{K}/clean   cnt   edge inference results
  EdgeMN/icing/node{K}/health  cnt   node / pipeline health
  EdgeMN/icing/node{K}/alert   cnt   V2X warnings published by the MEC warning AE (M2)

Cloud - Mobius4 IN-CSE  (CSE-ID /Mobius4, CSEBase Mobius)
  Mobius/icing                 AE    originator Cicing (service operator)
  Mobius/icing/acp-icing       ACP
  Mobius/icing/models          modelRepo            (TR-0071, Rel-5 candidate)
  Mobius/icing/edge-deploy     modelDeploymentList  (TR-0071, Rel-5 candidate)
  Mobius/icing/history/node{K} cnt   long-term copy of the edge results (written by the dashboard AE)

ACPs are attached at creation time: Mobius4 refuses a later acpi UPDATE on TR-0071 resources (5000).
A request that crosses CSEs arrives with the originator rewritten to SP-relative form, so the edge
AE is "/EdgeMN/CEdgeAI" on the IN and the dashboard is "/Mobius4/CDashboard" on the MN.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from onem2m import CSE, created_or_exists  # noqa: E402

# acop bits: 1 Create, 2 Retrieve, 4 Update, 8 Delete, 16 Notify, 32 Discovery
ALL, READ_SUB = 63, 1 | 2 | 16 | 32


def step(tag, result):
    code, data = result
    good = created_or_exists(code, data) or code == 200
    print(f"  {'ok ' if good else 'ERR'} {code} {tag}" + ("" if good else f"  {json.dumps(data)[:200]}"))
    if not good:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mn", default="http://127.0.0.1:3001")
    ap.add_argument("--in", dest="in_", default="http://127.0.0.1:7599")
    ap.add_argument("--nodes", default="1,2,3")
    ap.add_argument("--reset", action="store_true", help="delete both icing AEs first")
    ap.add_argument("--mn-admin", default="CAdmin")
    ap.add_argument("--in-admin", default="SAdminIcing")
    a = ap.parse_args()
    nodes = [int(n) for n in a.nodes.split(",")]

    mn, cloud = CSE(a.mn, "Cicing"), CSE(a.in_, "Cicing")
    if a.reset:
        print("reset:", CSE(a.mn, a.mn_admin).delete("EdgeMN/icing")[0],
              CSE(a.in_, a.in_admin).delete("Mobius/icing")[0])

    print("[edge] tinyIoT MN-CSE", a.mn)
    step("AE EdgeMN/icing", mn.create("EdgeMN", "ae", {"rn": "icing", "api": "Nicing", "rr": False, "srv": ["3"]}))
    step("ACP acp-icing", mn.create("EdgeMN/icing", "acp", {
        "rn": "acp-icing",
        "pv": {"acr": [{"acor": ["Cicing"], "acop": ALL},                 # field side (gateway IPE / virtual node)
                       {"acor": ["CEdgeAI"], "acop": ALL},                # edge inference AE (same host)
                       {"acor": ["CWarning"], "acop": READ_SUB},          # MEC warning AE (M2): read clean, write alert
                       {"acor": ["/Mobius4/CDashboard"], "acop": READ_SUB}]},  # cloud dashboard via the IN
        "pvs": {"acr": [{"acor": ["Cicing"], "acop": ALL}]}}))
    acpi = ["EdgeMN/icing/acp-icing"]
    for k in nodes:
        step(f"cnt node{k}", mn.create("EdgeMN/icing", "cnt", {"rn": f"node{k}", "acpi": acpi}))
        for c in ("raw", "clean", "health", "alert"):
            step(f"cnt node{k}/{c}", mn.create(f"EdgeMN/icing/node{k}", "cnt", {"rn": c, "acpi": acpi, "mni": 5000}))

    print("[cloud] Mobius4 IN-CSE", a.in_)
    step("AE Mobius/icing", cloud.create("Mobius", "ae", {"rn": "icing", "api": "Nicing", "rr": False, "srv": ["3"]}))
    step("ACP acp-icing", cloud.create("Mobius/icing", "acp", {
        "rn": "acp-icing",
        "pv": {"acr": [{"acor": ["Cicing"], "acop": ALL},
                       {"acor": ["/EdgeMN/CEdgeAI"], "acop": READ_SUB},   # edge AE reaching the IN through the MN
                       {"acor": ["CDashboard"], "acop": ALL}]},
        "pvs": {"acr": [{"acor": ["Cicing"], "acop": ALL}]}}))
    acpi = ["Mobius/icing/acp-icing"]
    step("modelRepo models", cloud.create("Mobius/icing", "mrp", {"rn": "models", "acpi": acpi}))
    step("modelDeploymentList edge-deploy", cloud.create("Mobius/icing", "mdp", {"rn": "edge-deploy", "acpi": acpi}))
    step("cnt history", cloud.create("Mobius/icing", "cnt", {"rn": "history", "acpi": acpi}))
    for k in nodes:
        step(f"cnt history/node{k}", cloud.create("Mobius/icing/history", "cnt", {"rn": f"node{k}", "acpi": acpi}))
    print("done")


if __name__ == "__main__":
    main()
