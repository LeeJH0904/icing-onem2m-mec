"""MEC 011 (Edge Platform Application Enablement) app-instance lifecycle for our warning AE.

Drives, against the ETSI MEC Sandbox MEP, the lifecycle of an app instance that was created in the
Sandbox UI ("MEC app instance manager" -> NEW, giving an appInstanceId):

  Step 1 (lifecycle, default):
    1. confirm_ready   POST {app}/applications/{id}/confirm_ready   {"indication":"READY"}   -> 204
    2. subscribe       POST {app}/applications/{id}/subscriptions   AppTerminationNotificationSubscription -> 201 (+Location)
    3. services        GET  {svc}/services                          -> 200, list serName/serInstanceId/state
    4. cleanup         DELETE the subscription (confirm_termination is NOT called by default: it is valid only
                       after the platform signals termination, else the MEP answers 400; use --confirm-termination)
  Step 2 (--register-service): also
    3b. register       POST {svc}/applications/{id}/services         ServiceInfo -> 201 (+Location, serInstanceId)
        (and DELETE it again on cleanup unless --keep)

  app = {MEP}/mec_app_support/v2 , svc = {MEP}/mec_service_mgmt/v1
  MEP = https://try-mec.etsi.org/<sandbox>/mep1  (from --mep-base, $MEC_MEP_BASE,
        or derived from $MEC_LOC_BASE by stripping a trailing /location/vN)

Every request prints METHOD URL -> STATUS and the response body, so the Sandbox itself confirms the exact
shapes (the AdvantEDGE source on labs.etsi.org was unreachable when this was written). Nothing is left behind
unless --keep. This does NOT invent an appInstanceId: pass the one the UI shows (--app-id or $MEC_APP_ID).

Usage:
  export MEC_MEP_BASE=https://try-mec.etsi.org/<sandbox>/mep1
  export MEC_APP_ID=<the appInstanceId from the UI>
  python scripts/mec011_register.py                 # step 1 only
  python scripts/mec011_register.py --register-service   # step 1 + step 2
  python scripts/mec011_register.py --keep          # don't terminate/deregister at the end
"""
import argparse
import json
import os
import sys

import requests


def mep_base(arg):
    base = arg or os.environ.get("MEC_MEP_BASE")
    if not base:
        loc = os.environ.get("MEC_LOC_BASE", "")
        for tail in ("/location/v3", "/location/v2", "/location/v1"):
            if loc.rstrip("/").endswith(tail):
                base = loc.rstrip("/")[: -len(tail)]
                break
    if not base:
        sys.exit("set --mep-base or MEC_MEP_BASE (e.g. https://try-mec.etsi.org/<sandbox>/mep1), "
                 "or MEC_LOC_BASE so it can be derived")
    return base.rstrip("/")


def call(method, url, body=None, timeout=15):
    """Send one request and print METHOD URL -> STATUS + body. Returns (status, parsed-or-text, resp)."""
    r = requests.request(method, url, json=body, timeout=timeout,
                         headers={"Accept": "application/json"})
    try:
        parsed = r.json()
        shown = json.dumps(parsed)
    except ValueError:
        parsed, shown = (r.text or ""), (r.text or "").strip()
    loc = r.headers.get("Location")
    print(f"  {method:6} {url}\n         -> {r.status_code}"
          + (f"  Location: {loc}" if loc else "")
          + (f"  body: {shown[:300]}" if shown else "  body: (empty)"), flush=True)
    return r.status_code, parsed, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mep-base", default=None)
    ap.add_argument("--app-id", default=os.environ.get("MEC_APP_ID"),
                    required=not os.environ.get("MEC_APP_ID"))
    ap.add_argument("--callback", default="https://example.com/mec011-termination")
    ap.add_argument("--register-service", action="store_true", help="also register our service (step 2)")
    ap.add_argument("--ser-name", default="icing-warning")
    ap.add_argument("--ser-endpoint", default="https://example.com/icing-warning")
    ap.add_argument("--keep", action="store_true", help="do not deregister / unsubscribe at the end")
    ap.add_argument("--confirm-termination", action="store_true",
                    help="also POST confirm_termination (only valid AFTER the platform signals termination; "
                         "otherwise the MEP answers 400 'Unexpected App Confirmation Termination Notification')")
    a = ap.parse_args()

    mep = mep_base(a.mep_base)
    app = f"{mep}/mec_app_support/v2"
    svc = f"{mep}/mec_service_mgmt/v1"
    aid = a.app_id
    print(f"MEP base : {mep}\napp id   : {aid}\n")

    results = {}
    sub_id = None
    ser_id = None

    # 1. confirm_ready
    print("1) confirm_ready")
    st, _, _ = call("POST", f"{app}/applications/{aid}/confirm_ready", {"indication": "READY"})
    results["confirm_ready"] = st                       # 204 expected

    # 2. app-termination subscription
    print("2) app-termination subscription")
    sub_body = {"subscriptionType": "AppTerminationNotificationSubscription",
                "callbackReference": a.callback,
                "appInstanceId": aid}
    st, data, r = call("POST", f"{app}/applications/{aid}/subscriptions", sub_body)
    results["subscribe"] = st                           # 201 expected
    if st in (200, 201):
        loc = r.headers.get("Location") or (((data or {}).get("_links") or {}).get("self") or {}).get("href", "")
        sub_id = loc.rstrip("/").rsplit("/", 1)[-1] if loc else None
        print(f"   subscription id: {sub_id}")

    # 3. list services
    print("3) service list (GET /services)")
    st, data, _ = call("GET", f"{svc}/services")
    results["services"] = st
    if st == 200 and isinstance(data, list):
        for s in data:
            print(f"     - {s.get('serName')}  id={s.get('serInstanceId')}  state={s.get('serState') or s.get('state')}")

    # 3b. register our service (step 2)
    if a.register_service:
        print("3b) register our service (POST /applications/{id}/services)")
        ser_body = {
            "serName": a.ser_name,
            "version": "1.0.0",
            "state": "ACTIVE",
            "serializer": "JSON",
            "scopeOfLocality": "MEC_HOST",
            "consumedLocalOnly": True,
            "isLocal": True,
            "serCategory": {"href": "catalogue1", "id": "icing", "name": "Icing", "version": "1.0"},
            "transportInfo": {
                "id": "icing-warning-rest", "name": "REST", "type": "REST_HTTP",
                "protocol": "HTTP", "version": "2.0",
                "endpoint": {"uris": [a.ser_endpoint]},
                "security": {"oAuth2Info": {"grantTypes": ["OAUTH2_CLIENT_CREDENTIALS"], "tokenEndpoint": ""}},
            },
        }
        st, data, r = call("POST", f"{svc}/applications/{aid}/services", ser_body)
        results["register_service"] = st                # 201 expected
        if st in (200, 201):
            loc = r.headers.get("Location") or ""
            ser_id = (data or {}).get("serInstanceId") or (loc.rstrip("/").rsplit("/", 1)[-1] if loc else None)
            print(f"   serInstanceId: {ser_id}")

    # 4. cleanup
    if not a.keep:
        print("4) cleanup")
        if a.register_service and ser_id:
            call("DELETE", f"{svc}/applications/{aid}/services/{ser_id}")
        if sub_id:
            call("DELETE", f"{app}/applications/{aid}/subscriptions/{sub_id}")
        # confirm_termination is valid only after the platform notifies the app of termination; calling it
        # unprompted returns 400 "Unexpected App Confirmation Termination Notification" (seen 2026-11-14), so
        # it is gated behind --confirm-termination and left out of the normal lifecycle.
        if a.confirm_termination:
            call("POST", f"{app}/applications/{aid}/confirm_termination", {"operationAction": "TERMINATING"})
    else:
        print("4) --keep: leaving subscription"
              + ("/service" if a.register_service else "") + " in place")

    print("\nSUMMARY:", json.dumps(results))
    ok = results.get("confirm_ready") == 204 and results.get("services") == 200
    print("MEC 011 lifecycle:", "confirm_ready + service list OK" if ok
          else "see statuses above (not the expected 204/200)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
