"""Cloud side of the AI lifecycle: register the model and deploy it to the edge, all as oneM2M resources.

  <modelRepo> models      <- <mlModel> rf-v040   (mmu = model store URL, sha256 in labels)
  <modelDeploymentList> edge-deploy
        <- <modelDeployment> node{K}-rf  (moid = the model, inr/our = the edge MN's raw/clean)
  then mcmd=1 (run)  ->  mds becomes 1 (running) and the edge AE starts inferring.

<mlModel>/<modelDeployment> are oneM2M TR-0071 resources (Release 5 candidate, not a ratified TS)
as implemented by Mobius4. The CSE stores and tracks the model; the edge AE runs it.

usage:
  python cloud/publish_model.py                 # register + deploy to nodes 1,2,3 + run
  python cloud/publish_model.py --command stop  # stop all deployments (mcmd=0)
  python cloud/publish_model.py --command run   # run again
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
from onem2m import CSE, created_or_exists  # noqa: E402

META = os.path.join(HERE, "..", "edge", "model", "model_meta.json")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_", default="http://127.0.0.1:7599")
    ap.add_argument("--model", default=os.path.join(HERE, "model_store", "rf-v040.joblib"))
    ap.add_argument("--store-url", default="http://127.0.0.1:8088",
                    help="base URL where model_store_server.py serves cloud/model_store (must be reachable from the edge)")
    ap.add_argument("--nodes", default="1,2,3")
    ap.add_argument("--edge-csi", default="/EdgeMN", help="CSE-ID of the edge MN-CSE")
    ap.add_argument("--edge-cb", default="EdgeMN", help="CSEBase name of the edge MN-CSE")
    ap.add_argument("--command", choices=["deploy", "run", "stop"], default="deploy")
    a = ap.parse_args()

    cloud = CSE(a.in_, "Cicing")
    nodes = [int(n) for n in a.nodes.split(",")]
    meta = json.load(open(META, encoding="utf-8"))
    version = meta.get("model_version", "0.4.0")
    mmd_rn = "rf-v" + version.replace(".", "")
    deploy_path = "Mobius/icing/edge-deploy"

    if a.command in ("run", "stop"):
        mcmd = 1 if a.command == "run" else 0
        for k in nodes:
            code, data = cloud.update(f"{deploy_path}/node{k}-rf", "dpm", {"mcmd": mcmd})
            mds = data.get("m2m:dpm", {}).get("mds") if isinstance(data, dict) else data
            print(f"node{k}-rf mcmd={mcmd} -> {code} mds={mds}")
        return

    # 1) model file into the store, fingerprint it
    store_dir = os.path.join(HERE, "model_store")
    os.makedirs(store_dir, exist_ok=True)
    fname = os.path.basename(a.model)
    if os.path.abspath(os.path.dirname(a.model)) != os.path.abspath(store_dir):
        shutil.copy2(a.model, os.path.join(store_dir, fname))
    digest, size = sha256(a.model), os.path.getsize(a.model)
    mmu = f"{a.store_url.rstrip('/')}/{fname}"
    print(f"model {fname}  {size:,} B  sha256={digest[:16]}...  mmu={mmu}")

    # 2) <mlModel> (registered by URL; bytes stay in the store)
    attrs = {
        "rn": mmd_rn, "mmu": mmu, "vr": version, "mlt": "classification",
        "plf": f"scikit-learn {meta.get('sklearn_version')} / numpy {meta.get('numpy_version')}",
        "dc": ("RandomForest road-surface state classifier (Dry/Wet/Frozen), 15 features. "
               "Trained on SYNTHETIC time series - reference model for the pipeline demo, not a performance claim."),
        "lbl": [f"sha256:{digest}", f"size:{size}", "provenance:synthetic",
                "classes:" + ",".join(meta["classes"]), f"features:{len(meta['features'])}"],
        "acpi": ["Mobius/icing/acp-icing"],
    }
    code, data = cloud.create("Mobius/icing/models", "mmd", attrs)
    print("mlModel", code, "" if created_or_exists(code, data) else json.dumps(data)[:200])
    code, data = cloud.retrieve(f"Mobius/icing/models/{mmd_rn}")
    moid = data["m2m:mmd"]["ri"]
    print("  moid =", moid)

    # 3) one <modelDeployment> per node: input = edge raw, output = edge clean (SP-relative IDs on the MN)
    for k in nodes:
        base = f"{a.edge_csi}/{a.edge_cb}/icing/node{k}"
        code, data = cloud.create(deploy_path, "dpm", {
            "rn": f"node{k}-rf", "moid": moid, "inr": f"{base}/raw", "our": f"{base}/clean",
            "acpi": ["Mobius/icing/acp-icing"]})
        print(f"modelDeployment node{k}-rf", code, "" if created_or_exists(code, data) else json.dumps(data)[:200])

    # 4) run
    for k in nodes:
        code, data = cloud.update(f"{deploy_path}/node{k}-rf", "dpm", {"mcmd": 1})
        print(f"  run node{k}-rf -> {code} mds={data.get('m2m:dpm', {}).get('mds') if isinstance(data, dict) else data}")
    code, data = cloud.retrieve(deploy_path)
    print("deployment list:", {k: data["m2m:mdp"].get(k) for k in ("ndm", "nrm", "nsm")})


if __name__ == "__main__":
    main()
