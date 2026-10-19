"""Vendored Aquanta cloud API client.

Replaces the external ``aquanta==0.2`` PyPI dependency. Interface-compatible with it
(``Aquanta(email, password)``, ``.devices()``, ``aquanta[id].water/.infocenter/.advanced``,
``.set_boost/.delete_boost/.set_away/.delete_away``) so the rest of the integration needs
only an import swap, while fixing/adding:

  * token lifecycle: Firebase refresh + automatic re-auth on 401 (the pip client authenticated
    once and never refreshed -> the integration would fail every poll until reloaded);
  * setpoint and mode writes (the pip client had none);
  * per-call errors raised as RuntimeError (what __init__.py / config_flow.py already catch).

Reconstructed from the Aquanta Android app v2.11.1 and cross-checked against aquanta==0.2.
  [OBSERVED] = seen verbatim in the app / aquanta==0.2, or confirmed live against a real account.
  Units: setPoint and water.temperature are both stored in degC (confirmed from app source:
  the app does Ju(advanced.setPoint,"degC").toNumber(tempUnits) on read and the inverse on write).
"""

from __future__ import annotations

import time
from typing import Any

import requests

FIREBASE_WEB_API_KEY = (
    "AIzaSyBHWHB8Org9BWCH-YFzis-8oEbMaKmI2Tw"  # [OBSERVED] public client key
)
IDENTITY_TOOLKIT = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)
SECURE_TOKEN = "https://securetoken.googleapis.com/v1/token"
API_ROOT = "https://api.aquanta.io"
API_V2 = API_ROOT + "/v2"

SETPOINT_MIN = 10  # [OBSERVED] app setPointMin=10 degC
SETPOINT_MAX = 110  # [OBSERVED] app setPointMax=110 degC

# [OBSERVED] mutually-exclusive mode flags in `advanced`
MODE_FIELDS = {
    "thermostat": "thermostatEnabled",
    "timer": "timerEnabled",
    "tou": "touEnabled",
    "intelligence": "intelEnabled",
}


class AquantaHelper:
    """GET/PUT/PATCH/DELETE against api.aquanta.io, with token refresh and 401 retry."""

    def __init__(self, session: requests.Session, timeout: int, reauth):
        self._session = session
        self._timeout = timeout
        self._reauth = reauth  # callable: () -> None, refreshes token + headers
        self.headers: dict[str, str] = {}

    def _do(self, method: str, url: str, **kw):
        resp = self._session.request(
            method, url, timeout=self._timeout, headers=self.headers, **kw
        )
        if resp.status_code == 401:
            self._reauth()
            resp = self._session.request(
                method, url, timeout=self._timeout, headers=self.headers, **kw
            )
        return resp

    def get(self, path: str, params: dict | None = None):
        resp = self._do("GET", API_V2 + path, params=params)
        if resp.ok:
            return resp.json()
        raise RuntimeError(f"Aquanta: GET {path} failed: {resp.status_code}")

    def put(self, path: str, value: Any):
        resp = self._do("PUT", API_V2 + path, json=value)
        if not resp.ok:
            raise RuntimeError(f"Aquanta: PUT {path} failed: {resp.status_code}")

    def patch(self, path: str, value: Any):
        resp = self._do("PATCH", API_V2 + path, json=value)
        if not resp.ok:
            raise RuntimeError(f"Aquanta: PATCH {path} failed: {resp.status_code}")
        return resp.json() if resp.content else None

    def delete(self, path: str):
        resp = self._do("DELETE", API_V2 + path)
        if not resp.ok:
            raise RuntimeError(f"Aquanta: DELETE {path} failed: {resp.status_code}")


class Aquanta:
    """Authenticated Aquanta session.

    Auth [OBSERVED, identical in app + pip client]:
      1. Firebase sign-in -> idToken (+ refreshToken, expiresIn)
      2. GET {API_ROOT}/auth?idtoken=<idToken> -> {"apiKey": <session token>}
      3. Authorization: Bearer <apiKey> on all /v2 calls
    """

    def __init__(self, email: str, password: str, timeout: int = 10):
        self._email = email
        self._password = password
        self._timeout = timeout
        self._session = requests.Session()
        self._refresh_token: str | None = None
        self._id_token_exp = 0.0
        self._helper = AquantaHelper(self._session, timeout, self._reauth)
        self._devices: dict[int, "AquantaDevice"] | None = None
        self._authenticate()

    # --- auth ---

    def _firebase_sign_in(self) -> None:
        r = self._session.post(
            IDENTITY_TOOLKIT,
            params={"key": FIREBASE_WEB_API_KEY},
            json={
                "email": self._email,
                "password": self._password,
                "returnSecureToken": True,
            },
            timeout=self._timeout,
        )
        if not r.ok:
            raise RuntimeError("Aquanta: Firebase sign-in failed")
        d = r.json()
        self._refresh_token = d["refreshToken"]
        self._id_token_exp = time.time() + int(d.get("expiresIn", 3600)) - 60
        return d["idToken"]

    def _firebase_refresh(self):
        if not self._refresh_token:
            return self._firebase_sign_in()
        r = self._session.post(
            SECURE_TOKEN,
            params={"key": FIREBASE_WEB_API_KEY},
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            timeout=self._timeout,
        )
        if not r.ok:
            return self._firebase_sign_in()
        d = r.json()
        self._refresh_token = d["refresh_token"]
        self._id_token_exp = time.time() + int(d.get("expires_in", 3600)) - 60
        return d["id_token"]

    def _exchange_key(self, id_token: str) -> None:
        r = self._session.get(
            API_ROOT + "/auth", params={"idtoken": id_token}, timeout=self._timeout
        )
        if not r.ok:
            raise RuntimeError("Aquanta: /auth token exchange failed")
        self._helper.headers = {"Authorization": f"Bearer {r.json()['apiKey']}"}

    def _authenticate(self) -> None:
        self._exchange_key(self._firebase_sign_in())

    def _reauth(self) -> None:
        """Called on 401: refresh Firebase token and re-exchange the Aquanta key."""
        self._exchange_key(self._firebase_refresh())

    # --- devices (pip-compatible) ---

    def devices(self):
        if self._devices is None:
            data = self._helper.get("/devices", params={"inlineAddress": "true"})
            self._devices = {
                d["id"]: AquantaDevice(self._helper, d["id"]) for d in (data or [])
            }
        return self._devices

    def __len__(self):
        return len(self.devices())

    def __contains__(self, key):
        return key in self.devices()

    def __iter__(self):
        return iter(self.devices().items())

    def __getitem__(self, key):
        return self.devices()[key]


class AquantaDevice:
    """One Aquanta device. Reads/writes mirror app + pip semantics."""

    def __init__(self, helper: AquantaHelper, device_id: int):
        self._helper = helper
        self._id = device_id

    def _p(self, name: str) -> str:
        return f"/devices/{self._id}/{name}"

    # reads [OBSERVED]
    @property
    def water(self):
        return self._helper.get(self._p("water"))

    @property
    def infocenter(self):
        return self._helper.get(self._p("infocenter"))

    @property
    def advanced(self):
        return self._helper.get(self._p("advanced"))

    @property
    def timer(self):
        return self._helper.get(self._p("timer"))

    @timer.setter
    def timer(self, value):
        self._helper.put(self._p("timer"), value)

    # boost / away [OBSERVED] — start/end are ISO-8601 UTC strings (callers format them)
    def set_boost(self, start: str, end: str):
        self._helper.put(self._p("boost"), {"start": start, "end": end})

    def delete_boost(self):
        self._helper.delete(self._p("boost"))

    def set_away(self, start: str, end: str):
        self._helper.put(self._p("away"), {"start": start, "end": end})

    def delete_away(self):
        self._helper.delete(self._p("away"))

    # advanced / setpoint / mode [OBSERVED] — PATCH /v2/devices/advanced (batch), wire-confirmed
    def patch_advanced(self, fields: dict[str, Any]):
        """[OBSERVED] body: [{id, patch:{op:"replace", path:<field>, value:<v>}}].
        Endpoint and body shape confirmed live (setPoint write returned 2xx and persisted).
        """
        body = [
            {"id": self._id, "patch": {"op": "replace", "path": k, "value": v}}
            for k, v in fields.items()
        ]
        return self._helper.patch("/devices/advanced", body)

    def set_setpoint(self, value: int):
        """Set thermostat target, in degC. Endpoint [OBSERVED]/wire-confirmed; unit is degC
        per the app's own conversion (Ju(...,"degC")). Range is the app's 10-110 degC.
        """
        v = int(round(value))
        if not (SETPOINT_MIN <= v <= SETPOINT_MAX):
            raise ValueError(f"setpoint {v} out of range {SETPOINT_MIN}-{SETPOINT_MAX}")
        return self.patch_advanced({"setPoint": v})

    def set_mode(self, mode: str):
        """Select one mode ('thermostat'|'timer'|'tou'|'intelligence') or 'off'.
        Uses the same PATCH /v2/devices/advanced path confirmed by the setPoint write;
        the per-flag mode semantics are reconstructed from the app (setThermostatEnabled).
        """
        if mode == "off":
            return self.patch_advanced({"controlEnabled": False})
        if mode not in MODE_FIELDS:
            raise ValueError(f"unknown mode '{mode}'")
        fields = {flag: (name == mode) for name, flag in MODE_FIELDS.items()}
        fields["controlEnabled"] = True
        return self.patch_advanced(fields)
