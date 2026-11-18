"""Virtual sensor nodes - stand-in for the LoRa nodes + Pi 5 gateway IPE while no field data exists.

Replays a synthetic time series (one row = one 15-minute measurement) and writes each row as a
contentInstance into the edge MN-CSE, exactly where the gateway IPE writes real packets:
    EdgeMN/icing/node{K}/raw    (originator Cicing)
Rows carry provenance="synthetic" so every downstream result can be traced back to simulated input.

usage: python sim/virtual_node.py --pace 0.2            # all nodes, 0.2 s per row
       python sim/virtual_node.py --nodes 1 --limit 60 --pace 0
"""
import argparse
import os
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from onem2m import CSE  # noqa: E402

FIELDS = ["freq", "air_temp", "air_hum", "road_temp"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mn", default="http://127.0.0.1:3001")
    ap.add_argument("--mn-cb", default="EdgeMN")
    ap.add_argument("--data", default=os.path.join(HERE, "data", "stream_demo_labeled.csv"))
    ap.add_argument("--nodes", default="1,2,3")
    ap.add_argument("--limit", type=int, default=0, help="rows per node (0 = all)")
    ap.add_argument("--pace", type=float, default=0.2, help="seconds between rows (real system: 900)")
    a = ap.parse_args()

    mn = CSE(a.mn, "Cicing")
    df = pd.read_csv(a.data)
    nodes = [int(n) for n in a.nodes.split(",")]
    df = df[df.node_id.isin(nodes)]
    if a.limit:
        df = df.groupby("node_id", group_keys=False).head(a.limit)
    df = df.sort_values(["ts", "node_id"], kind="stable")          # interleave nodes in time order
    print(f"virtual nodes {nodes}: {len(df)} rows, {df.ts.min()} .. {df.ts.max()}, pace {a.pace}s", flush=True)

    sent = failed = 0
    t0 = time.time()
    for r in df.itertuples(index=False):
        con = {"ts": r.ts, "node_id": int(r.node_id), **{f: float(getattr(r, f)) for f in FIELDS},
               "provenance": "synthetic", "src": "virtual-node"}
        code, data = mn.create_cin(f"{a.mn_cb}/icing/node{int(r.node_id)}/raw", con)
        if code == 201:
            sent += 1
        else:
            failed += 1
            print("  write failed", code, data, flush=True)
        if sent % 100 == 0 and sent:
            print(f"  {sent} rows sent ({time.time() - t0:.0f}s)", flush=True)
        time.sleep(a.pace)
    print(f"done: sent={sent} failed={failed} in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
