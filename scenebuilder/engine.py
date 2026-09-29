"""Engine: turns the active float's live look into Art-Net frames, and runs timed jobs
(flash-to-find, save-to-fixtures, show-saved)."""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from . import fixtures
from .node import ArtNetController
from .store import Store, effective_variant, fixture_footprint

SAVE_HOLD_S = 4.5    # manual: hold >= 3 s
SETTLE_S = 1.5


VCW_WHITES = {1800: 1, 2700: 3, 3200: 5, 4200: 7, 5600: 9, 6500: 11}  # Mode 7 virtual colour wheel presets


def white_test_bytes(method: int, k: int) -> bytes:
    """Mode 7 frame for comparing white methods on RGB + cool-white Calumma (dimmer ~60%)."""
    ctc = fixtures.kelvin_to_ctc(k)
    r = g = b = w = 0
    vcw = 0
    if method == 1:            # app's current method: all emitters full + CTC
        r = g = b = w = 255
    elif method == 2:          # fixture's built-in calibrated white preset (nearest listed K)
        vcw = VCW_WHITES[min(VCW_WHITES, key=lambda x: abs(x - k))]
        ctc = 0
    elif method == 3:          # cool white LED only
        w, ctc = 255, 0
    elif method == 4:          # all emitters full, no correction
        r = g = b = w = 255
        ctc = 0
    elif method == 5:          # white LED + CTC
        w = 255
    # special r rf g gf b bf w wf gc ctc vcw shutter dim dimf
    return bytes([0, r, 0, g, 0, b, 0, w, 0, 128, ctc, vcw, 255, 153, 0])


class Engine:
    def __init__(self, store: Store, ctl: ArtNetController):
        self.store = store
        self.ctl = ctl
        self.active_float: Optional[str] = None
        self.lock = threading.RLock()
        self.flash: Dict[str, float] = {}         # fixture id -> until (monotonic)
        self.special: Dict[str, int] = {}         # fixture id -> Special functions byte override
        self.dark: set = set()                    # fixture ids forced to dim 0 (verify step)
        self.sweep: Optional[dict] = None         # address-finder overlay
        self.white_test: Optional[dict] = None    # {"method": 1-5, "k": kelvin}: compare ways of making white
        self._paused_for_rdm = 0
        self._resume_after_rdm = False
        self.hold = False                         # send all-zero universes (fixtures being re-moded/re-addressed)
        self.blackout = False
        self.job: Optional[dict] = None
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="engine", daemon=True).start()

    # ------------------------------------------------------------ output control
    def set_active(self, fid: Optional[str], output: bool = True):
        with self.lock:
            self.active_float = fid
            self.flash.clear()
            self.special.clear()
            self.dark.clear()
            self.sweep = None
            self.hold = False
            self.white_test = None
            want = bool(fid) and output
            if self._paused_for_rdm:
                self._resume_after_rdm = want
            else:
                self.ctl.output_enabled = want
            if not fid:
                self.ctl.clear_targets()
        self.refresh()

    def release(self):
        """Stop sending entirely (the box falls back to DMX Hold or its stored look)."""
        with self.lock:
            self._resume_after_rdm = False
            self.hold = False
            self.white_test = None
            self.ctl.output_enabled = False
            self.ctl.clear_targets()
            if self.job and self.job.get("state") == "running":
                self.job["abort"] = "Release was pressed"

    def resume(self):
        with self.lock:
            if self.active_float:
                if self._paused_for_rdm:
                    self._resume_after_rdm = True
                else:
                    self.ctl.output_enabled = True
        self.refresh()

    def set_hold(self, on: bool):
        """All-zero output while fixtures change mode/address, so no stale byte lands on a Special channel."""
        with self.lock:
            self.hold = bool(on)
        self.refresh()

    def status(self) -> dict:
        with self.lock:
            return {"active_float": self.active_float, "output": self.ctl.output_enabled,
                    "blackout": self.blackout, "flashing": list(self.flash.keys()),
                    "job": dict(self.job) if self.job else None,
                    "sweep": {k: v for k, v in self.sweep.items() if k != "target"} if self.sweep else None,
                    "white_test": self.white_test,
                    "paused_for_rdm": self._paused_for_rdm > 0, "hold": self.hold,
                    "packets_sent": self.ctl.packets_sent, "packets_received": self.ctl.packets_received,
                    "bind_error": self.ctl.bind_error, "send_error": self.ctl.last_send_error,
                    "artnet_port": self.ctl.port}

    # ------------------------------------------------------------ rendering
    def frames(self) -> dict:
        with self.store.lock:
            fl = self.store.get_float(self.active_float) if self.active_float else None
            if not fl:
                return {}
            boxes = {b["id"]: b for b in fl["boxes"] if b.get("ip")}
            out = {}
            for b in boxes.values():
                out[(b["ip"], int(b.get("udp_port") or 6454),
                     (b["net"] << 8) | (b["subnet"] << 4) | b["universe"])] = bytearray(512)
            if self.hold:
                return {k: bytes(v) for k, v in out.items()}
            now = time.monotonic()
            specials = []  # (buffer, index, value): Mode 7 Special-functions bytes, written last
            for fx in fl["fixtures"]:
                b = boxes.get(fx.get("box_id"))
                if not b or not fx.get("address"):
                    continue
                key = (b["ip"], int(b.get("udp_port") or 6454), (b["net"] << 8) | (b["subnet"] << 4) | b["universe"])
                state = dict(fl["live"].get(fx["id"]) or fixtures.DEFAULT_STATE)
                if self.blackout or fx["id"] in self.dark or self.sweep:
                    state["dim"] = 0.0
                until = self.flash.get(fx["id"])
                if until:
                    if now > until:
                        self.flash.pop(fx["id"], None)
                    else:
                        state = {"dim": 1.0 if int(now * 3) % 2 == 0 else 0.0, "kind": "white", "cct": 4000}
                try:
                    data = fixtures.render(effective_variant(fx), fx["mode"], state, self.special.get(fx["id"], 0))
                except ValueError:
                    continue
                wt = self.white_test
                if wt and effective_variant(fx) == "RGBW" and fx["mode"] == fixtures.SAVE_MODE:
                    data = white_test_bytes(wt["method"], wt["k"])
                a = fx["address"] - 1
                buf = out[key]
                buf[a:a + len(data)] = data[:512 - a]
                roles = fixtures.mode_info(effective_variant(fx), fx["mode"])["roles"]
                if "special" in roles and a + roles.index("special") < 512:
                    specials.append((buf, a + roles.index("special"), self.special.get(fx["id"], 0)))
            sw = self.sweep
            if sw and sw.get("target") in out:
                a = sw["current"] - 1
                if 0 <= a < 512:
                    probe = {"dim": 1.0, "kind": "white", "cct": 4000}
                    data = fixtures.render(sw["variant"], sw["mode"], probe)
                    out[sw["target"]][a:a + len(data)] = data[:512 - a]
            # A neighbour's overlapping bytes must never land on a Mode 7 Special channel
            # (1-2 = save, 5-6 = factory demo at power-on).
            dark_all = self.blackout or bool(sw)
            for buf, i, v in specials:
                buf[i] = 0 if dark_all else v
            return {k: bytes(v) for k, v in out.items()}

    def refresh(self):
        self.ctl.set_targets(self.frames())
        if self.ctl.output_enabled:
            self.ctl.send_now()

    def _loop(self):
        import logging
        while not self._stop.wait(0.1):
            try:
                with self.lock:
                    need = bool(self.flash) or bool(self.special) or bool(self.sweep)
                if need and self.ctl.output_enabled:
                    self.ctl.set_targets(self.frames())
            except Exception:
                logging.getLogger("scenebuilder.engine").exception("engine loop")

    # ------------------------------------------------------------ live edits
    def update_live(self, fid: str, changes: Dict[str, dict]):
        with self.store.lock:
            fl = self.store.get_float(fid)
            if not fl:
                raise KeyError("float not found")
            ids = {fx["id"]: fx for fx in fl["fixtures"]}
            for fx_id, st in changes.items():
                if fx_id not in ids:
                    continue
                cur = dict(fl["live"].get(fx_id) or fixtures.DEFAULT_STATE)
                cur.update(st)
                fl["live"][fx_id] = fixtures.normalize_state(cur, effective_variant(ids[fx_id]))
            self.store.mark_dirty(fl)
        if fid == self.active_float:
            self.refresh()

    def flash_fixture(self, fx_id: str, seconds: float = 6.0):
        with self.lock:
            self.flash[fx_id] = time.monotonic() + seconds
        self.refresh()

    def set_white_test(self, method: Optional[int], k: int = 6500):
        with self.lock:
            if method and self.job and self.job.get("state") == "running":
                raise RuntimeError("A save is running. Wait for it to finish.")
            self.white_test = {"method": int(method), "k": int(k)} if method else None
        self.refresh()

    def set_blackout(self, on: bool):
        with self.lock:
            if on and self.job and self.job.get("state") == "running":
                raise RuntimeError("A save is running. Wait for it to finish.")
            self.blackout = bool(on)
        self.refresh()

    def start_save(self, fid: str, fx_ids, verify_only: bool = False) -> dict:
        with self.lock:
            if self.job and self.job.get("state") == "running":
                raise RuntimeError("Another save is already running")
            fl = self.store.get_float(fid)
            if not fl:
                raise KeyError("float not found")
            chosen = [fx for fx in fl["fixtures"] if fx["id"] in set(fx_ids)]
            ok = [fx for fx in chosen if fx["mode"] == fixtures.SAVE_MODE and fx.get("address")]
            skipped = [{"id": fx["id"], "label": fx["label"],
                        "why": "not in Mode 7" if fx["mode"] != fixtures.SAVE_MODE else "no address"}
                       for fx in chosen if fx not in ok]
            if not ok:
                raise RuntimeError("None of the selected fixtures are in Mode 7 with an address. "
                                   "Switch them to Mode 7 first (Addressing tab, or REAP).")
            if self.hold:
                raise RuntimeError("Fixtures are being re-addressed. Wait for that to finish.")
            if self._paused_for_rdm:
                raise RuntimeError("Busy talking to fixtures. Try again in a moment.")
            # Never save a dark look: clear Blackout, the address finder and flashes first.
            self.blackout = False
            self.sweep = None
            self.flash.clear()
            if fid != self.active_float or not self.ctl.output_enabled:
                self.active_float = fid
                self.ctl.output_enabled = True
            self.job = {"state": "running", "kind": "verify" if verify_only else "save", "float": fid,
                        "fixtures": [fx["id"] for fx in ok], "skipped": skipped, "step": "starting",
                        "progress": 0.0, "started": time.time(), "message": ""}
        threading.Thread(target=self._run_save, args=([fx["id"] for fx in ok], verify_only), daemon=True).start()
        return dict(self.job)

    def _run_save(self, ids, verify_only):
        # Special functions: 1-2 = save current values as initial values, 3-4 = show saved initial values.
        # Never park on 5-6 (factory demo at power-on).
        if verify_only:
            steps = [("Blacking out the live look", SETTLE_S, 0, True),
                     ("Showing what is saved in the fixtures", 6.0, 3, True),
                     ("Back to the live look", SETTLE_S, 0, False)]
        else:
            steps = [("Sending the look", SETTLE_S, 0, False),
                     ("Saving into the fixtures (holding the save command)", SAVE_HOLD_S, 1, False),
                     ("Releasing", SETTLE_S, 0, False)]
        total = sum(s[1] for s in steps)
        done = 0.0
        try:
            fid = self.job["float"]
            for name, dur, val, dark in steps:
                with self.lock:
                    for i in ids:
                        self.special[i] = val
                        if dark:
                            self.dark.add(i)
                        else:
                            self.dark.discard(i)
                    self.job["step"] = name
                self.ctl.set_targets(self.frames())
                t0 = time.monotonic()
                while time.monotonic() - t0 < dur:
                    time.sleep(0.1)
                    with self.lock:
                        self.job["progress"] = min(1.0, (done + time.monotonic() - t0) / total)
                        why = self.job.get("abort")
                        if not why and self.active_float != fid:
                            why = "Another float went live"
                        if not why and (not self.ctl.output_enabled or self.hold or self.blackout or self.sweep):
                            why = "Output was stopped or changed"
                    if why:
                        raise RuntimeError(why + " before the save finished. Nothing is confirmed; run it again.")
                done += dur
            with self.lock:
                self.job.update(state="done", step="Done", progress=1.0,
                                message="Showed the saved look for 6 s." if verify_only else
                                "Save command held for %.1f s." % SAVE_HOLD_S)
        except Exception as e:
            with self.lock:
                self.job.update(state="error", step="Stopped", message=str(e))
        finally:
            with self.lock:
                for i in ids:
                    self.special.pop(i, None)
                    self.dark.discard(i)
            self.refresh()

    # ------------------------------------------------------------ address finder (no RDM needed)
    def start_sweep(self, fid: str, box_id: str, variant: str, mode: int, start: int = 1,
                    count: int = 20, seconds: float = 2.5) -> dict:
        with self.lock:
            if self.job and self.job.get("state") == "running":
                raise RuntimeError("A save is running. Wait for it to finish.")
        with self.store.lock:
            fl = self.store.get_float(fid)
            if not fl:
                raise KeyError("float not found")
            box = next((b for b in fl["boxes"] if b["id"] == box_id), None)
            if not box or not box.get("ip"):
                raise ValueError("Set the box IP first.")
            target = (box["ip"], int(box.get("udp_port") or 6454),
                      (box["net"] << 8) | (box["subnet"] << 4) | box["universe"])
        fp = fixtures.footprint(variant, mode)
        addrs = [a for a in range(int(start), 513, fp)][:int(count)]
        if not addrs:
            raise ValueError("Nothing to sweep")
        with self.lock:
            self.active_float = fid
            self.ctl.output_enabled = True
            self.sweep = {"target": target, "variant": variant, "mode": int(mode), "addresses": addrs,
                          "current": addrs[0], "index": 0, "seconds": float(seconds), "footprint": fp,
                          "paused": False, "token": time.monotonic()}
            token = self.sweep["token"]
        threading.Thread(target=self._run_sweep, args=(token,), daemon=True).start()
        return self.status()["sweep"]

    def sweep_control(self, action: str):
        with self.lock:
            sw = self.sweep
            if not sw:
                return
            if action == "stop":
                self.sweep = None
            elif action == "pause":
                sw["paused"] = True
            elif action == "resume":
                sw["paused"] = False
            elif action in ("next", "prev"):
                sw["paused"] = True
                sw["index"] = max(0, min(len(sw["addresses"]) - 1, sw["index"] + (1 if action == "next" else -1)))
                sw["current"] = sw["addresses"][sw["index"]]
        self.refresh()

    def _run_sweep(self, token):
        while True:
            with self.lock:
                sw = self.sweep
                if not sw or sw["token"] != token:
                    break
                secs = sw["seconds"]
            time.sleep(secs)
            with self.lock:
                sw = self.sweep
                if not sw or sw["token"] != token:
                    break
                if not sw["paused"]:
                    sw["index"] = (sw["index"] + 1) % len(sw["addresses"])
                    sw["current"] = sw["addresses"][sw["index"]]
            self.ctl.set_targets(self.frames())
        self.refresh()

    # ------------------------------------------------------------ RDM etiquette
    def pause_for_rdm(self):
        with self.lock:
            if self.job and self.job.get("state") == "running":
                raise RuntimeError("A save is running. Wait for it to finish.")
            self._paused_for_rdm += 1
            if self._paused_for_rdm == 1:
                self._resume_after_rdm = self.ctl.output_enabled
                self.ctl.output_enabled = False

    def resume_after_rdm(self):
        with self.lock:
            self._paused_for_rdm = max(0, self._paused_for_rdm - 1)
            if self._paused_for_rdm == 0 and getattr(self, "_resume_after_rdm", False):
                self.ctl.output_enabled = True

    def stop(self):
        self._stop.set()
