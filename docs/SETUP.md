# Setup — one Linux host (tested on WSL2 Ubuntu 24.04 for M1/M2 and a native Ubuntu server for M3)

Everything below runs in **one** Linux environment. Do not put Mobius4 in a Docker container on WSL: inside a
container `127.0.0.1` is the container itself, so the IN-CSE cannot forward requests to the MN on `:3001`.

Ports: Mobius4 IN-CSE 7599 · tinyIoT MN-CSE 3001 · edge AE 9800 · dashboard 8080 (web) / 8081 (notifications) ·
model store 8088.

## 0. Where the repository lives

All commands below use `$REPO` for this repository's folder. Set it in every new terminal:

```bash
REPO=~/onem2m/icing-onem2m-mec      # wherever you cloned or copied this repository
```

**Run the scripts as `bash scripts/...`.** That works whether or not the files carry the execute bit, which they
lose when the repository is copied from a Windows folder (`scripts/x.sh` alone then gives `Permission denied`,
and `sudo` does not change that).

## 1. Packages

```bash
sudo apt update
sudo apt install -y git curl build-essential pkg-config libsqlite3-dev libwebsockets-dev libssl-dev \
                    postgresql-16 postgresql-16-postgis-3 python3-venv
# Node.js 22 (Mobius4)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs
```

## 2. Cloud IN-CSE — Mobius4 v4.24.5  (terminal 1)

```bash
sudo service postgresql start
sudo -u postgres psql -c "CREATE USER icing WITH PASSWORD 'CHANGE_ME' SUPERUSER;"
sudo -u postgres psql -c "CREATE DATABASE mobius4 OWNER icing;"
sudo -u postgres psql -d mobius4 -c "CREATE EXTENSION IF NOT EXISTS postgis;"

git clone https://github.com/IoTKETI/mobius4.git ~/onem2m/mobius4
cd ~/onem2m/mobius4 && git checkout v4.24.5 && npm ci
cp "$REPO/cloud/mobius4/local.json.example" config/local.json
nano config/local.json               # db.pw = the password from CREATE USER above
node mobius4.js                      # keep this terminal open
```

Check: `curl -s localhost:7599/health` → `{"status":"ok",...}`.
Pinned to v4.24.5: Mobius4 changed quickly in 2026-08..10 and later versions were not tested here.
`request.max_body_size` is raised to 20 MB only so a model can also be uploaded as bytes; the pipeline
registers it by URL.

## 3. Edge MN-CSE — tinyIoT (patched)  (terminal 2)

```bash
cd "$REPO"
bash scripts/build_tinyiot.sh ~/onem2m/tinyIoT   # clones c140495, applies patches/, configures as MN, builds
cd ~/onem2m/tinyIoT/source/server && ./server    # keep open; start after Mobius4
```

The log should show `Remote CSE registration: 201` (first run) or `check: 200` (already registered).
To start the MN from an empty database, stop it, delete `data.db`, and delete its old registration on the IN:
`curl -X DELETE -H "X-M2M-Origin: SAdminIcing" -H "X-M2M-RI: d" -H "X-M2M-RVI: 3" localhost:7599/Mobius/EdgeMN`.

**Keep both CSE terminals untouched while they run.** Clicking or dragging inside a Windows console window starts
a text selection that pauses the program writing to it (tinyIoT logs constantly), and `Ctrl+Z` stops it.
A paused CSE still accepts connections but never answers. Fix: `Esc` (selection) or `fg` (Ctrl+Z) in that window.

## 4. Application side  (terminal 3)

### 4-1. Virtual environment inside the repository

```bash
cd "$REPO"
python3 -m venv .venv                 # creates $REPO/.venv (ignored by git)
source .venv/bin/activate             # the prompt now starts with (.venv)
which python3                         # must print .../icing-onem2m-mec/.venv/bin/python3
pip install -r requirements.txt       # scikit-learn 1.9.1, numpy 2.5.3, pandas 3.0.6 pinned
```

- The versions are pinned because the model file (`cloud/model_store/rf-v040.joblib`) was saved with them; loading it with other versions is not
  guaranteed to give the same predictions. Verified on Python 3.12 (Ubuntu 24.04 default) and 3.13: identical
  states on all 543 demo rows.
- Activate the environment in **every new terminal** (`cd "$REPO" && source .venv/bin/activate`).
  `deactivate` leaves it. While it is active, `python3` is the environment's Python, so the scripts need no `PY=`.
- A `.venv` created in WSL works only in WSL (not from Windows Python), and vice versa.

### 4-2. Run

```bash
bash scripts/demo_up.sh               # checks both CSEs, fresh resource trees, model store, edge AE,
                                      # dashboard, model deployment (about 5 s)
python sim/virtual_node.py --pace 0.2 # 543 synthetic rows, about 2 minutes; ends with "done: sent=543 failed=0"
                                      # meanwhile open http://127.0.0.1:8080/ in a Windows browser
python scripts/e2e_check.py           # M1 acceptance check -> "M1 end-to-end: PASS"
```

More:

```bash
bash scripts/demo_up.sh --keep               # restart the AEs, keep resources and data
python cloud/publish_model.py --command stop # cloud stops the edge model; --command run resumes
python sim/virtual_node.py --pace 0.05       # faster (~30 s); --limit N sends N rows per node
python scripts/e2e_check.py --limit N        # ...then check with the same N
bash scripts/demo_down.sh                    # stop the AEs (CSEs keep running)
```

Logs: `logs/edge.log`, `logs/dashboard.log`, `logs/store.log`; edge counters: `curl localhost:9800/stats`.

### 4-3. MEC warning AE (M2) — ETSI MEC Sandbox

Deploy the `4g-5g-macro-v2x` network on https://try-mec.etsi.org (GitHub login), then use its API bases:

```bash
export MEC_LOC_BASE=https://try-mec.etsi.org/<sandbox>/mep1/location/v3   # MEC 013 (location API is v3)
export MEC_VIS_BASE=https://try-mec.etsi.org/<sandbox>/mep1/vis/v2        # MEC 030 (VIS API is v2)

python scripts/mec_probe.py --track 120   # 1) position, distance, subscription, DENM publish (204), cleanup
                                          # 2) prints the vehicle route for 2 minutes
```

- The two APIs are mounted at different versions on the hosted Sandbox: Location is `.../location/v3`
  and VIS is `.../vis/v2`. `.../location/v2` answers 404.
- The publish body is the v3.2.2 shape (`msgContent` + `msgRepresentationFormat` + `msgPropertiesValues`,
  with `msgType`/`locationInfo` inside `msgPropertiesValues`); it is already in `edge/denm_template.json`.
  The Sandbox broker accepts **only** `"msgRepresentationFormat": "hexadump"` with a **hex** `msgContent`;
  any other encoding (e.g. `base64`) is answered `500 "MQTT encoding not supported"`. `build_denm` hex-encodes
  for this reason. If a Sandbox update changes the schema, paste the Swagger "Example Value" of
  `POST /publish_v2x_message` into `edge/denm_template.json` (keep the structure) and probe again.
- `python scripts/mec_probe.py --diag` prints the exact publish body and the Sandbox's answer to six variants.
  On 2026-11-14 the hosted Sandbox answered the valid body with **500 and an empty body**, while the variants
  with a missing field or a wrong `msgType` got 400 with a reason: the body passes every Sandbox check and its
  V2X message broker fails afterwards. That is not fixable from the client; the warning AE records it as
  "Sandbox V2X broker error" and `m2_check.py` reports it separately from the warning decision.
- Put the three nodes **on the printed route** in `edge/mec_config.json`, or no vehicle will come within
  `radius_m` (500 m). The Sandbox map is an emulated network over Monaco, not a real deployment.

```bash
bash scripts/demo_up.sh                 # with MEC_* set it also starts the warning AE (:9810)
python sim/virtual_node.py --pace 0.2
python scripts/m2_check.py              # warning decision (Frozen + radius + alert + cloud) PASS/FAIL,
                                        # plus the MEC 030 delivery tally; --strict also requires every 204
```

The warning AE keeps one MEC 030 subscription open while it runs (the Sandbox refuses to publish without one)
and deletes it when stopped (`bash scripts/demo_down.sh`). Its `callbackReference` is a local URL, so the
Sandbox accepts the DENMs (204) but cannot deliver them back here.
Replay runs ~4,500x faster than real time (15-minute measurements every 0.2 s) while vehicles move in real
time, so which node/vehicle pairs meet differs from run to run.

Offline test without the Sandbox: `python sim/mock_mec.py` (local stand-in that reproduces the request rules
observed on the hosted Sandbox) and `MEC_LOC_BASE=http://127.0.0.1:8090/sbx/mep1/location/v2`,
`MEC_VIS_BASE=http://127.0.0.1:8090/sbx/mep1/vis/v2`. It proves the AE logic only, not the Sandbox.
If you stop `virtual_node.py` midway, `e2e_check.py` reports FAIL (it expects every row); run
`bash scripts/demo_up.sh` to reset and replay.

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `bash: scripts/....sh: Permission denied` | no execute bit (copied from a Windows folder) | `bash scripts/....sh` |
| `bash: PY=python: No such file or directory` | variable written after `bash` | `PY=python bash scripts/demo_up.sh` - or activate `.venv` and drop `PY=` |
| `!! ... did not answer within 5 s` | that CSE is paused or hung | in its window: `Esc` (selection) / `fg` (Ctrl+Z); otherwise `Ctrl+C` and start it again (Mobius4 first) |
| `!! port(s) ... still in use` | AEs from an earlier run still running | `pkill -f 'cloud/dashboard_ae.py\|edge/edge_ai_ae.py\|cloud/model_store_server.py'`, run again |
| `!! ... exited right after start` | the AE crashed on start | read `logs/<name>.log` |
| dashboard page is blank | browser kept an old empty response | `Ctrl+Shift+R`, or a private window |
| `ModuleNotFoundError: sklearn` (or numpy, pandas) | `.venv` not active in this terminal | `source .venv/bin/activate` |
| `MEC 013 ...` 404 | Location API is mounted at v3 on the hosted Sandbox | `MEC_LOC_BASE=.../mep1/location/v3` |
| `MEC 030 publish DENM: HTTP 500, empty body` | the Sandbox's V2X broker failed after the body passed its checks | not a body problem: confirm with `mec_probe.py --diag` (B-E answer 400 with a reason); `m2_check.py` reports it as "Sandbox broker error" |
| `MEC 030 publish DENM: HTTP 400 ...` with a reason | a field is missing or `msgType` is not a lower-case enum name | the reason names the field; compare `edge/denm_template.json` with the Swagger Example Value |

## 6. Across machines (M3: Pi 5 gateway, separate hosts)

Every AE takes its CSE URLs and its own public notification URL as arguments, e.g.
`edge/edge_ai_ae.py --mn http://EDGE:3001 --public http://EDGE:9800`,
`cloud/dashboard_ae.py --in http://CLOUD:7599 --public http://CLOUD:8081`,
`cloud/publish_model.py --store-url http://CLOUD:8088`. The CSE `poa` values and tinyIoT's `REMOTE_CSE_HOST`
(`IN_HOST=... bash scripts/build_tinyiot.sh`) must also be reachable addresses. On WSL2 a device on the LAN
reaches WSL only through Windows port forwarding (`netsh interface portproxy`) or WSL mirrored networking.
