"""M1 completion check: virtual node -> MN -> edge inference -> /clean -> IN, end to end.

For every node it compares three things:
  sent      rows the virtual node wrote into EdgeMN/icing/node{K}/raw
  clean     results the edge AE wrote into EdgeMN/icing/node{K}/clean   (read on the MN)
  history   copies the dashboard AE stored in Mobius/icing/history/node{K} (read on the IN)
and checks that every edge result equals what the same model gives when run offline on the same rows.
The label agreement line is printed for transparency only: the labels are synthetic, so it is not a
performance figure.

usage: python scripts/e2e_check.py [--nodes 1,2,3] [--limit 0]
"""
import argparse
import os
import sys

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
sys.path.insert(0, os.path.join(HERE, "..", "edge", "model"))
from onem2m import CSE, parse_con  # noqa: E402


def clean_results(mn, cb, k, page=150):
    # tinyIoT caps one discovery at DEFAULT_DISCOVERY_LIMIT (150, +1 row that signals "more"), so page with ofst
    paths, ofst = [], 0
    while True:
        code, data = mn.retrieve(f"{cb}/icing/node{k}/clean", fu=1, ty=4, lim=page, ofst=ofst)
        batch = data.get("m2m:uril", []) if code == 200 and isinstance(data, dict) else []
        paths += batch
        if len(batch) <= page:
            break
        ofst += page
    out = {}
    for p in dict.fromkeys(paths):
        c, d = mn.retrieve(p)
        if c == 200:
            r = parse_con(d["m2m:cin"]["con"])
            out[r["ts"]] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mn", default="http://127.0.0.1:3001")
    ap.add_argument("--in", dest="in_", default="http://127.0.0.1:7599")
    ap.add_argument("--edge-stats", default="http://127.0.0.1:9800/stats")
    ap.add_argument("--data", default=os.path.join(HERE, "..", "sim", "data", "stream_demo_labeled.csv"))
    ap.add_argument("--model", default=os.path.join(HERE, "..", "cloud", "model_store", "rf-v040.joblib"))
    ap.add_argument("--nodes", default="1,2,3")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    from inference import StreamingClassifier
    mn, cloud = CSE(a.mn, "Cicing"), CSE(a.in_, "Cicing")
    nodes = [int(n) for n in a.nodes.split(",")]
    df = pd.read_csv(a.data)
    df = df[df.node_id.isin(nodes)]
    if a.limit:
        df = df.groupby("node_id", group_keys=False).head(a.limit)
    offline = StreamingClassifier(model_path=a.model)
    try:
        stats = requests.get(a.edge_stats, timeout=5).json()
    except Exception:
        stats = {}

    ok_all = True
    tot = {"sent": 0, "clean": 0, "match": 0, "history": 0, "label": 0}
    print(f"{'node':<6}{'sent':>6}{'clean':>7}{'match':>7}{'IN hist':>9}{'late':>6}   label agreement (synthetic)")
    for k in nodes:
        rows = df[df.node_id == k].sort_values("ts", kind="stable")
        expect = {r.ts: offline.update({"ts": r.ts, "node_id": k, "freq": r.freq, "air_temp": r.air_temp,
                                        "air_hum": r.air_hum, "road_temp": r.road_temp})["state"]
                  for r in rows.itertuples(index=False)}
        got = clean_results(mn, "EdgeMN", k)
        match = sum(got.get(ts, {}).get("state") == st for ts, st in expect.items())
        code, h = cloud.retrieve(f"Mobius/icing/history/node{k}")
        hist = h["m2m:cnt"].get("cni", 0) if code == 200 else 0
        label = sum(got.get(r.ts, {}).get("state") == r.state_name for r in rows.itertuples(index=False))
        late = stats.get(f"node{k}-rf", {}).get("late", "?")
        print(f"node{k:<2}{len(rows):>6}{len(got):>7}{match:>7}{hist:>9}{late!s:>6}   {label}/{len(rows)}")
        ok_all &= (len(got) == len(rows) == match == hist)
        for key, v in (("sent", len(rows)), ("clean", len(got)), ("match", match), ("history", hist), ("label", label)):
            tot[key] += v
    print(f"{'total':<6}{tot['sent']:>6}{tot['clean']:>7}{tot['match']:>7}{tot['history']:>9}         {tot['label']}/{tot['sent']}")
    print("M1 end-to-end:", "PASS" if ok_all else "FAIL",
          "(every row reached /clean on the MN and the IN history, and every edge result equals offline inference)"
          if ok_all else "")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
