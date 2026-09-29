"""Persistent project data: floats, boxes, fixtures, looks. JSON on disk with rolling backups."""
from __future__ import annotations

import copy
import json
import os
import platform
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import fixtures


def _default_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "DMX Scene Builder"
    if system == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "DMX Scene Builder"
    return Path.home() / ".dmx-scene-builder"  # Linux and anything else


DEFAULT_DIR = _default_dir()
SCHEMA = 1


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])


def empty_project() -> dict:
    return {"schema": SCHEMA, "project": "Untitled", "floats": [], "updated": time.time()}


def new_box(name="E-Box 1", ip="", udp_port=6454, net=0, subnet=0, universe=0) -> dict:
    return {"id": new_id("box"), "name": name, "ip": ip, "udp_port": udp_port, "net": net,
            "subnet": subnet, "universe": universe, "notes": ""}


def new_fixture(label="", variant="TW", mode=None, box_id=None, address=None, type_id="", notes="") -> dict:
    return {"id": new_id("fx"), "label": label, "type_id": type_id, "variant": variant,
            "mode": mode or fixtures.DEFAULT_MODE[variant], "box_id": box_id, "address": address,
            "uid": None, "notes": notes}


def new_float(code="", name="New float") -> dict:
    box = new_box()
    return {"id": new_id("fl"), "code": code, "name": name, "notes": "", "boxes": [box],
            "fixtures": [], "looks": [], "live": {}, "run_mode": "standalone"}


class Store:
    def __init__(self, directory: Optional[Path] = None):
        self.dir = Path(directory or DEFAULT_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "backups").mkdir(exist_ok=True)
        self.path = self.dir / "project.json"
        self.lock = threading.RLock()
        self._dirty = False
        self._last_backup = 0.0
        if self.path.exists():
            with open(self.path) as f:
                self.data = json.load(f)
        else:
            self.data = empty_project()
        self._migrate()
        self._stop = threading.Event()
        threading.Thread(target=self._saver, name="store-saver", daemon=True).start()

    def _migrate(self):
        self.data.setdefault("schema", SCHEMA)
        self.data.setdefault("floats", [])
        self.data.setdefault("network_interface", None)  # pinned adapter IP, or None for automatic
        self.data.setdefault("palette", [])
        for fl in self.data["floats"]:
            fl.setdefault("looks", [])
            fl.setdefault("live", {})
            fl.setdefault("boxes", [])
            fl.setdefault("fixtures", [])
            fl.setdefault("run_mode", "standalone")
            for b in fl["boxes"]:
                b.setdefault("udp_port", 6454)

    # ---------------------------------------------------------- persistence
    def bump(self):
        """Structural change (patch, looks, palette): other devices reload when this changes."""
        with self.lock:
            self.data["rev"] = int(self.data.get("rev", 0)) + 1
            self.mark_dirty()

    def mark_dirty(self):
        with self.lock:
            self.data["updated"] = time.time()
            self._dirty = True

    def flush(self):
        with self.lock:
            if not self._dirty:
                return
            snapshot = json.dumps(self.data, indent=1)
            self._dirty = False
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(snapshot)
        os.replace(tmp, self.path)
        now = time.time()
        if now - self._last_backup > 60:  # at most one backup a minute, keep 100
            self._last_backup = now
            stamp = time.strftime("%Y%m%d-%H%M%S")
            with open(self.dir / "backups" / ("project-%s.json" % stamp), "w") as f:
                f.write(snapshot)
            backups = sorted((self.dir / "backups").glob("project-*.json"))
            for old in backups[:-100]:
                old.unlink()

    def _saver(self):
        while not self._stop.wait(1.0):
            try:
                self.flush()
            except Exception:
                import logging
                logging.getLogger("scenebuilder.store").exception("save failed")

    def close(self):
        self._stop.set()
        self.mark_dirty()
        self.flush()

    # ---------------------------------------------------------- access
    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def get_float(self, fid: str) -> Optional[dict]:
        for fl in self.data["floats"]:
            if fl["id"] == fid:
                return fl
        return None

    def replace_project(self, data: dict):
        if not isinstance(data, dict) or not isinstance(data.get("floats"), list):
            raise ValueError("Not a DMX Scene Builder project file")
        data.setdefault("palette", [])
        for fl in data["floats"]:
            for key in ("boxes", "fixtures", "looks"):
                fl.setdefault(key, [])
            validate_float(fl)  # raises with a clear message before anything is replaced
        with self.lock:
            rev = int(self.data.get("rev", 0))
            self.data = data
            self._migrate()
            self.data["rev"] = rev + 1
            self.mark_dirty()

    def put_float(self, fl: dict) -> dict:
        validate_float(fl)
        with self.lock:
            for i, old in enumerate(self.data["floats"]):
                if old["id"] == fl["id"]:
                    self.data["floats"][i] = fl
                    break
            else:
                self.data["floats"].append(fl)
            self.bump()
            return fl

    def delete_float(self, fid: str):
        with self.lock:
            self.data["floats"] = [f for f in self.data["floats"] if f["id"] != fid]
            self.bump()


def validate_float(fl: dict):
    if not isinstance(fl, dict) or not fl.get("id"):
        raise ValueError("float needs an id")
    for key in ("boxes", "fixtures", "looks"):
        if not isinstance(fl.get(key), list):
            raise ValueError("float.%s must be a list" % key)
    fl.setdefault("live", {})
    if fl.get("run_mode") not in ("standalone", "live_dmx"):
        fl["run_mode"] = "standalone"
    box_ids = {b["id"] for b in fl["boxes"]}
    for b in fl["boxes"]:
        for k, lo, hi in (("net", 0, 127), ("subnet", 0, 15), ("universe", 0, 15), ("udp_port", 1, 65535)):
            b[k] = int(b.get(k) or 0) if k != "udp_port" else int(b.get(k) or 6454)
            if not lo <= b[k] <= hi:
                raise ValueError("%s: %s must be %d-%d" % (b.get("name"), k, lo, hi))
        b["ip"] = (b.get("ip") or "").strip()
    for fx in fl["fixtures"]:
        if fx.get("variant") not in fixtures.MODES:
            raise ValueError("%s: unknown fixture type %r" % (fx.get("label"), fx.get("variant")))
        fx["mode"] = int(fx.get("mode") or fixtures.DEFAULT_MODE[fx["variant"]])
        if fx["mode"] not in fixtures.MODES[fx["variant"]] and fx["mode"] != fixtures.SAVE_MODE:
            raise ValueError("%s: mode %s isn't valid for %s" % (fx.get("label"), fx["mode"], fx["variant"]))
        if fx.get("address") in ("", None):
            fx["address"] = None
        else:
            fx["address"] = int(fx["address"])
            if not 1 <= fx["address"] <= 512:
                raise ValueError("%s: address must be 1-512" % fx.get("label"))
        if fx.get("box_id") not in box_ids:
            fx["box_id"] = fl["boxes"][0]["id"] if fl["boxes"] else None


def fixture_footprint(fx: dict) -> int:
    v = fx["variant"] if fx["mode"] in fixtures.MODES[fx["variant"]] else "RGBW"
    return fixtures.footprint(v, fx["mode"])


def patch_problems(fl: dict) -> list:
    """Overlaps, out-of-range footprints, missing addresses."""
    problems = []
    by_box = {}
    for fx in fl["fixtures"]:
        if fx.get("address") is None:
            problems.append({"fixture": fx["id"], "level": "warn", "text": "%s has no DMX address" % (fx["label"] or "Fixture")})
            continue
        end = fx["address"] + fixture_footprint(fx) - 1
        if end > 512:
            problems.append({"fixture": fx["id"], "level": "error", "text": "%s runs past channel 512" % fx["label"]})
        by_box.setdefault(fx.get("box_id"), []).append((fx["address"], end, fx))
    for items in by_box.values():
        items.sort(key=lambda t: t[0])
        for (a0, e0, f0), (a1, e1, f1) in zip(items, items[1:]):
            if a1 <= e0:
                for f in (f0, f1):
                    problems.append({"fixture": f["id"], "level": "error",
                                     "text": "%s and %s overlap (ch %d-%d vs %d-%d)" % (f0["label"] or "?", f1["label"] or "?", a0, e0, a1, e1)})
    return problems


def effective_variant(fx: dict) -> str:
    """Rendering variant: a TW module switched to Mode 7 renders with the RGBW Mode 7 layout."""
    return fx["variant"] if fx["mode"] in fixtures.MODES[fx["variant"]] else "RGBW"
