# Road Icing Monitor — oneM2M on ETSI MEC

Low-cost, low-power LoRa nodes watch road surfaces for ice. Their measurements flow through a
**oneM2M edge–cloud platform** — a tinyIoT **MN-CSE** on the MEC host and a Mobius4 **IN-CSE** in the cloud —
where an edge AI classifies each reading as **Dry / Wet / Frozen**. The cloud manages the AI model as oneM2M
resources and deploys it to the edge. (6th oneM2M International Hackathon, 2026.)

> **Honesty notes.** The classifier is a *reference model trained on synthetic time series*; it exists to prove
> the pipeline works end to end, and no accuracy figure in this repository is a claim about real roads. The
> synthetic-data generator and the training code are not included; the repository ships the trained model
> (`cloud/model_store/rf-v040.joblib`) and its metadata (`edge/model/model_meta.json`).
> `<mlModel>` / `<modelDeployment>` are oneM2M **TR-0071 resources (Release 5 candidate, not a ratified TS)** as
> implemented by Mobius4. MEC API integration (MEC 013 / 030 / 011) is milestone M2 and is marked as such below.

## Architecture

```
 Field                         MEC host (edge)                                   Cloud
 LoRa nodes ×N ──LoRa──► Pi 5 gateway IPE ──Mca ──► tinyIoT MN-CSE  /EdgeMN  ◄──Mcc──►  Mobius4 IN-CSE  /Mobius4
 (conductivity freq,                               EdgeMN/icing/nodeK/raw     remoteCSE   Mobius/icing/models       <modelRepo>
  road + air temp)                                 EdgeMN/icing/nodeK/clean   + request    Mobius/icing/edge-deploy  <modelDeploymentList>
                                                   EdgeMN/icing/nodeK/health  forwarding   Mobius/icing/history/nodeK  long-term copy
                                                        │  subscription                          ▲
                                                        ▼                                        │ subscription via IN
                                                   Edge inference AE  (CEdgeAI)            Dashboard AE (CDashboard)
                                                   features + RandomForest + safety rule   road authority web view
                                                        │ Frozen
                                                        ▼
                                                   Warning AE (CWarning) → MEC 013 distance → MEC 030 DENM
                                                   EdgeMN/icing/nodeK/alert  ← every published warning      (M2)
```

**Data path (per measurement).** The gateway IPE (`firmware/gateway_onem2m.py` on the Pi 5, or `sim/virtual_node.py`
without hardware) writes a contentInstance to
`EdgeMN/icing/nodeK/raw` → the MN notifies the edge AE → per-node reorder buffer → `StreamingClassifier`
(15 features incl. time-series history, RandomForest, "if unsure, say Frozen" safety rule, Dry→Wet confirmation)
→ result contentInstance in `EdgeMN/icing/nodeK/clean` → the dashboard AE, subscribed **through the IN-CSE**,
is notified and stores a copy in `Mobius/icing/history/nodeK`.

**Model lifecycle (cloud → edge).** `cloud/publish_model.py` registers the model as an `<mlModel>` (by URL `mmu`,
with its SHA-256 in the labels) and creates one `<modelDeployment>` per node whose `inr`/`our` point at the
**edge MN's** raw/clean containers (`/EdgeMN/EdgeMN/icing/nodeK/raw`). The edge AE, subscribed to the
deployment list *through the MN*, downloads the model, verifies the hash, subscribes to `inr`, and follows the
cloud's run/stop commands (`mcmd` → `mds`). The CSEs store and track the model; the edge AE runs it.

## Status

| Milestone | Done when | Status |
|---|---|---|
| M1 oneM2M pipeline | virtual node → MN → inference → /clean → IN works end to end | **done** — see [docs/M1_VERIFICATION.md](docs/M1_VERIFICATION.md) |
| M2 MEC integration | MEC 011 registration, 013 radius check, 030 warning in one scenario | 013 position/distance and 030 subscribe/delete confirmed on the hosted Sandbox (`location/v3`, `vis/v2`); 030 publish body (v3.2.2 `msgPropertiesValues` shape, `"hexadump"` + hex content, `msgType "denm"`) passes every Sandbox check, but the hosted Sandbox's V2X broker answers it with 500 / empty body (`mec_probe.py --diag`, 2026-11-14), so **DENM delivery on the hosted Sandbox is not confirmed**; the warning decision path passes (`m2_check.py`), and with a working broker (`sim/mock_mec.py`) every DENM is 204; MEC 011 app-instance lifecycle and service registration verified — see [docs/M2_MEC_INTEGRATION.md](docs/M2_MEC_INTEGRATION.md) |
| M3 real hardware | node firmware + Pi 5 IPE; real data passes the pipeline | **done** (2026-11-15) — a real LoRa packet traversed node → Pi gateway (`firmware/gateway_onem2m.py`) → Tailscale → home-server tinyIoT MN → edge inference → `/clean` → Mobius4 IN → dashboard; see [docs/M3_FIELD_SETUP.md](docs/M3_FIELD_SETUP.md) |
| M4 demo | demo video and final submission | submitted 2026-11-20 with the hackster.io project page and demo video |

## Repository layout

| Path | What |
|---|---|
| `common/onem2m.py` | minimal oneM2M HTTP client used by every AE (handles Mobius4/tinyIoT differences) |
| `edge/edge_ai_ae.py` | edge inference AE (MEC host) |
| `edge/model/` | `features.py`, `inference.py` (StreamingClassifier), model metadata, per-node calibration |
| `cloud/publish_model.py` | register `<mlModel>`, create `<modelDeployment>`s, run/stop |
| `cloud/model_store_server.py`, `cloud/model_store/` | serves the model file the `<mlModel>` points to |
| `cloud/dashboard_ae.py`, `cloud/dashboard.html` | road authority view + long-term copy on the IN |
| `edge/warning_ae.py`, `edge/mec_client.py` | MEC warning AE: Frozen node + vehicle within radius (MEC 013) → DENM (MEC 030) → `alert` container |
| `edge/mec_config.json`, `edge/denm_template.json` | node positions (Sandbox map), radius, vehicles; MEC 030 publication body |
| `sim/mock_mec.py` | offline stand-in for the Sandbox (tests only) |
| `scripts/mec_probe.py`, `scripts/m2_check.py` | Sandbox endpoint probe; M2 warning-path check |
| `sim/virtual_node.py`, `sim/data/` | replays synthetic measurements as the gateway IPE would |
| `scripts/setup_resources.py` | creates both resource trees and their ACPs |
| `scripts/build_tinyiot.sh`, `patches/` | builds the edge MN-CSE (tinyIoT, pinned commit) with this project's patches |
| `scripts/demo_up.sh`, `demo_down.sh` | start/stop the application side |
| `scripts/e2e_check.py` | the M1 acceptance check |
| `firmware/gateway_onem2m.py` | Pi 5 LoRa gateway / oneM2M IPE (M3): decodes the 12-byte packet and writes it as a cin to the edge MN |
| `firmware/node/` | sensor node firmware (Arduino Pro Mini, E22 LoRa, 74HC14 oscillator, DS18B20, DHT22) |

## Quick start (one Linux host, e.g. Ubuntu 24.04)

Full instructions: [docs/SETUP.md](docs/SETUP.md).

```bash
# 1. cloud IN-CSE: Mobius4 v4.24.5 on :7599 (PostgreSQL + PostGIS)  -> docs/SETUP.md §2
# 2. edge MN-CSE:  tinyIoT + patches on :3001
scripts/build_tinyiot.sh && (cd ../tinyIoT/source/server && ./server &)
# 3. application side
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
PY=python scripts/demo_up.sh          # resource trees, model store, edge AE, dashboard, model deployment
python sim/virtual_node.py --pace 0.2 # open http://127.0.0.1:8080/ while it runs
python scripts/e2e_check.py           # expect: M1 end-to-end: PASS
```

## Interoperability findings (Mobius4 v4.24.5 ↔ tinyIoT c140495)

Running two independent oneM2M implementations against each other surfaced these; details in
[patches/README.md](patches/README.md) and [docs/M1_VERIFICATION.md](docs/M1_VERIFICATION.md).

1. tinyIoT's `<remoteCSE>` registration carries `dcse`, `ast`, `spi`; Mobius4 v4.24.5 refuses them
   (`dcse is not allowed`) → patch 01 drops them. (Mobius4 v4.25.0 accepts `dcse`; this project stays on v4.24.5.)
2. Forwarded discovery fails: tinyIoT drops `ty` and appends `fo=1`, and Mobius4 refuses `fo`
   (`fc => fo is not allowed`) → the edge AE looks deployments up by name instead.
3. Notification shape differs: tinyIoT sends `con` as a string and `sur` as a resource ID; Mobius4 parses `con`
   into an object → every reader goes through `parse_con()`, and every result carries `node_id`.
4. tinyIoT requires `cnf` = `application/json:0` and a string `con`; Mobius4 accepts both.
5. Notification order is not guaranteed under load → per-node reorder buffer in the edge AE.
6. `<modelDeployment>` `inr`/`our` may name resources on another CSE (SP-relative IDs) — Mobius4 stores them,
   which lets the cloud deploy a model onto the edge's containers.

ETSI MEC Sandbox (hosted, 2026-11-14): the Location API is at `.../location/v3`, the V2X API at `.../vis/v2`;
a publication needs the `msgPropertiesValues` shape, `msgType` as a lower-case enum name (`"denm"`; `"DENM"`/`"1"`
are refused), and `msgRepresentationFormat: "hexadump"` with hex content. A body meeting all of that is answered
500 with an empty body (broker stage), so delivery could not be confirmed there. The Sandbox's own API console
gives the same 500 with `content-length: 0` for that body
([docs/evidence/mec030_publish_500_swagger_20261114.png](docs/evidence/mec030_publish_500_swagger_20261114.png)).

## Acknowledgements

[Mobius4](https://github.com/IoTKETI/mobius4) (KETI) · [tinyIoT](https://github.com/seslabSJU/tinyIoT)
(Sejong University SESLab) · ETSI MEC Sandbox · oneM2M.

## License

MIT — see [LICENSE](LICENSE). Exception: `patches/` modify [tinyIoT](https://github.com/seslabSJU/tinyIoT) and
are provided under its BSD 3-Clause License. tinyIoT and Mobius4 are not redistributed in this repository.
