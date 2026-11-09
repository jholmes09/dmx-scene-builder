"""Client for the Robe/Anolis E-Box built-in web page (REAP).

On a real E-Box Remote (software 6.6, Pass-Thr mode) the box does NOT bridge RDM over Art-Net
(ArtRdm), but its web page runs RDM on its own LED outputs: discover the connected Calumma
modules, set each one's address, DMX preset (mode) and label, and identify it. This module
drives those same endpoints the page's own JavaScript uses (all POST, form-encoded, HTTP basic
auth, default login robe / 2479). Field encoding was read from the box's page script:

  /rdm_test                  start discovery
  /ds_get_device             poll: one found device per call {d_uid, u_l, dmx_a(0-based),
                             dmx_p(1-based mode), dmx_p_c, sp, dmx_t, b_f, pwr, ...}; disc_done:1 at end
  /ds_setup_device           uid, u_l, dmx_a (0-based), dmx_p (0-based), [dmx_t, b_f, pwr (indexes)]
  /rdm_identify              uid, i_v (0/1)
  /ds_get_setup              poll after setup/identify until it returns d_uid; err==0 is success
  /oth_s                     read (no params) or write dmxh (0=on,1=off), ebm (0=standard,
                             1=pass-through), ic_od (0=disabled, 1=enabled)
  /sys_res                   restart the box ("Reset now"), needed after changing Output data
"""
from __future__ import annotations

import base64
import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

WEB_PORT = 80  # the box's web page; tests point this at a fake box
DEFAULT_USER = "robe"
DEFAULT_PASSWORD = "2479"
ESTA_ROBE = "5253"


class ReapError(RuntimeError):
    pass


def uid_to_str(d_uid: str) -> str:
    """REAP's 8-hex device id -> the app's RDM UID format '5253:012E2A57'."""
    return "%s:%s" % (ESTA_ROBE, d_uid.upper())


def uid_from_str(uid: str) -> str:
    """'5253:012E2A57' -> '012e2a57' (what the box's page expects)."""
    return uid.split(":")[-1].lower()


class Reap:
    def __init__(self, ip: str, user: str = DEFAULT_USER, password: str = DEFAULT_PASSWORD, timeout: float = 5.0):
        self.ip = ip
        self.timeout = timeout
        self._auth = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()

    # ------------------------------------------------------------ transport
    def _post(self, path: str, data: Optional[Dict] = None) -> dict:
        body = urllib.parse.urlencode(data or {}).encode()
        host = self.ip if WEB_PORT == 80 else "%s:%d" % (self.ip, WEB_PORT)
        req = urllib.request.Request("http://%s/%s" % (host, path.lstrip("/")), data=body, method="POST",
                                     headers={"Authorization": self._auth, "Accept-Encoding": "gzip",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ReapError("The box's web page rejected the login (robe / 2479).")
            raise ReapError("The box's web page answered %d for %s." % (e.code, path))
        except (urllib.error.URLError, OSError) as e:
            raise ReapError("Can't reach the box's web page at %s (%s)." % (self.ip, getattr(e, "reason", e)))
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            raise ReapError("Unexpected answer from the box's web page for %s." % path)

    # ------------------------------------------------------------ info
    def status(self) -> dict:
        return self._post("status_i")

    def available(self) -> bool:
        try:
            return "rdmu" in self.status()
        except ReapError:
            return False

    # ------------------------------------------------------------ RDM through the box
    def discover(self, timeout: float = 25.0) -> List[dict]:
        self._post("rdm_test")
        found: Dict[str, dict] = {}
        self.last_repeats = 0  # same light reported twice in one search: a sign of a noisy cable run
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self._post("ds_get_device")
            if r.get("d_uid"):
                if r["d_uid"] in found:
                    self.last_repeats += 1
                found[r["d_uid"]] = r
            if r.get("disc_done") == 1:
                return list(found.values())
            time.sleep(0.2)
        raise ReapError("The box's device search didn't finish within %d s." % timeout)

    def _wait_setup(self, d_uid: str, timeout: float = 8.0) -> dict:
        """Wait for the box to confirm a change to *this* light (ignore leftovers for others)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.25)
            r = self._post("ds_get_setup")
            if r.get("d_uid") and str(r["d_uid"]).lower() == d_uid.lower():
                return r
        raise ReapError("The fixture didn't confirm the change within %d s." % timeout)

    def setup(self, d_uid: str, label: str, address: int, mode: int, device: Optional[dict] = None,
              terminator: Optional[bool] = None) -> None:
        """Write label/address/mode (and optionally the line terminator) to one fixture."""
        if not 1 <= int(address) <= 512:
            raise ReapError("Address must be 1-512.")
        dev = device or {}
        count = int(dev.get("dmx_p_c") or 16)
        if not 1 <= int(mode) <= count:
            raise ReapError("Mode must be 1-%d on this fixture." % count)
        data = {"uid": d_uid, "u_l": (label or "")[:32], "dmx_a": int(address) - 1, "dmx_p": int(mode) - 1}
        sp = int(dev.get("sp") or 0)
        if sp & 1:
            cur = 1 if dev.get("dmx_t") == "on" else 0
            data["dmx_t"] = cur if terminator is None else (1 if terminator else 0)
        if sp & 2:
            data["b_f"] = 1 if dev.get("b_f") == "on" else 0
        if sp & 4:
            data["pwr"] = {"low": 0, "middle": 1, "high": 2}.get(dev.get("pwr"), 0)
        r = self._post("ds_setup_device", data)
        if r.get("status", 0) != 0:
            raise ReapError("The box refused the setting (status %s)." % r.get("status"))
        done = self._wait_setup(d_uid)
        if done.get("err", 0) != 0:
            raise ReapError("The fixture rejected the setting (error %s)." % done.get("err"))

    def identify(self, d_uid: str, on: bool) -> None:
        self._post("rdm_identify", {"uid": d_uid, "i_v": 1 if on else 0})
        self._wait_setup(d_uid)

    # ------------------------------------------------------------ box settings
    def other_settings(self) -> dict:
        """{'dmxh': 'on'|'off', 'ebm': 'standard'|'pass-through', 'ic_od': 'enabled'|'disabled'}"""
        return self._post("oth_s")

    def set_other(self, dmx_hold: Optional[bool] = None, pass_through: Optional[bool] = None,
                  output_data: Optional[bool] = None) -> None:
        cur = self.other_settings()
        data = {
            "dmxh": 0 if (cur.get("dmxh") == "on" if dmx_hold is None else dmx_hold) else 1,
            "ebm": 1 if (cur.get("ebm") == "pass-through" if pass_through is None else pass_through) else 0,
            "ic_od": 1 if (cur.get("ic_od") == "enabled" if output_data is None else output_data) else 0,
        }
        r = self._post("oth_s", data)
        if r.get("status") != 0:
            raise ReapError("The box didn't confirm the setting (status %s)." % r.get("status"))

    # Option lists exactly as the box's own settings page offers them (index = value written).
    DMX_INPUTS = ["wired DMX", "wireless DMX", "wireless out DMX", "ethernet"]
    ETH_MODES = ["Art-NET", "sACN", "gMA I", "gMA II"]

    @staticmethod
    def option_index(value, options) -> Optional[int]:
        """The box may report a setting as its index or its label; return the index, or None."""
        if value is None or value == "":
            return None
        try:
            i = int(value)
            return i if 0 <= i < len(options) else None
        except (TypeError, ValueError):
            pass
        low = str(value).strip().lower()
        for i, o in enumerate(options):
            if o.lower() == low:
                return i
        return None

    def dmx_settings(self) -> dict:
        """{'dmxa': box DMX address, 'dmxi': DMX input, 'rdmn': box label}"""
        return self._post("set_dmx")

    def eth_settings(self) -> dict:
        """{'ip', 'netm', 'ethm': Ethernet mode, 'igmpt'}"""
        return self._post("eth_s")

    def set_dmx_input(self, index: int) -> None:
        cur = self.dmx_settings()
        try:
            dmxa = int(cur.get("dmxa"))
        except (TypeError, ValueError):
            raise ReapError("Couldn't read the box's DMX settings, so nothing was changed.")
        r = self._post("set_dmx", {"dmxa": dmxa, "dmxi": int(index), "rdmn": (cur.get("rdmn") or "")[:32]})
        if r.get("status") != 0:
            raise ReapError("The box didn't confirm the DMX input change (status %s)." % r.get("status"))

    def set_eth_mode(self, index: int) -> None:
        """Change only the Ethernet mode. The box's IP and mask are sent back exactly as read, and
        nothing is written unless the box reports the IP we are talking to."""
        import ipaddress
        cur = self.eth_settings()
        ip, netm = str(cur.get("ip") or ""), str(cur.get("netm") or "")
        try:
            ipaddress.IPv4Address(ip)
            ipaddress.IPv4Address(netm)
        except ValueError:
            raise ReapError("Couldn't read the box's network settings, so nothing was changed.")
        if ip != self.ip:
            raise ReapError("The box reports IP %s, not %s, so nothing was changed." % (ip, self.ip))
        igmpt = self.option_index(cur.get("igmpt"), ["off"] + ["%d s" % n for n in range(1, 11)])
        r = self._post("eth_s", {"ip": ip, "netm": netm, "ethm": int(index), "igmpt": igmpt or 0})
        if r.get("status") != 0:
            raise ReapError("The box didn't confirm the Ethernet mode change (status %s)." % r.get("status"))

    def restart(self) -> None:
        try:
            self._post("sys_res")
        except ReapError:
            pass  # the box drops the connection as it restarts
