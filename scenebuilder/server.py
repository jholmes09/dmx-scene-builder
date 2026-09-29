"""HTTP server: static web app + JSON API."""
from __future__ import annotations

import json
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
                app.scan_lock.acquire()
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
            with store.lock:
                store.data["network_interface"] = ip
                store.mark_dirty()
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
        if p == ["settings"] and method == "POST":
            b = self._body()
            if "pause_during_rdm" in b:
                app.pause_during_rdm = bool(b["pause_during_rdm"])
            return self._json({"pause_during_rdm": app.pause_during_rdm})
        if p == ["output"] and method == "POST":
            b = self._body()
            action = b.get("action")
            if action == "activate":
                eng.set_active(b.get("float_id"), output=True)
            elif action == "release":
                eng.release()
            elif action == "resume":
                eng.resume()
            elif action == "blackout":
                eng.set_blackout(b.get("on", True))
            elif action == "hold":
                eng.set_hold(bool(b.get("on", True)))
            else:
                raise ValueError("unknown action")
            return self._json({"engine": eng.status()})
        if p and p[0] == "palette":
            return self._palette(method, p[1:])
        if p == ["project"] and method == "GET":
            return self._json(store.snapshot())
        if p == ["project"] and method == "PUT":
            store.replace_project(self._body())
            eng.refresh()
            return self._json({"ok": True})
        if p == ["project", "merge_plan"] and method == "POST":
            return self._json(store.plan_merge(self._body()))
        if p == ["project", "merge_apply"] and method == "POST":
            b = self._body()
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
                    store.bump()
                return self._json(look)
            if len(rest) == 3 and rest[0] == "looks" and rest[2] == "recall" and method == "POST":
                with store.lock:
                    look = next(l for l in fl["looks"] if l["id"] == rest[1])
                    fl["live"] = json.loads(json.dumps(look["states"]))
                    store.mark_dirty()
                eng.refresh()
                return self._json({"ok": True})
            if len(rest) == 2 and rest[0] == "looks" and method == "DELETE":
                with store.lock:
                    fl["looks"] = [l for l in fl["looks"] if l["id"] != rest[1]]
                    store.bump()
                return self._json({"ok": True})
            if len(rest) == 2 and rest[0] == "looks" and method == "PUT":
                b = self._body()
                with store.lock:
                    look = next(l for l in fl["looks"] if l["id"] == rest[1])
                    if "name" in b:
                        look["name"] = str(b["name"]).strip()[:60] or look["name"]
                    if b.get("overwrite"):
                        look["states"] = json.loads(json.dumps(fl["live"]))
                    store.mark_dirty()
                return self._json(look)
            if rest == ["flash"] and method == "POST":
                eng.flash_fixture(self._body()["fixture_id"], float(self._body().get("seconds", 6)))
                return self._json({"ok": True})
            if rest == ["scan"] and method == "POST":
                b = self._body()
                return self._json(app.scan(fl, b["box_id"], bool(b.get("flush"))))
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
            if rest == ["save"] and method == "POST":
                b = self._body()
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
                raise RuntimeError("Fixtures are being re-addressed. Wait for that to finish.")
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

    def _rdm_action(self, fl, b):
        app = self.app
        ip, port, pa = app.box_target(fl, b["box_id"])
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
            for fx in fl["fixtures"]:
                if fx.get("uid") == uid_s:
                    if "mode" in fields:
                        if fields["mode"] in fixtures.MODES.get(fx["variant"], {}) or fields["mode"] == fixtures.SAVE_MODE:
                            fx["mode"] = fields["mode"]
                    if "address" in fields:
                        fx["address"] = fields["address"]
            fl["rev"] = fl.get("rev", 0) + 1
            store.bump()

    def _patch_sheet(self, fid):
        from .patchsheet import render_patch_sheet
        with self.app.store.lock:
            fl = self.app.store.get_float(fid)
            html = render_patch_sheet(self.app.store.data.get("project", ""), fl) if fl else None
        if html is None:
            return self.send_error(404)
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def autopatch(store, fl, box_id, start, fixture_ids=None, gap=0):
    """Give the chosen fixtures consecutive blocks by footprint, in list order, skipping over
    channels used by fixtures on the same box that are not being re-addressed."""
    from .store import fixture_footprint
    with store.lock:
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
        fl["rev"] = fl.get("rev", 0) + 1
        store.bump()
    return {"changed": changed}


def make_server(app: App, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv
