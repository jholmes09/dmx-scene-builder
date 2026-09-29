"""HTTP server: static web app + JSON API."""
from __future__ import annotations

import json
import platform
import os
import logging
import mimetypes
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from . import __version__, artnet, fixtures, rdm
from .engine import Engine
from .node import ArtNetController, RdmError, local_interfaces
from .reap import Reap, ReapError, uid_from_str as reap_uid, uid_to_str as reap_uid_str
from .simulator import EBoxSimulator, demo_box
from .store import Store, new_box, new_fixture, new_float, new_id, patch_problems

log = logging.getLogger("scenebuilder.server")
WEB = Path(__file__).resolve().parent.parent / "web"


class App:
    def __init__(self, store: Store, ctl: ArtNetController, sim_port: int = 6455):
        self.store = store
        self.ctl = ctl
        self.engine = Engine(store, ctl)
        self.sim: Optional[EBoxSimulator] = None
        self.sim_port = sim_port
        self.scan_lock = threading.Lock()
        self.pause_during_rdm = True
        # (box ip, model_id) -> {personality index: {"mode": n or None, "footprint": f, "description": s}}
        self.personalities: dict = {}
        self._identify_timers: dict = {}
        self.reap_devices: dict = {}  # box ip -> {uid_str: device dict from the box's web page}

    # ---------------------------------------------------------- simulator
    def sim_on(self):
        if not self.sim:
            self.sim = demo_box(self.sim_port).start()
        return self.sim

    def sim_off(self):
        if self.sim:
            self.sim.stop()
            self.sim = None

    # ---------------------------------------------------------- helpers
    def box_target(self, fl: dict, box_id: str):
        for b in fl["boxes"]:
            if b["id"] == box_id:
                if not b.get("ip"):
                    raise ValueError("Set the box's IP address first.")
                return b["ip"], int(b.get("udp_port") or 6454), artnet.port_address(b["net"], b["subnet"], b["universe"])
        raise KeyError("box not found")

    def rdm_session(self):
        app = self

        class _S:
            def __enter__(self_):
                if not app.scan_lock.acquire(timeout=3):
                    raise RuntimeError("Busy talking to the box. Try again in a moment.")
                try:
                    if app.pause_during_rdm:
                        app.engine.pause_for_rdm()
                    elif app.engine.job and app.engine.job.get("state") == "running":
                        raise RuntimeError("A save is running. Wait for it to finish.")
                except Exception:
                    app.scan_lock.release()
                    raise

            def __exit__(self_, *a):
                if app.pause_during_rdm:
                    app.engine.resume_after_rdm()
                app.scan_lock.release()
        return _S()

    def artnet_alive(self, ip: str, port: int, wait: float = 0.8) -> bool:
        """Does a box answer an Art-Net poll sent straight to it?"""
        t0 = time.time()
        self.ctl._send_quiet(artnet.build_poll(), ip, port)
        while time.time() - t0 < wait:
            time.sleep(0.05)
            with self.ctl._lock:
                if any(n.get("ip") == ip and n.get("seen", 0) >= t0 - 0.01 for n in self.ctl.nodes.values()):
                    return True
        return False

    def personality_map(self, ip, port, pa, uid, info) -> dict:
        key = (ip, info["model_id"], info["personality_count"])
        if key in self.personalities:
            return self.personalities[key]
        pmap = {}
        for n in range(1, min(info["personality_count"], 32) + 1):
            d = self.ctl.personality_description(ip, port, pa, uid, n)
            if not d:
                continue
            pmap[n] = {"mode": mode_from_description(n, d), "footprint": d["footprint"],
                       "description": d["description"]}
        self.personalities[key] = pmap
        return pmap

    def scan(self, fl: dict, box_id: str, flush: bool = False) -> dict:
        ip, port, pa = self.box_target(fl, box_id)
        # Preferred: the box's own web page runs RDM on its outputs. On a real E-Box Remote (6.6)
        # this finds every Calumma in Pass-Thr, while Art-Net RDM only ever sees the box itself.
        reap = Reap(ip)
        if reap.available():
            expected = sum(1 for fx in fl["fixtures"] if fx.get("box_id") == box_id)
            found = {}
            with self.rdm_session():
                for _ in range(2):  # the box's search sometimes misses a light; one retry fills the gaps
                    found.update({d["d_uid"]: d for d in reap.discover()})
                    if len(found) >= expected:
                        break
            devs = list(found.values())
            self.reap_devices[ip] = {reap_uid_str(d["d_uid"]): d for d in devs}
            # A light left "unknown" (address cleared) by a failed change gets its real values back.
            with self.store.lock:
                for fx in fl["fixtures"]:
                    d = self.reap_devices[ip].get(fx.get("uid") or "")
                    if d is not None and fx.get("box_id") == box_id and fx.get("address") is None:
                        fx["address"] = int(d.get("dmx_a", 0)) + 1
                        m = int(d.get("dmx_p") or 1)
                        if m in fixtures.MODES.get(fx["variant"], {}) or m == fixtures.SAVE_MODE:
                            fx["mode"] = m
                        fl["rev"] = fl.get("rev", 0) + 1
                        self.store.bump(fl)
            devices = [reap_scan_entry(d) for d in sorted(devs, key=lambda d: d.get("dmx_a", 0))]
            return {"box_id": box_id, "ip": ip, "port_address": pa, "devices": devices, "via": "box web page",
                    "time": time.time()}
        with self.rdm_session():
            uids = self.ctl.tod(ip, port, pa, flush=flush)
            devices = []
            for uid in uids:
                d = {"uid": rdm.uid_str(uid), "ok": True}
                try:
                    info = self.ctl.device_info(ip, port, pa, uid)
                    d.update(info)
                    d["label"] = self.ctl.get_text(ip, port, pa, uid, rdm.PID_DEVICE_LABEL) or ""
                    d["model"] = self.ctl.get_text(ip, port, pa, uid, rdm.PID_DEVICE_MODEL_DESCRIPTION) or ""
                    pmap = self.personality_map(ip, port, pa, uid, info)
                    cur = pmap.get(info["personality"], {})
                    d["personality_name"] = cur.get("description", "")
                    d["mode"] = cur.get("mode") or info["personality"]
                    d["modes_available"] = sorted({v["mode"] for v in pmap.values() if v.get("mode")})
                    d["variant_guess"] = guess_variant(d["mode"], d["model"], d["personality_name"], d["modes_available"])
                    d["kind"] = "ebox" if "BOX" in (d["model"] or "").upper() else "fixture"
                except RdmError as e:
                    d.update(ok=False, error=str(e))
                devices.append(d)
        return {"box_id": box_id, "ip": ip, "port_address": pa, "devices": devices, "time": time.time()}

    def mfr_params(self, ip, port, pa, uid, with_values: bool = True) -> list:
        """Manufacturer-specific RDM parameters (0x8000-0xFFDF), described by the fixture itself."""
        info = self.ctl.device_info(ip, port, pa, uid)
        key = ("params", ip, info["model_id"], info["software_id"])
        if key not in self.personalities:
            try:
                pids = self.ctl.supported_parameters(ip, port, pa, uid)
            except RdmError:
                pids = []
            descs = []
            for pid in pids:
                if 0x8000 <= pid <= 0xFFDF:
                    try:
                        descs.append(self.ctl.parameter_description(ip, port, pa, uid, pid))
                    except (RdmError, ValueError):
                        descs.append({"pid": pid, "size": 1, "data_type": "unknown", "command_class": "?",
                                      "can_get": True, "can_set": True, "min": 0, "max": 255, "default": 0,
                                      "description": "Manufacturer PID 0x%04X" % pid})
            self.personalities[key] = descs
        out = []
        for d in self.personalities[key]:
            d = dict(d)
            d["pid_hex"] = "0x%04X" % d["pid"]
            d["value"] = None
            if with_values and d.get("can_get") and d.get("size") in (1, 2, 4):
                try:
                    d["value"] = self.ctl.get_param(ip, port, pa, uid, d["pid"], d["size"], d["data_type"].startswith("int"))
                except RdmError as e:
                    d["error"] = str(e)
            out.append(d)
        return out

    def find_save_param(self, params: list):
        for d in params:
            text = d.get("description", "").lower()
            if "init" in text and d.get("can_set"):
                return d
        return None

    def personality_for_mode(self, ip, port, pa, uid, mode: int) -> int:
        info = self.ctl.device_info(ip, port, pa, uid)
        pmap = self.personality_map(ip, port, pa, uid, info)
        for n, v in pmap.items():
            if v.get("mode") == mode:
                return n
        if not pmap:
            return mode  # fixture didn't describe its personalities; assume index == mode number
        raise RdmError("This fixture doesn't offer Mode %d. It offers: %s" % (
            mode, ", ".join("Mode %s" % v["mode"] for v in pmap.values() if v.get("mode")) or "unknown"))

    def identify(self, ip, port, pa, uid, on: bool, seconds: float = 15.0):
        self.ctl.identify(ip, port, pa, uid, on)
        key = (ip, uid)
        t = self._identify_timers.pop(key, None)
        if t:
            t.cancel()
        if on:
            def off():
                try:
                    with self.scan_lock:
                        self.ctl.identify(ip, port, pa, uid, False)
                except RdmError:
                    pass
            t = threading.Timer(seconds, off)
            t.daemon = True
            t.start()
            self._identify_timers[key] = t


def mode_from_description(index: int, d: dict):
    """Robe describes personalities like 'Mode 11' or a text name; map to the Calumma chart mode number."""
    import re
    m = re.search(r"mode\s*(\d+)", d.get("description", ""), re.I)
    if m:
        return int(m.group(1))
    # Fall back: if the index is a known Calumma mode with the same footprint, trust it.
    for v, modes in fixtures.MODES.items():
        if index in modes and len(modes[index]["roles"]) == d.get("footprint"):
            return index
    return None


def cloud_folders() -> list:
    """Likely backup destinations on whichever computer runs the app (Mac or Windows)."""
    home = Path.home()
    cands = [("Dropbox", home / "Dropbox")]
    cs = home / "Library" / "CloudStorage"  # macOS: Dropbox, OneDrive, Google Drive live here
    if cs.is_dir():
        for d in sorted(cs.iterdir()):
            if d.is_dir():
                cands.append((d.name.split("-")[0].replace("GoogleDrive", "Google Drive"), d))
    cands += [("iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"),
              ("iCloud Drive", home / "iCloudDrive"),
              ("OneDrive", Path(os.environ["OneDrive"]) if os.environ.get("OneDrive") else home / "OneDrive"),
              ("Google Drive", home / "Google Drive"), ("Google Drive", Path("G:/My Drive")),
              ("Documents", home / "Documents"), ("Desktop", home / "Desktop")]
    out, seen, names = [], set(), {}
    for name, path in cands:
        try:
            if not path.is_dir():
                continue
            real = str(path.resolve())
        except OSError:
            continue
        if real in seen:
            continue  # same folder reached two ways (e.g. ~/Dropbox -> CloudStorage/Dropbox)
        seen.add(real)
        names[name] = names.get(name, 0) + 1
        label = name if names[name] == 1 else "%s (%s)" % (name, path.name)
        out.append({"name": label, "path": str(path)})
    return out


def list_folders(path: str) -> dict:
    """Subfolders of `path` (inside the user's home, or a cloud drive letter on Windows), for the folder picker."""
    if not path:
        return {"path": None, "parent": None, "folders": [], "places": cloud_folders()}
    p = Path(os.path.expanduser(path)).resolve()
    allowed = [Path.home().resolve()] + [Path(c["path"]).resolve() for c in cloud_folders()]
    if not any(p == a or a in p.parents for a in allowed):
        raise ValueError("Pick a folder inside your home folder or a cloud drive.")
    if not p.is_dir():
        raise ValueError("That folder doesn't exist: %s" % p)
    try:
        subs = sorted((c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")), key=lambda c: c.name.lower())
    except OSError as e:
        raise ValueError("Can't open that folder: %s" % e)
    parent = p.parent if any(p.parent == a or a in p.parent.parents for a in allowed) else None
    return {"path": str(p), "parent": str(parent) if parent else None,
            "folders": [{"name": c.name, "path": str(c)} for c in subs[:300]], "places": cloud_folders()}


def reap_variant(mode: int):
    if mode in (11, 12):
        return "TW"
    if mode == 13:
        return "PW"
    if mode == fixtures.SAVE_MODE:
        return None  # Mode 7 exists on RGBW and (possibly) tunable white: keep the patch's type
    return "RGBW"


def reap_scan_entry(d: dict) -> dict:
    mode = int(d.get("dmx_p") or 1)
    variant = reap_variant(mode)
    avail = sorted(fixtures.MODES[variant or "RGBW"]) + ([fixtures.SAVE_MODE] if variant not in ("RGBW", None) else [])
    fp = None
    try:
        fp = fixtures.footprint(variant if variant and mode in fixtures.MODES[variant] else "RGBW", mode)
    except ValueError:
        pass
    return {"uid": reap_uid_str(d["d_uid"]), "ok": True, "address": int(d.get("dmx_a", 0)) + 1, "mode": mode,
            "personality": mode, "personality_count": d.get("dmx_p_c"), "footprint": fp,
            "label": (d.get("u_l") or "").strip(), "model": d.get("d_l") or "", "variant_guess": variant,
            "modes_available": avail, "kind": "fixture", "via": "reap", "terminator": d.get("dmx_t")}


def guess_variant(mode, model: str, pers_name: str, modes_available=()) -> str:
    text = (model + " " + pers_name).upper()
    avail = set(modes_available or ())
    if avail and avail <= {11, 12, 13, 7}:
        return "TW" if (11 in avail or 12 in avail) else "PW"
    if avail & {1, 2, 3, 5, 6}:
        return "RGBW"
    if mode in (11, 12) or "TW" in text or "TUNABLE" in text:
        return "TW"
    if mode == 13 or " PW" in text:
        return "PW"
    return "RGBW"


class Handler(BaseHTTPRequestHandler):
    server_version = "DMXSceneBuilder/" + __version__
    app: App = None  # set by make_server

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---------------------------------------------------------- plumbing
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    def _static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        f = (WEB / path.lstrip("/")).resolve()
        if WEB not in f.parents and f != WEB or not f.is_file():
            self.send_error(404)
            return
        data = f.read_bytes()
        self.send_response(200)
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if f.suffix == ".webmanifest":
            ctype = "application/manifest+json"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if not parts or parts[0] != "api":
                if method == "GET" and parts[:1] == ["patch"] and len(parts) == 2:
                    return self._patch_sheet(parts[1])
                if method == "GET":
                    return self._static(url.path)
                return self._err("not found", 404)
            return self._api(method, parts[1:], parse_qs(url.query))
        except (ValueError, KeyError, RuntimeError, RdmError) as e:
            return self._err(e, 404 if isinstance(e, KeyError) else 400)
        except Exception as e:  # pragma: no cover
            traceback.print_exc()
            return self._err("Internal error: %s" % e, 500)

    # ---------------------------------------------------------- API
    def _api(self, method, p, q):
        app, store, eng = self.app, self.app.store, self.app.engine
        if p == ["state"] and method == "GET":
            snap = store.snapshot()
            for fl in snap["floats"]:
                fl["problems"] = patch_problems(fl)
            return self._json({"version": __version__, "project": snap, "engine": eng.status(),
                               "settings": {"pause_during_rdm": app.pause_during_rdm},
                               "modes": fixtures.mode_catalog(), "sim": app.sim.state() if app.sim else None})
        if p == ["debug"] and method == "GET":
            return self._json({"rdm_log": app.ctl.rdm_log[-50:], "raw_rdm": app.ctl.raw_rdm_log[-40:],
                               "packets_sent": app.ctl.packets_sent, "packets_received": app.ctl.packets_received})
        if p == ["status"] and method == "GET":
            return self._json({"engine": eng.status(), "sim": app.sim.state() if app.sim else None,
                               "rev": store.data.get("rev", 0)})
        if p == ["network"] and method == "GET":
            return self._json({"interfaces": local_interfaces(), "http_port": self.server.server_address[1],
                               "artnet_port": app.ctl.port, "bind_error": app.ctl.bind_error,
                               "pinned_ip": app.ctl.preferred_interface})
        if p == ["network", "interface"] and method == "POST":
            ip = (self._body().get("ip") or "").strip() or None
            if ip:
                known = {i["ip"] for i in local_interfaces()}
                if ip not in known:
                    return self._err("That address isn't one of this machine's network adapters right now.")
            # Only changes which adapter Find/Scan broadcasts on; live output is untouched.
            app.ctl.set_preferred_interface(ip)
            store.save_settings(network_interface=ip)
            return self._json({"interfaces": local_interfaces(), "pinned_ip": ip, "bind_error": app.ctl.bind_error})
        if p == ["discover"] and method == "POST":
            b = self._body()
            extra = [(t["ip"], int(t.get("udp_port") or 6454)) for t in b.get("targets", []) if t.get("ip")]
            if app.sim:
                extra.append(("127.0.0.1", app.sim.port))
            nodes = app.ctl.poll(extra, wait=float(b.get("wait", 2.0)))
            return self._json({"nodes": nodes})
        if p == ["sim"] and method == "POST":
            on = bool(self._body().get("on"))
            if on:
                app.sim_on()
            else:
                app.sim_off()
            return self._json({"sim": app.sim.state() if app.sim else None})
        if p == ["folders"] and method == "GET":
            return self._json(list_folders((q.get("path") or [""])[0]))
        if p == ["settings"] and method == "GET":
            return self._json(self._settings_view())
        if p == ["settings"] and method == "POST":
            b = self._body()
            if "pause_during_rdm" in b:
                app.pause_during_rdm = bool(b["pause_during_rdm"])
            if "mirror_dir" in b:
                d = (b.get("mirror_dir") or "").strip() or None
                if d:
                    d = os.path.expanduser(d)
                    if not os.path.isdir(d):
                        raise ValueError("That folder doesn't exist on this computer: %s" % d)
                store.save_settings(mirror_dir=d)
                store.flush()
            return self._json(self._settings_view())
        if p == ["output"] and method == "POST":
            b = self._body()
            action = b.get("action")
            if eng.applying and action in ("activate", "resume", "release", "white_test", "hold"):
                raise RuntimeError("Lights are being changed right now. Wait for that to finish.")
            if action == "activate":
                eng.set_active(b.get("float_id"), output=True)
            elif action == "release":
                eng.release()
            elif action == "resume":
                eng.resume()
            elif action == "blackout":
                eng.set_blackout(b.get("on", True))
            elif action == "white_test":
                eng.set_white_test(b.get("method"), int(b.get("k") or 6500), b.get("mix"))
            elif action == "hold":
                eng.set_hold(bool(b.get("on", True)))
            else:
                raise ValueError("unknown action")
            return self._json({"engine": eng.status()})
        if p == ["white_cal"] and method == "POST":
            b = self._body()
            k = int(b.get("k") or 6500)
            if not 1800 <= k <= 10000:
                raise ValueError("Color temperature must be 1800-10000K.")
            with store.lock:
                cal = store.data.setdefault("white_cal", {}).setdefault("RGBW", {})
                if b.get("reset_all"):
                    cal.clear()
                elif b.get("delete"):
                    cal.pop(str(k), None)
                else:
                    mix = [max(0.0, min(1.0, float(x))) for x in b["mix"]]
                    if len(mix) != 4:
                        raise ValueError("mix must be [r, g, b, w]")
                    cal[str(k)] = mix
                store.bump()
            eng.refresh()
            return self._json({"white_cal": store.data["white_cal"]})
        if p and p[0] == "palette":
            return self._palette(method, p[1:])
        if p == ["project"] and method == "GET":
            return self._json(store.snapshot())
        if p == ["project"] and method == "PUT":
            if eng.applying:
                raise RuntimeError("Lights are being changed right now. Try again in a moment.")
            eng.set_active(None)
            store.replace_project(self._body())
            eng.refresh()
            return self._json({"ok": True})
        if p == ["project", "merge_plan"] and method == "POST":
            return self._json(store.plan_merge(self._body()))
        if p == ["project", "merge_apply"] and method == "POST":
            b = self._body()
            if eng.applying and (b.get("resolutions") or {}).get(eng.applying) == "theirs":
                raise RuntimeError("Lights on that float are being changed right now. Try again in a moment.")
            if eng.active_float and (b.get("resolutions") or {}).get(eng.active_float) == "theirs":
                eng.release()  # the other computer's patch may not match these lights
            res = store.apply_merge(b.get("incoming") or {}, b.get("resolutions") or {})
            eng.refresh()
            return self._json(res)
        if p == ["project", "name"] and method == "POST":
            with store.lock:
                store.data["project"] = str(self._body().get("name") or "Untitled")[:80]
                store.mark_dirty()
            return self._json({"ok": True})
        if p == ["floats"] and method == "POST":
            b = self._body()
            fl = new_float(b.get("code", ""), b.get("name") or "New float")
            store.put_float(fl)
            return self._json(fl)
        if len(p) >= 2 and p[0] == "floats":
            fid = p[1]
            with store.lock:
                fl = store.get_float(fid)
            if fl is None:
                raise KeyError("float not found")
            rest = p[2:]
            if not rest and method == "GET":
                return self._json(fl)
            if not rest and method == "PUT":
                if eng.applying == fid:
                    return self._err("Lights on this float are being changed right now. Try again in a moment.", 409)
                b = self._body()
                with store.lock:
                    if "rev" in b and b["rev"] != fl.get("rev", 0):
                        return self._err("This float was changed on another device. Reloading it now.", 409)
                    b["id"] = fid
                    ids = {fx.get("id") for fx in b.get("fixtures", [])}
                    b["live"] = {k: v for k, v in fl.get("live", {}).items() if k in ids}
                    b["looks"] = fl.get("looks", [])
                    b["rev"] = fl.get("rev", 0) + 1
                    store.put_float(b)
                eng.refresh()
                return self._json(b)
            if not rest and method == "DELETE":
                if eng.active_float == fid:
                    eng.set_active(None)
                store.delete_float(fid)
                return self._json({"ok": True})
            if rest == ["live"] and method == "POST":
                eng.update_live(fid, self._body().get("changes", {}))
                return self._json({"ok": True})
            if rest == ["looks"] and method == "POST":
                b = self._body()
                with store.lock:
                    look = {"id": new_id("lk"), "name": (b.get("name") or "Look").strip()[:60],
                            "created": time.time(), "states": json.loads(json.dumps(fl["live"]))}
                    fl["looks"].append(look)
                    store.bump(fl)
                return self._json(look)
            if len(rest) == 3 and rest[0] == "looks" and rest[2] == "recall" and method == "POST":
                with store.lock:
                    look = next(l for l in fl["looks"] if l["id"] == rest[1])
                    fl["live"] = json.loads(json.dumps(look["states"]))
                    store.mark_dirty(fl)
                eng.refresh()
                return self._json({"ok": True})
            if len(rest) == 2 and rest[0] == "looks" and method == "DELETE":
                with store.lock:
                    fl["looks"] = [l for l in fl["looks"] if l["id"] != rest[1]]
                    store.bump(fl)
                return self._json({"ok": True})
            if len(rest) == 2 and rest[0] == "looks" and method == "PUT":
                b = self._body()
                with store.lock:
                    look = next(l for l in fl["looks"] if l["id"] == rest[1])
                    if "name" in b:
                        look["name"] = str(b["name"]).strip()[:60] or look["name"]
                    if b.get("overwrite"):
                        look["states"] = json.loads(json.dumps(fl["live"]))
                    store.mark_dirty(fl)
                return self._json(look)
            if rest == ["flash"] and method == "POST":
                eng.flash_fixture(self._body()["fixture_id"], float(self._body().get("seconds", 6)))
                return self._json({"ok": True})
            if rest == ["scan"] and method == "POST":
                b = self._body()
                return self._json(app.scan(fl, b["box_id"], bool(b.get("flush"))))
            if rest == ["box"] and method == "POST":
                return self._box_action(fl, self._body())
            if rest == ["rdm"] and method == "POST":
                return self._rdm_action(fl, self._body())
            if rest == ["sweep"] and method == "POST":
                b = self._body()
                if b.get("action") in (None, "start"):
                    return self._json(eng.start_sweep(fid, b["box_id"], b.get("variant", "TW"), int(b.get("mode", 11)),
                                                      int(b.get("start", 1)), int(b.get("count", 24)),
                                                      float(b.get("seconds", 2.5))))
                eng.sweep_control(b["action"])
                return self._json({"engine": eng.status()})
            if rest == ["autopatch"] and method == "POST":
                b = self._body()
                res = autopatch(store, fl, b.get("box_id"), int(b.get("start", 1)),
                                b.get("fixture_ids"), int(b.get("gap", 0)))
                eng.refresh()
                return self._json(res)
            if rest == ["save_rdm"] and method == "POST":
                return self._json(self._save_rdm(fl, self._body().get("box_ids")))
            if rest == ["apply"] and method == "POST":
                return self._json(self._apply_to_lights(fl, self._body()))
            if rest == ["save"] and method == "POST":
                b = self._body()
                for box in fl["boxes"]:  # a box on "Play saved look" ignores the app: saving would do nothing
                    if box.get("ip") and not b.get("verify"):
                        reap = Reap(box["ip"])
                        if not reap.available():
                            if not self.app.artnet_alive(box["ip"], int(box.get("udp_port") or 6454)):
                                raise RuntimeError("%s isn't answering. Check its cable and power, then try again." % box["name"])
                            continue  # answers Art-Net but has no web page (other gear, simulator)
                        if reap.other_settings().get("ic_od") == "disabled":
                            raise RuntimeError("%s is on 'Play saved look'. Switch it back to app control first." % box["name"])
                return self._json(eng.start_save(fid, b.get("fixture_ids", []), bool(b.get("verify"))))
        raise KeyError("no such endpoint")

    def _save_rdm(self, fl, box_ids=None):
        """Tell every fixture on the float's boxes, over RDM, to keep what it is showing now as its
        power-on look (Anolis manufacturer parameter "Init position LEDs" = 1, found by its name).
        No prior Scan or linking needed: each box's device list is read fresh."""
        app = self.app
        eng = app.engine
        with eng.lock:
            if eng.hold:
                raise RuntimeError("Output is held dark because the last address or mode change didn't finish. Press Clear hold (top right), then try again.")
            if eng.job and eng.job.get("state") == "running":
                raise RuntimeError("A save is running. Wait for it to finish.")
            if eng.active_float != fl["id"] or not eng.ctl.output_enabled:
                raise RuntimeError("Go live on this float first.")
            # Never save a dark look.
            eng.blackout = False
            eng.sweep = None
            eng.flash.clear()
        eng.refresh()
        time.sleep(0.6)
        boxes = [b for b in fl["boxes"] if b.get("ip") and (not box_ids or b["id"] in box_ids)]
        if not boxes:
            raise RuntimeError("Set the box IP first (Patch tab).")
        labels = {fx.get("uid"): fx["label"] for fx in fl["fixtures"] if fx.get("uid")}
        results = []
        # Output stays on (no RDM pause): the fixtures must be showing the look while they save it.
        with app.scan_lock:
            for b in boxes:
                ip, port, pa = app.box_target(fl, b["id"])
                try:
                    uids = self.app.ctl.tod(ip, port, pa)
                except RdmError as e:
                    results.append({"box": b["name"], "label": b["name"], "ok": False, "why": str(e)})
                    continue
                if not uids:
                    results.append({"box": b["name"], "label": b["name"], "ok": False, "why": "no fixtures reported"})
                for uid in uids:
                    us = rdm.uid_str(uid)
                    r = {"box": b["name"], "uid": us, "label": labels.get(us, us)}
                    try:
                        params = app.mfr_params(ip, port, pa, uid, with_values=False)
                        d = app.find_save_param(params)
                        if not d:
                            r.update(ok=False, why="no 'Init position' setting")
                        else:
                            app.ctl.set_param(ip, port, pa, uid, d["pid"], 1, d["size"] if d["size"] in (1, 2, 4) else 1)
                            r.update(ok=True, param=d["description"])
                    except (RdmError, ValueError) as e:
                        r.update(ok=False, why=str(e))
                    results.append(r)
        return {"results": results, "saved": sum(1 for r in results if r.get("ok"))}

    def _palette(self, method, rest):
        """Saved colors, shared by every float."""
        store = self.app.store
        with store.lock:
            pal = store.data.setdefault("palette", [])
            if not rest and method == "GET":
                return self._json(pal)
            if not rest and method == "POST":
                b = self._body()
                st = b.get("state") or {}
                keep = {k: st[k] for k in ("kind", "cct", "hue", "sat", "white", "dim") if k in st}
                if b.get("include_dim") is False:
                    keep.pop("dim", None)
                entry = {"id": new_id("col"), "name": (b.get("name") or "Color").strip()[:40],
                         "state": fixtures.normalize_state(keep, "RGBW" if keep.get("kind") == "color" else "TW"),
                         "include_dim": "dim" in keep, "created": time.time()}
                if not entry["include_dim"]:
                    entry["state"].pop("dim", None)
                pal.append(entry)
                store.bump()
                return self._json(entry)
            if len(rest) == 1:
                entry = next((c for c in pal if c["id"] == rest[0]), None)
                if entry is None:
                    raise KeyError("color not found")
                if method == "DELETE":
                    store.data["palette"] = [c for c in pal if c["id"] != rest[0]]
                    store.bump()
                    return self._json({"ok": True})
                if method == "PUT":
                    b = self._body()
                    if b.get("name"):
                        entry["name"] = str(b["name"]).strip()[:40]
                    store.mark_dirty()
                    return self._json(entry)
        raise KeyError("no such endpoint")

    def _reap_action(self, fl, b, ip, dev):
        """Same actions as _rdm_action, through the box's web page."""
        app = self.app
        reap = Reap(ip)
        act = b["action"]
        d_uid = reap_uid(b["uid"])
        if act in ("address", "mode", "label", "set_param"):
            # Every box write resends address, mode and label together: use the light's current
            # values, not what we saw at the last Scan (someone may have changed it since).
            fresh = {reap_uid_str(d["d_uid"]): d for d in reap.discover(timeout=20)}
            app.reap_devices.setdefault(ip, {}).update(fresh)
            if b["uid"] not in fresh:
                raise ValueError("That light didn't answer the box's search. Try again.")
            dev = fresh[b["uid"]]
        label = (dev.get("u_l") or "").strip()
        addr, mode = int(dev.get("dmx_a", 0)) + 1, int(dev.get("dmx_p") or 1)
        with app.rdm_session():
            if act == "identify":
                on = bool(b.get("on", True))
                reap.identify(d_uid, on)
                key = (ip, d_uid)
                t = app._identify_timers.pop(key, None)
                if t:
                    t.cancel()
                if on:
                    def off():
                        try:
                            with app.scan_lock:
                                reap.identify(d_uid, False)
                        except ReapError:
                            pass
                    t = threading.Timer(15.0, off)
                    t.daemon = True
                    t.start()
                    app._identify_timers[key] = t
            elif act == "address":
                addr = int(b["address"])
                reap.setup(d_uid, label, addr, mode, dev)
                dev["dmx_a"] = addr - 1
                self._write_back(fl, b["uid"], address=addr)
            elif act == "mode":
                mode = int(b["mode"])
                reap.setup(d_uid, label, addr, mode, dev)
                dev["dmx_p"] = mode
                self._write_back(fl, b["uid"], mode=mode)
            elif act == "label":
                label = str(b["label"])[:32]
                reap.setup(d_uid, label, addr, mode, dev)
                dev["u_l"] = label
            elif act == "params":
                params = []
                if int(dev.get("sp") or 0) & 1:
                    params.append({"pid": 1, "pid_hex": "box", "description": "DMX terminator (last light on a cable)",
                                   "size": 1, "data_type": "uint8", "command_class": "GET_SET", "can_get": True,
                                   "can_set": True, "min": 0, "max": 1, "default": 0,
                                   "value": 1 if dev.get("dmx_t") == "on" else 0})
                return self._json({"ok": True, "uid": b["uid"], "params": params, "save_pid": None})
            elif act == "set_param":
                if int(b["pid"]) != 1:
                    raise ValueError("That setting isn't available through this box.")
                on = bool(int(b["value"]))
                reap.setup(d_uid, label, addr, mode, dev, terminator=on)
                dev["dmx_t"] = "on" if on else "off"
            elif act != "info":
                raise ValueError("unknown RDM action")
        app.engine.refresh()
        app.store.flush()
        info = {"address": int(dev.get("dmx_a", 0)) + 1, "mode": int(dev.get("dmx_p") or 1),
                "personality": int(dev.get("dmx_p") or 1)}
        return self._json({"ok": True, "uid": b["uid"], "info": info, "rev": app.store.data.get("rev", 0)})

    def _apply_to_lights(self, fl, b):
        """Write modes/addresses to the real lights, safely, from the Mac.

        body: {"to_mode7": [fixture ids]}  or  {"push": true} (write the patch as it stands).
        Output goes live on this float and is held at zero first, so no light ever reads a stale
        byte on its save channel while layouts change. The store only records a light's new
        mode/address after that light confirms it. On any failure the hold stays on."""
        app, eng, store = self.app, self.app.engine, self.app.store
        with store.lock:
            fxs = [fx for fx in fl["fixtures"] if fx.get("uid") and fx.get("address")]
            if b.get("to_mode7"):
                ids = set(b["to_mode7"])
                chosen = [fx for fx in fxs if fx["id"] in ids]
                targets = {}
                for box_id in {fx["box_id"] for fx in chosen}:
                    plan = plan_addresses(fl, box_id, 1, [fx["id"] for fx in chosen if fx["box_id"] == box_id],
                                          modes={fx["id"]: fixtures.SAVE_MODE for fx in chosen})
                    targets.update({fid: (addr, fixtures.SAVE_MODE) for fid, addr in plan.items()})
            else:
                targets = {fx["id"]: (fx["address"], fx["mode"]) for fx in fxs}
            by_id = {fx["id"]: fx for fx in fl["fixtures"]}
        if not targets:
            raise ValueError("No lights to change. Scan the box first so the app knows which light is which.")
        if not app.scan_lock.acquire(timeout=3):
            raise RuntimeError("Busy talking to the box. Try again in a moment.")
        if eng.applying or (eng.job and eng.job.get("state") == "running"):
            app.scan_lock.release()
            raise RuntimeError("Busy. Try again in a moment.")
        done, errors = [], []
        try:
            eng.applying = fl["id"]
            eng.hold = True                               # zeros from the very first frame...
            eng.set_active(fl["id"], output=True, keep_hold=True)   # ...then go live (still held)
            time.sleep(0.4)  # let a few all-zero frames reach the box
            for box in fl["boxes"]:
                mine = [fid for fid in targets if by_id[fid].get("box_id") == box["id"]]
                if not mine or not box.get("ip"):
                    continue
                reap = Reap(box["ip"])
                if not reap.available():
                    errors.append("%s: the box's web page didn't answer." % box["name"])
                    continue
                cache = app.reap_devices.setdefault(box["ip"], {})
                want = {by_id[fid]["uid"] for fid in mine}
                try:
                    for _ in range(2):  # the box's search sometimes misses a light
                        cache.update({reap_uid_str(d["d_uid"]): d for d in reap.discover(timeout=20)})
                        if want <= set(cache):
                            break
                except ReapError as e:
                    errors.append("%s: %s" % (box["name"], e))
                    continue
                failed, box_gone = [], False
                for n, fid in enumerate(mine):
                    fx = by_id[fid]
                    addr, mode = targets[fid]
                    dev = cache.get(fx["uid"])
                    if dev is None:
                        errors.append("%s: didn't answer the box's search." % fx["label"])
                        continue
                    if int(dev.get("dmx_a", -1)) + 1 == addr and int(dev.get("dmx_p") or 0) == mode:
                        self._write_back(fl, fx["uid"], address=addr, mode=mode)
                        done.append(fx["label"])
                        continue
                    try:
                        reap.setup(reap_uid(fx["uid"]), (dev.get("u_l") or "").strip(), addr, mode, dev)
                    except ReapError as e:
                        errors.append("%s: %s" % (fx["label"], e))
                        failed.append(fx)
                        if "Can't reach" in str(e):  # the box is gone: don't wait on every remaining light
                            box_gone = True
                            for rest_fid in mine[n + 1:]:
                                errors.append("%s: skipped, the box stopped answering." % by_id[rest_fid]["label"])
                                failed.append(by_id[rest_fid])
                            break
                        continue
                    dev["dmx_a"], dev["dmx_p"] = addr - 1, mode
                    self._write_back(fl, fx["uid"], address=addr, mode=mode)
                    done.append(fx["label"])
                if failed:
                    # A light whose change wasn't confirmed may or may not have switched. Ask the box
                    # what it really is now; if it can't say, forget that light's address so the app
                    # never drives it with a guessed layout (it shows as unaddressed until re-scanned).
                    actual = {}
                    try:
                        if not box_gone and reap.available():
                            actual = {reap_uid_str(d["d_uid"]): d for d in reap.discover(timeout=15)}
                            cache.update(actual)
                    except ReapError:
                        actual = {}
                    for fx in failed:
                        d = actual.get(fx["uid"])
                        if d is not None:
                            self._write_back(fl, fx["uid"], address=int(d.get("dmx_a", 0)) + 1, mode=int(d.get("dmx_p") or 1))
                        else:
                            self._write_back(fl, fx["uid"], address=None)
        finally:
            eng.applying = None
            app.scan_lock.release()
            store.flush()  # the lights already changed: get it on disk now, not up to a second later
        if not errors:
            eng.set_hold(False)
        return {"ok": not errors, "done": done, "errors": errors, "held": bool(errors)}

    def _settings_view(self):
        st = self.app.store
        mp = st.mirror_path()
        return {"pause_during_rdm": self.app.pause_during_rdm, "data_file": str(st.path),
                "backups_dir": str(st.dir / "backups"), "last_saved": st.last_saved,
                "mirror_dir": st.settings.get("mirror_dir"), "mirror_file": str(mp) if mp else None,
                "last_mirrored": st.last_mirrored, "mirror_error": st.mirror_error}

    def _box_action(self, fl, b):
        """Box-wide settings through its web page: read them, or switch Output data."""
        ip, _, _ = self.app.box_target(fl, b["box_id"])
        reap = Reap(ip)
        if b.get("action") == "output_data":
            self.app.engine.release()
            reap.set_other(output_data=bool(b["enabled"]))
            reap.restart()
            return self._json({"ok": True, "restarting": True})
        s = reap.other_settings()
        return self._json({"ok": True, "output_data": s.get("ic_od"), "mode": s.get("ebm"), "dmx_hold": s.get("dmxh")})

    def _rdm_action(self, fl, b):
        app = self.app
        ip, port, pa = app.box_target(fl, b["box_id"])
        dev = app.reap_devices.get(ip, {}).get(b["uid"])
        if dev is None:
            reap = Reap(ip)
            if reap.available():
                # Not known yet (app restarted, or the box's search missed it last time): search again.
                with app.rdm_session():
                    devs = reap.discover()
                app.reap_devices.setdefault(ip, {}).update({reap_uid_str(d["d_uid"]): d for d in devs})
                dev = app.reap_devices[ip].get(b["uid"])
                if dev is None:
                    raise ValueError("That light didn't answer the box's search. Try again in a moment.")
        if dev is not None:
            return self._reap_action(fl, b, ip, dev)
        uid = rdm.uid_from_str(b["uid"])
        act = b["action"]
        with app.rdm_session():
            if act == "identify":
                app.identify(ip, port, pa, uid, bool(b.get("on", True)))
            elif act == "address":
                want = int(b["address"])
                try:
                    app.ctl.set_address(ip, port, pa, uid, want)
                except RdmError:
                    if app.ctl.device_info(ip, port, pa, uid).get("address") != want:
                        raise
                self._write_back(fl, b["uid"], address=want)
            elif act == "mode":
                want = int(b["mode"])
                n = app.personality_for_mode(ip, port, pa, uid, want)
                try:
                    app.ctl.set_personality(ip, port, pa, uid, n)
                except RdmError:
                    if app.ctl.device_info(ip, port, pa, uid).get("personality") != n:
                        raise
                self._write_back(fl, b["uid"], mode=want)
            elif act == "label":
                app.ctl.set_label(ip, port, pa, uid, str(b["label"]))
            elif act == "info":
                pass
            elif act == "params":
                params = app.mfr_params(ip, port, pa, uid)
                save = app.find_save_param(params)
                return self._json({"ok": True, "uid": b["uid"], "params": params,
                                   "save_pid": save["pid"] if save else None})
            elif act == "set_param":
                params = app.mfr_params(ip, port, pa, uid, with_values=False)
                d = next((x for x in params if x["pid"] == int(b["pid"])), None)
                size = d["size"] if d and d.get("size") in (1, 2, 4) else int(b.get("size", 1))
                app.ctl.set_param(ip, port, pa, uid, int(b["pid"]), int(b["value"]), size,
                                  bool(d and d["data_type"].startswith("int")))
            else:
                raise ValueError("unknown RDM action")
            info = app.ctl.device_info(ip, port, pa, uid)
            pmap = app.personality_map(ip, port, pa, uid, info)
            info["mode"] = (pmap.get(info["personality"]) or {}).get("mode") or info["personality"]
        app.engine.refresh()
        return self._json({"ok": True, "uid": b["uid"], "info": info, "rev": app.store.data.get("rev", 0)})

    def _write_back(self, fl, uid_s, **fields):
        """Keep the patch in step with the hardware the moment an RDM change succeeds."""
        store = self.app.store
        with store.lock:
            fl = store.get_float(fl["id"]) or fl
            for fx in fl["fixtures"]:
                if fx.get("uid") == uid_s:
                    if "mode" in fields:
                        if fields["mode"] in fixtures.MODES.get(fx["variant"], {}) or fields["mode"] == fixtures.SAVE_MODE:
                            fx["mode"] = fields["mode"]
                    if "address" in fields:
                        fx["address"] = fields["address"]  # None = unknown: the light isn't driven until re-scanned
            fl["rev"] = fl.get("rev", 0) + 1
            store.bump(fl)

    def _patch_sheet(self, fid):
        from .patchsheet import render_patch_sheet
        with self.app.store.lock:
            fl = self.app.store.get_float(fid)
            html = render_patch_sheet(self.app.store.data.get("project", ""), fl,
                                      (self.app.store.data.get("white_cal") or {}).get("RGBW")) if fl else None
        if html is None:
            return self.send_error(404)
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def plan_addresses(fl, box_id, start=1, fixture_ids=None, gap=0, modes=None):
    """Pure: {fixture id: address} giving the chosen fixtures consecutive blocks (by footprint,
    list order), skipping channels used by other fixtures on the same box. `modes` overrides
    fixture modes for the plan (e.g. {id: 7}) without touching the store."""
    from .store import fixture_footprint
    fake = {"fixtures": [dict(fx, mode=(modes or {}).get(fx["id"], fx["mode"])) for fx in fl["fixtures"]]}
    return {c["id"]: c["address"] for c in _autopatch_core(fake, box_id, start, fixture_ids, gap)}


def autopatch(store, fl, box_id, start, fixture_ids=None, gap=0):
    with store.lock:
        changed = _autopatch_core(fl, box_id, start, fixture_ids, gap)
        fl["rev"] = fl.get("rev", 0) + 1
        store.bump(fl)
    return {"changed": changed}


def _autopatch_core(fl, box_id, start, fixture_ids=None, gap=0):
    """Give the chosen fixtures consecutive blocks by footprint, in list order, skipping over
    channels used by fixtures on the same box that are not being re-addressed."""
    from .store import fixture_footprint
    if True:
        chosen = [fx for fx in fl["fixtures"]
                  if (not box_id or fx.get("box_id") == box_id) and (not fixture_ids or fx["id"] in fixture_ids)]
        chosen_ids = {fx["id"] for fx in chosen}
        taken = []
        for fx in fl["fixtures"]:
            if fx["id"] in chosen_ids or not fx.get("address"):
                continue
            if chosen and fx.get("box_id") != chosen[0].get("box_id") and box_id:
                continue
            if box_id and fx.get("box_id") != box_id:
                continue
            taken.append((fx["address"], fx["address"] + fixture_footprint(fx) - 1))
        addr = max(1, start)
        changed = []
        for fx in chosen:
            fp = fixture_footprint(fx)
            moved = True
            while moved:
                moved = False
                for a0, a1 in taken:
                    if addr <= a1 and addr + fp - 1 >= a0:
                        addr = a1 + 1
                        moved = True
            if addr + fp - 1 > 512:
                raise ValueError("Ran out of channels at %s" % fx["label"])
            fx["address"] = addr
            changed.append({"id": fx["id"], "address": addr})
            addr += fp + max(0, gap)
    return changed


def make_server(app: App, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server_cls = type("FieldServer", (ThreadingHTTPServer,), {"request_queue_size": 64})
    if platform.system() == "Windows":  # don't let a second copy share the web port silently
        server_cls = type("ExclusiveServer", (server_cls,), {"allow_reuse_address": False})
    srv = server_cls((host, port), handler)
    srv.daemon_threads = True
    return srv
