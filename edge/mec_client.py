"""Client for the two ETSI MEC APIs the warning AE uses, in the request formats the hosted
ETSI MEC Sandbox accepted during pre-verification (2026-10-14..17):

  MEC 013 Location   GET  {loc}/queries/users?address=<ue>                 -> userList.user[0].locationInfo
                     GET  {loc}/queries/distance?address=&latitude=&longitude=  -> terminalDistance.distance (m)
  MEC 030 V2X (VIS)  POST {vis}/subscriptions            V2xMsgSubscription, filterCriteria.msgType ["denm"]
                     POST {vis}/publish_v2x_message      204 = accepted (not "delivered")
                     DELETE {vis}/subscriptions/{id}

Sandbox request shapes (confirmed against the hosted Sandbox, AdvantEDGE V2X API v3.2.2):
  * a SUBSCRIPTION filterCriteria.msgType is an ARRAY of lower-case names (["denm"]);
  * a PUBLICATION body has three root attributes - msgContent, msgRepresentationFormat and
    msgPropertiesValues - and the message details (stdOrganization, msgType as a plain STRING,
    msgProtocolVersion, locationInfo{ecgi, geoArea}) live INSIDE msgPropertiesValues. A flat body
    with root msgType/locationInfo is answered 400 "Mandatory attribute MsgPropertiesValues is
    missing in the request body." (2026-11-14);
  * publishing is refused (400) while no V2X subscription exists;
  * the broker accepts ONLY msgRepresentationFormat == "hexadump" and hex-decodes msgContent
    (go-packages/meep-vis-traffic-mgr/mqtt.go). Any other value - including "base64" - is answered
    500 "MQTT encoding not supported", so msgContent is sent as HEX, not base64.
The full publication body comes from denm_template.json so it can be replaced by the Sandbox
Swagger "Example Value" if a Sandbox update changes it.
"""
import base64
import copy
import json
import os

import requests

HERE = os.path.dirname(os.path.abspath(__file__))


class MecClient:
    def __init__(self, loc_base, vis_base, template=None, timeout=10):
        self.loc = loc_base.rstrip("/")
        self.vis = vis_base.rstrip("/")
        self.timeout = timeout
        with open(template or os.path.join(HERE, "denm_template.json"), encoding="utf-8") as f:
            self.template = json.load(f)

    # ---------------- MEC 013 ----------------
    def user_location(self, address):
        r = requests.get(f"{self.loc}/queries/users", params={"address": address}, timeout=self.timeout)
        r.raise_for_status()
        users = (r.json().get("userList") or {}).get("user") or []
        if not users:
            return None
        li = users[0].get("locationInfo") or {}
        lat, lon = li.get("latitude"), li.get("longitude")
        lat = lat[0] if isinstance(lat, list) else lat          # MEC 013 returns arrays
        lon = lon[0] if isinstance(lon, list) else lon
        return {"lat": lat, "lon": lon, "zone": users[0].get("zoneId"), "ap": users[0].get("accessPointId")}

    def distance(self, address, lat, lon):
        r = requests.get(f"{self.loc}/queries/distance",
                         params={"address": address, "latitude": lat, "longitude": lon}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["terminalDistance"]["distance"]

    # ---------------- MEC 030 ----------------
    def subscribe_v2x(self, callback):
        body = {"subscriptionType": "V2xMsgSubscription", "callbackReference": callback,
                "filterCriteria": {"stdOrganization": "ETSI", "msgType": ["denm"]}}
        r = requests.post(f"{self.vis}/subscriptions", json=body, timeout=self.timeout)
        if r.status_code != 201:
            raise RuntimeError(f"V2X subscription refused: {r.status_code} {r.text[:200]}")
        href = r.headers.get("Location") or (r.json().get("_links", {}).get("self", {}) or {}).get("href", "")
        return href.rstrip("/").rsplit("/", 1)[-1]

    def delete_subscription(self, sub_id):
        return requests.delete(f"{self.vis}/subscriptions/{sub_id}", timeout=self.timeout).status_code

    def build_denm(self, lat, lon, info):
        """Fill the template: the warning position (msgPropertiesValues.locationInfo.geoArea)
        and the message content (msgContent at the root)."""
        body = {k: v for k, v in copy.deepcopy(self.template).items() if not k.startswith("_")}
        props = body.setdefault("msgPropertiesValues", {})
        props.setdefault("stdOrganization", "ETSI")
        props.setdefault("msgType", "denm")
        geo = props.setdefault("locationInfo", {}).setdefault("geoArea", {})
        geo["latitude"] = float(lat)
        geo["longitude"] = float(lon)
        raw = json.dumps(info, separators=(",", ":")).encode()
        # The Sandbox broker accepts only "hexadump" (hex-decoded), so hex is the default.
        fmt = str(body.get("msgRepresentationFormat", "hexadump")).lower()
        if fmt.startswith("hex"):          # "hexadump"
            body["msgContent"] = raw.hex()
        elif fmt.startswith("base64"):
            body["msgContent"] = base64.b64encode(raw).decode()
        else:
            body["msgContent"] = raw.decode()
        return body

    def publish_denm(self, lat, lon, info):
        body = self.build_denm(lat, lon, info)
        r = requests.post(f"{self.vis}/publish_v2x_message", json=body, timeout=self.timeout)
        return r.status_code, r.text[:300]
