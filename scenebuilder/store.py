"""Persistent project data: floats, boxes, fixtures, looks. JSON on disk with rolling backups."""
from __future__ import annotations

import copy
import json
import os
import platform
import socket
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
        self.settings_path = self.dir / "settings.json"
        self.lock = threading.RLock()
        self._dirty = False
        self._last_backup = 0.0
        self._last_mirror = 0.0
        self._mirror_pending = False
        self.mirror_error: Optional[str] = None
        self.last_saved = 0.0
        self.last_mirrored = 0.0
        # Per-computer settings (never exported with the project): pinned adapter, backup folder.
        self.settings = {"network_interface": None, "mirror_dir": None}
        if self.settings_path.exists():
            try:
                with open(self.settings_path) as f:
                    self.settings.update(json.load(f))
            except (OSError, ValueError):
                pass
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
        if "network_interface" in self.data:  # moved to per-computer settings
            legacy = self.data.pop("network_interface")
            if legacy and not self.settings.get("network_interface"):
                self.settings["network_interface"] = legacy
                try:
                    _atomic_write(self.settings_path, json.dumps(self.settings, indent=1))
                except OSError:
                    pass
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
    def touch(self, fl: Optional[dict]):
        """Stamp a float as edited now, on this computer. Import uses it to tell which copy is newer."""
        if fl is not None:
            fl["updated"] = time.time()
            fl["edited_on"] = HOSTNAME

    def bump(self, fl: Optional[dict] = None):
        """Structural change (patch, looks, palette): other devices reload when this changes."""
        with self.lock:
            self.data["rev"] = int(self.data.get("rev", 0)) + 1
            self.mark_dirty(fl)

    def mark_dirty(self, fl: Optional[dict] = None):
        with self.lock:
            self.touch(fl)
            self.data["updated"] = time.time()
            self._dirty = True

    def save_settings(self, **changes):
        with self.lock:
            self.settings.update(changes)
            _atomic_write(self.settings_path, json.dumps(self.settings, indent=1))
            if "mirror_dir" in changes:
                self._last_mirror = 0.0
                self._mirror_pending = True

    def mirror_path(self) -> Optional[Path]:
        d = self.settings.get("mirror_dir")
        return Path(d) / ("DMX Scene Builder - %s.json" % HOSTNAME) if d else None

    def flush(self):
        with self.lock:
            if not self._dirty and not self._mirror_pending:
                return
            snapshot = json.dumps(self.data, indent=1)
            wrote_main = self._dirty
            self._dirty = False
        if wrote_main:
            _atomic_write(self.path, snapshot)  # temp file + fsync + rename: a crash can't leave half a file
            self.last_saved = time.time()
            self._mirror_pending = True
        self._mirror(snapshot)
        if not wrote_main:
            return
        now = time.time()
        if now - self._last_backup > 60:  # at most one backup a minute, keep 100
            self._last_backup = now
            stamp = time.strftime("%Y%m%d-%H%M%S")
            with open(self.dir / "backups" / ("project-%s.json" % stamp), "w") as f:
                f.write(snapshot)
            backups = sorted((self.dir / "backups").glob("project-*.json"))
            for old in backups[:-100]:
                old.unlink()

    def _mirror(self, snapshot: str, force: bool = False):
        """Copy the project to the backup folder (e.g. Dropbox) at most every 30 s."""
        target = self.mirror_path()
        if not target or not self._mirror_pending:
            return
        if not force and time.time() - self._last_mirror < 30:
            return
        try:
            if not target.parent.is_dir():
                raise OSError("folder not found: %s" % target.parent)
            _atomic_write(target, snapshot)
            self._last_mirror = self.last_mirrored = time.time()
            self._mirror_pending = False
            self.mirror_error = None
        except OSError as e:
            self._last_mirror = time.time()  # retry in 30 s, don't spin
            self.mirror_error = str(e)

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
        with self.lock:
            snapshot = json.dumps(self.data, indent=1)
        self._mirror(snapshot, force=True)

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
        self.touch(fl)
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

    # ---------------------------------------------------------- merge import
    def plan_merge(self, incoming: dict) -> dict:
        """Compare another computer's project file against this one, float by float.
        Never mutates anything: safe to call just to preview. A float only one side has is
        never touched by the merge, so combining two computers' work never deletes anything."""
        if not isinstance(incoming, dict) or not isinstance(incoming.get("floats"), list):
            raise ValueError("Not a DMX Scene Builder project file")
        with self.lock:
            mine_by_id = {f["id"]: f for f in self.data["floats"]}
        new, identical, conflicts = [], [], []
        for fl in incoming["floats"]:
            fid = fl.get("id")
            if not fid:
                continue
            summary = _float_summary(fl)
            mine = mine_by_id.get(fid)
            if mine is None:
                new.append(summary)
            elif _floats_equal(mine, fl):
                identical.append(summary)
            else:
                conflicts.append({"id": fid, "code": fl.get("code", ""), "name": fl.get("name", ""),
                                  "mine": _float_summary(mine), "theirs": summary})
        return {"new": new, "identical": identical, "conflicts": conflicts}

    def apply_merge(self, incoming: dict, resolutions: dict) -> dict:
        """Add floats the other computer has that this one doesn't, and replace only the
        floats you explicitly chose "theirs" for. Everything else on this computer, including
        every float not mentioned in `incoming`, is left exactly as it is."""
        if not isinstance(incoming, dict) or not isinstance(incoming.get("floats"), list):
            raise ValueError("Not a DMX Scene Builder project file")
        added, replaced = 0, 0
        with self.lock:
            mine_by_id = {f["id"]: f for f in self.data["floats"]}
            incoming.setdefault("palette", [])
            for fl in incoming["floats"]:
                fid = fl.get("id")
                if not fid:
                    continue
                for key in ("boxes", "fixtures", "looks"):
                    fl.setdefault(key, [])
                if fid not in mine_by_id:
                    validate_float(fl)
                    self.data["floats"].append(fl)
                    added += 1
                elif resolutions.get(fid) == "theirs" and not _floats_equal(mine_by_id[fid], fl):
                    validate_float(fl)
                    for i, old in enumerate(self.data["floats"]):
                        if old["id"] == fid:
                            self.data["floats"][i] = fl
                            break
                    replaced += 1
            existing_names = {c["name"] for c in self.data.get("palette", [])}
            for c in incoming.get("palette", []):
                if c.get("name") not in existing_names:
                    self.data.setdefault("palette", []).append(c)
            if added or replaced:
                self.bump()
        return {"added": added, "replaced": replaced}


def _float_summary(fl: dict) -> dict:
    return {"id": fl.get("id"), "code": fl.get("code", ""), "name": fl.get("name", ""),
            "fixtures": len(fl.get("fixtures") or []), "boxes": len(fl.get("boxes") or []),
            "looks": len(fl.get("looks") or []), "updated": fl.get("updated") or 0,
            "edited_on": fl.get("edited_on") or ""}


def _atomic_write(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


HOSTNAME = socket.gethostname().split(".")[0]


def _floats_equal(a: dict, b: dict) -> bool:
    strip = lambda f: {k: v for k, v in f.items() if k not in ("rev", "updated", "edited_on")}
    return json.dumps(strip(a), sort_keys=True) == json.dumps(strip(b), sort_keys=True)


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
