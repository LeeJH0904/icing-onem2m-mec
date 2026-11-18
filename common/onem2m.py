"""Minimal oneM2M HTTP client used by every AE in this project.

Works against both CSEs we run:
  * Mobius4 (IN-CSE, cloud)  - returns cin `con` as a parsed object
  * tinyIoT (MN-CSE, edge)   - returns cin `con` as a string, needs cnf "application/json:0"
so every reader goes through `parse_con()` and every writer uses `CNF`.
"""
import json
import uuid

import requests

RVI = "3"
CNF = "application/json:0"          # media-type:encoding - required by tinyIoT, accepted by Mobius4

# resource type codes (TS-0004; 101-104 are TR-0071 Rel-5 candidate types implemented by Mobius4)
TY = {"acp": 1, "ae": 2, "cnt": 3, "cin": 4, "csr": 16, "sub": 23,
      "mrp": 101, "mmd": 102, "mdp": 103, "dpm": 104}


class CSE:
    """One CSE endpoint seen with one originator."""

    def __init__(self, base, origin, timeout=30):
        self.base = base.rstrip("/")
        self.origin = origin
        self.timeout = timeout

    def _headers(self, ty=None):
        h = {"X-M2M-Origin": self.origin, "X-M2M-RI": uuid.uuid4().hex[:16],
             "X-M2M-RVI": RVI, "Accept": "application/json"}
        h["Content-Type"] = f"application/json;ty={ty}" if ty else "application/json"
        return h

    def request(self, method, path, ty=None, body=None, params=None):
        r = requests.request(method, f"{self.base}/{path.lstrip('/')}", headers=self._headers(ty),
                             data=json.dumps(body) if body is not None else None,
                             params=params, timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, data

    # ---- CRUD helpers -------------------------------------------------------------------------
    def create(self, parent, kind, attrs):
        return self.request("POST", parent, TY[kind], {f"m2m:{kind}": attrs})

    def retrieve(self, path, **params):
        return self.request("GET", path, params=params or None)

    def update(self, path, kind, attrs):
        return self.request("PUT", path, None, {f"m2m:{kind}": attrs})

    def delete(self, path):
        return self.request("DELETE", path)

    def create_cin(self, cnt_path, con):
        # tinyIoT refuses an object as con ("invalid attribute : con"), so always send a JSON string
        if not isinstance(con, str):
            con = json.dumps(con, separators=(",", ":"))
        return self.create(cnt_path, "cin", {"cnf": CNF, "con": con})

    def subscribe(self, path, rn, nu, net=(3,)):
        return self.create(path, "sub", {"rn": rn, "nu": [nu], "nct": 1, "enc": {"net": list(net)}})

    def latest(self, cnt_path):
        code, data = self.retrieve(f"{cnt_path}/la")
        if code != 200 or not isinstance(data, dict) or "m2m:cin" not in data:
            return None
        return parse_con(data["m2m:cin"].get("con"))


def parse_con(con):
    """cin content may arrive as an object (Mobius4) or a JSON string (tinyIoT)."""
    if isinstance(con, str):
        try:
            return json.loads(con)
        except ValueError:
            return con
    return con


def ok(code):
    return code in (200, 201)


def created_or_exists(code, data):
    """201, or 409/4105 'already exists' - lets setup scripts run twice."""
    return code == 201 or code == 409 or (isinstance(data, dict) and "already exists" in json.dumps(data).lower())
