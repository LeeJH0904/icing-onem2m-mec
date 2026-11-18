"""Check the ETSI MEC Sandbox endpoints the warning AE needs - run this where the Sandbox is reachable.

  python scripts/mec_probe.py --loc-base https://try-mec.etsi.org/<sandbox>/mep1/location/v2 \
                              --vis-base https://try-mec.etsi.org/<sandbox>/mep1/vis/v2 [--track 120]

Steps: MEC 013 vehicle position, MEC 013 distance to node1, MEC 030 subscription, DENM publication
(with edge/denm_template.json), subscription removal. --track N also prints the vehicle position every
5 s for N seconds, to place the virtual nodes on its route (edge/mec_config.json).
Leaves nothing behind in the Sandbox (the subscription is deleted even if a step fails).
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "edge"))
from mec_client import MecClient  # noqa: E402


def run_diag(mec, n1):
    """Publish a few variants while the subscription is open and show what the Sandbox answers.

    Reading the result:
      * B and C answered 400 with a JSON "detail"  -> the request reached the body checks, so the
        handler parses our JSON; an empty-body 500 on A then comes from after those checks.
      * A answered 500 with an empty body, B/C 400  -> our body passes every check the Sandbox makes;
        the failure is on the Sandbox side, not in the JSON.
      * A answered 500 WITH a JSON "detail"          -> read the detail: it names the problem field.
      * G answers like A                             -> the remaining differences from the Swagger example
        (msgProtocolVersion, ecgi values, geoArea, key order) do not change the Sandbox's answer.
      * H is the Swagger example verbatim: its "SEE_DESCRIPTION" / "string" placeholders are not valid values.
    """
    import copy
    import requests
    base = mec.build_denm(n1["lat"], n1["lon"], {"type": "probe", "causeCode": 6, "subCauseCode": 6})
    print("\n--diag: body sent in variant A")
    print(json.dumps(base, indent=2))

    # Swagger UI "Example Value" of POST /publish_v2x_message on the hosted Sandbox, verbatim (2026-11-14)
    swagger = {
        "msgContent": "string",
        "msgPropertiesValues": {
            "locationInfo": {
                "geoArea": {"latitude": 0.8008282, "longitude": 6.0274563},
                "ecgi": {"plmn": {"mnc": "mnc", "mcc": "mcc"}, "cellId": {"cellId": "cellId"}}},
            "msgProtocolVersion": 0,
            "msgType": "SEE_DESCRIPTION",
            "stdOrganization": "SEE_DESCRIPTION"},
        "msgRepresentationFormat": "string"}

    def swagger_filled(_):
        """The Swagger example with ONLY msgType, stdOrganization, msgContent and the format filled in;
        every other value (msgProtocolVersion 0, ecgi 'mcc'/'mnc'/'cellId', geoArea) left as in the example."""
        b = copy.deepcopy(swagger)
        b["msgContent"], b["msgRepresentationFormat"] = base["msgContent"], "hexadump"
        b["msgPropertiesValues"].update(msgType="denm", stdOrganization="ETSI")
        return b

    def variant(name, mutate):
        body = copy.deepcopy(base)
        out = mutate(body)
        body = out if isinstance(out, dict) else body
        try:
            r = requests.post(f"{mec.vis}/publish_v2x_message", json=body, timeout=10)
            text = r.text.strip().replace("\n", " ")
            print(f"  {name:<44} -> HTTP {r.status_code}  body: {text[:200] or '(empty)'}", flush=True)
        except requests.RequestException as e:
            print(f"  {name:<44} -> request failed: {e}", flush=True)

    print("\n--diag: variants")
    variant("A  as sent (hexadump, msgType 'denm')", lambda b: None)
    variant("B  msgContent empty (expect 400)", lambda b: b.update(msgContent=""))
    variant("C  msgType 'nosuchtype' (expect 400)", lambda b: b["msgPropertiesValues"].update(msgType="nosuchtype"))
    variant("D  msgType 'DENM'", lambda b: b["msgPropertiesValues"].update(msgType="DENM"))
    variant("E  msgType '1'", lambda b: b["msgPropertiesValues"].update(msgType="1"))
    variant("F  msgRepresentationFormat 'base64'", lambda b: b.update(msgRepresentationFormat="base64"))
    variant("G  Swagger example + only 4 values filled", swagger_filled)
    variant("H  Swagger example verbatim", lambda b: copy.deepcopy(swagger))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loc-base", default=os.environ.get("MEC_LOC_BASE"), required=not os.environ.get("MEC_LOC_BASE"))
    ap.add_argument("--vis-base", default=os.environ.get("MEC_VIS_BASE"), required=not os.environ.get("MEC_VIS_BASE"))
    ap.add_argument("--config", default=os.path.join(HERE, "..", "edge", "mec_config.json"))
    ap.add_argument("--track", type=int, default=0)
    ap.add_argument("--diag", action="store_true",
                    help="print the exact publish body and try a few variants (status + response body each)")
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))
    mec = MecClient(a.loc_base, a.vis_base)
    ue = cfg["vehicles"][0]
    n1 = cfg["nodes"]["1"]
    results, sub_id = [], None

    def step(name, fn):
        try:
            out = fn()
            results.append((name, "OK", out)); print(f"[OK]   {name}: {out}", flush=True); return out
        except Exception as e:
            results.append((name, "FAIL", str(e)[:200])); print(f"[FAIL] {name}: {str(e)[:300]}", flush=True)

    step(f"MEC 013 position of {ue}", lambda: mec.user_location(ue))
    step(f"MEC 013 distance {ue} -> node1 {n1['lat']},{n1['lon']}", lambda: f"{mec.distance(ue, n1['lat'], n1['lon'])} m")
    sub_id = step("MEC 030 V2X subscription (denm)", lambda: mec.subscribe_v2x("https://example.com/v2x-callback"))
    try:
        def pub():
            code, text = mec.publish_denm(n1["lat"], n1["lon"], {"type": "probe", "causeCode": 6, "subCauseCode": 6})
            if code == 500 and not (text or "").strip():
                raise RuntimeError("HTTP 500, empty body -> Sandbox V2X broker error after the body passed its checks "
                                   "(not a body problem; confirm with --diag: B-E answer 400 with a reason)")
            if code != 204:
                raise RuntimeError(f"HTTP {code} {text}  -> the reason above names the field; compare "
                                   "edge/denm_template.json with the Swagger Example Value")
            return "204 accepted"
        step("MEC 030 publish DENM", pub)
        if a.diag and sub_id:
            run_diag(mec, n1)
    finally:
        if sub_id:
            step("MEC 030 delete subscription", lambda: f"HTTP {mec.delete_subscription(sub_id)}")

    if a.track:
        print(f"\nvehicle {ue} track (every 5 s, {a.track} s):", flush=True)
        end = time.time() + a.track
        while time.time() < end:
            p = mec.user_location(ue) or {}
            print(f"  {time.strftime('%H:%M:%S')}  lat {p.get('lat')}  lon {p.get('lon')}  zone {p.get('zone')}", flush=True)
            time.sleep(5)

    bad = [r for r in results if r[1] != "OK"]
    print("\nSUMMARY:", "all steps OK" if not bad else f"{len(bad)} step(s) failed: " + ", ".join(r[0] for r in bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
