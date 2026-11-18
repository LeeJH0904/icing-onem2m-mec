# M2 verification — ETSI MEC integration (MEC 013 / 030 / 011)

**Criterion (interim report, slide 9):** MEC 011 service registration, MEC 013 radius check, MEC 030 warning
in one scenario.
**Result: partially met** (2026-11-14). MEC 013 and MEC 011 (full app-instance lifecycle **and** service
registration) work on the hosted Sandbox; the warning-decision path is verified; MEC 030 **DENM publication is
not confirmed** because the hosted Sandbox's V2X message broker fails server-side on a body that passes every
one of its checks.

## Environment under test

| Part | Value |
|---|---|
| MEC platform | ETSI MEC Sandbox (hosted), AdvantEDGE V2X Information Service API **v3.2.2** |
| Sandbox / MEP | `sbxdtxp20h` / `mep1`, scenario `4g-5g-macro-v2x` (emulated network over Monaco) |
| Vehicle UE | `10.100.0.1` (MEC-locatable); `10.10.0.1` is the pedestrian |
| API bases | Location (013) `.../mep1/location/v3` · V2X (030) `.../mep1/vis/v2` · App-enablement (011) `.../mep1/mec_app_support/v2` and `.../mep1/mec_service_mgmt/v1` |
| Our side | warning AE (`edge/warning_ae.py`) + `edge/mec_client.py` on one WSL2 Ubuntu host; our MN-CSE/IN-CSE run on the laptop, not inside the Sandbox |
| Deployment pattern | TR-0077 / TR-0086 **Option B** (oneM2M CSE and MEC on different nodes); we call MEC APIs as a MEC-app consumer, without a MEC-IPE and without MEC 033 |

The Sandbox behaviour below was captured by direct calls (`scripts/mec_probe.py`, `scripts/mec011_register.py`);
the AdvantEDGE source confirms the request shapes where cited.

## MEC 013 — Location (radius check)

| Check | Observed |
|---|---|
| `GET {loc}/queries/users?address=10.100.0.1` | 200; real-time `locationInfo` (latitude/longitude as 1-element arrays), `zoneId`, `accessPointId` |
| `GET {loc}/queries/distance?address=&latitude=&longitude=` | 200; `terminalDistance.distance` (metres, integer) |
| API version | the Location API is mounted at **v3**; `.../location/v2` answers 404 |

The warning AE uses `distance` to decide whether the vehicle is within a node's radius. **Only the vehicle is a
MEC-locatable UE**; the icing nodes are not UEs, so their coordinates are configured in `edge/mec_config.json`,
not queried. The emulated vehicle drives a long deterministic loop, so a single 2-minute `--track` window shows
only one segment of it; the three nodes are spread across observed segments with `radius_m` = 700.

## MEC 030 — V2X Information Service (warning publication)

| Step | Observed |
|---|---|
| `POST {vis}/subscriptions` (`filterCriteria.msgType ["denm"]`) | **201**, subscription id returned; `DELETE` → 204 |
| `POST {vis}/publish_v2x_message` (valid body) | **500 with an empty body** |

The publication body is the v3.2.2 shape and passes every Sandbox check; the failure is after the checks, in the
V2X message broker. Evidence, via `scripts/mec_probe.py --diag` (one subscription open, eight variants A–H):

| Variant | Sandbox answer |
|---|---|
| A — valid body as sent (`hexadump` + hex content, `msgType "denm"`) | **500, empty body** |
| B — `msgContent` empty | 400 `"Mandatory attribute MsgContent is missing..."` |
| C/D/E — `msgType` = `nosuchtype` / `DENM` / `1` | 400 `"Mandatory attribute MsgType is missing..."` (only lower-case `denm` is accepted) |
| F — `msgRepresentationFormat` = `base64` | 500, empty body |
| G — Swagger "Example Value" with only the 4 values filled in | **500, empty body** (same as A) |
| H — Swagger "Example Value" verbatim (`"SEE_DESCRIPTION"`) | 400 |

A missing field or a wrong `msgType` is a 400 **with a reason**; the valid body (A) and the Sandbox's own example
filled in (G) both give an empty-body 500. So the body satisfies every Sandbox check and the broker stage fails
regardless of content. The Sandbox's own API console gives the same `500 / content-length: 0`
(`docs/evidence/mec030_publish_500_swagger_20261114.png`). Confirmed from the AdvantEDGE source: the broker
`Send` accepts only `msgRepresentationFormat == "hexadump"` (hex-decoded) and returns any other error as a bare
500 (`go-packages/meep-vis-traffic-mgr/mqtt.go`). This is **not** fixable from the client.

**Conclusion:** DENM *delivery* on the hosted Sandbox could not be confirmed. The warning AE records each attempt
as a "Sandbox V2X broker error" and `scripts/m2_check.py` reports it separately from the warning decision.
Note the pre-verification record of 2026-10: an earlier Sandbox build answered `publish_v2x_message` with 204
(acceptance only, no delivery proof); the V2X service was later updated to v3.2.2.

## Warning decision path (what this project controls)

`scripts/m2_check.py` checks, for every alert the warning AE wrote to `EdgeMN/icing/nodeK/alert`: the node's edge
result at the measured time was **Frozen**, the reported MEC-013 distance is within the radius, a MEC-030
publication was attempted, and the cloud can read the newest alert through the IN-CSE.

| Run (offline stand-in `sim/mock_mec.py`, 543 rows) | Warning decision | MEC 030 delivery |
|---|---|---|
| broker working (`--broker` default) | **PASS** (4 warnings, 0 problems) | 4 × 204 |
| broker failing (`--broker-fail`, mirrors the hosted Sandbox) | **PASS** (4 warnings, 0 problems) | 4 × "Sandbox broker error (500, empty)" |

The decision logic and the oneM2M recording are correct in both cases; only the Sandbox-side delivery differs.
`M1 end-to-end` stays PASS under the same runs. The stand-in only exercises the AE logic; it is not the Sandbox.

## MEC 011 — Edge Platform Application Enablement

App instance `icing-warning` (id `63562521-722e-409e-82f5-bf17aa753eb6`) created in the Sandbox UI
("MEC app instance manager" → NEW — on the hosted Sandbox the appInstanceId must be provisioned manually).
Driven by `scripts/mec011_register.py`:

| Step | Request | Observed |
|---|---|---|
| App ready | `POST {app}/applications/{id}/confirm_ready` `{"indication":"READY"}` | **204** |
| Termination subscription | `POST {app}/applications/{id}/subscriptions` (`AppTerminationNotificationSubscription`) | **201**, id returned; `DELETE` → 204 |
| Service discovery | `GET {svc}/services` | **200**; 7 MEP services ACTIVE: mec011, 012, 013, 015, 016, 028, **030** |
| Service registration | `POST {svc}/applications/{id}/services` (`ServiceInfo`, `serName "icing-warning"`) | **201**, `serInstanceId ecda6261-72bd-445f-93a3-899ab76517ff` (no auth token required); `DELETE` → 204 |

`confirm_termination` is valid only **after** the platform notifies the app of termination; called unprompted it
returns 400 `"Unexpected App Confirmation Termination Notification"` (expected), so it is left out of the normal
lifecycle (gated behind `--confirm-termination`).

**Conclusion:** our warning service completes the full MEC 011 lifecycle — readiness confirmation, termination
subscription, service discovery — **and** registers itself as a discoverable MEC service, then deregisters
cleanly. This is beyond the fallback of "service list only" planned for M2. To capture a screenshot of the
registered service, run `scripts/mec011_register.py --register-service --keep` and then `GET {svc}/services`.

## Status summary

| Item | State |
|---|---|
| MEC 013 position + distance | verified on the hosted Sandbox |
| MEC 030 subscribe / delete | verified on the hosted Sandbox |
| MEC 030 DENM publication (delivery) | **not confirmed** — Sandbox V2X broker error (500, empty body), server-side |
| Warning decision (Frozen + radius + alert + cloud via IN) | verified (`m2_check.py`) |
| MEC 011 app-instance lifecycle | verified (confirm_ready + termination subscription + service discovery) |
| MEC 011 service registration | verified (`icing-warning` registered and discoverable) |
| MEC 033 | out of scope (not used) |

## How to reproduce

```bash
# MEC 013 + MEC 030 (needs a deployed 4g-5g-macro-v2x scenario; set <sandbox> to the current id)
export MEC_LOC_BASE=https://try-mec.etsi.org/<sandbox>/mep1/location/v3
export MEC_VIS_BASE=https://try-mec.etsi.org/<sandbox>/mep1/vis/v2
python scripts/mec_probe.py --diag          # position, distance, subscription, publish variants, cleanup

# full warning path (with the CSEs and AEs up, MEC_* exported)
bash scripts/demo_up.sh && python sim/virtual_node.py --pace 0.2 && python scripts/m2_check.py

# MEC 011 lifecycle + service registration (appInstanceId from the Sandbox UI)
export MEC_MEP_BASE=https://try-mec.etsi.org/<sandbox>/mep1
export MEC_APP_ID=<appInstanceId>
python scripts/mec011_register.py --register-service
```
