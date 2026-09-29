"""Stress / field-failure scenarios: a box that drops off the network at arbitrary moments.

Each test builds a combined fake E-Box on 127.0.0.1:
  * the Art-Net simulator (UDP, ephemeral port) records every DMX frame it receives and checks
    the Mode 7 Special-functions byte of every light that is *physically* in Mode 7:
    never 5-6, and 1-2 only while a save job is holding it;
  * a fake REAP web page (HTTP, ephemeral port) backed by the SAME virtual modules, so a
    mode/address change through the web page really moves the light the simulator decodes.
The app runs in-process on an ephemeral port and is driven through its HTTP API the way the
iPad does (web/app.js). Nothing here touches port 6454/8080 or any 10.x address.

Tests marked "# BUG ..." currently fail on purpose: each names the bug it reproduces.
"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scenebuilder import artnet, reap as reap_mod
from scenebuilder import engine as engine_mod
from scenebuilder.node import ArtNetController
from scenebuilder.server import App, make_server
from scenebuilder.simulator import EBoxSimulator, VirtualModule
from scenebuilder.store import Store

BLOCK_LIMIT_S = 10.0  # an iPad fetch that hangs longer than this looks like a dead app


# ---------------------------------------------------------------------------- fake hardware
class RecSim(EBoxSimulator):
    """Art-Net side of the box. Records every frame; flags Special-functions violations."""

    def __init__(self, modules):
        super().__init__(host="127.0.0.1", port=0, modules=modules, rdm_enabled=False)
        self.online = True
        self.engine = None
        self.frames = []          # (time, 512 bytes)
        self.violations = []      # (time, uid, address, value)
        self._save_seen = 0.0

    def _handle(self, data, addr):
        if not self.online:
            return  # cable pulled / box powered off: packets go nowhere
        n = self.dmx_frames
        super()._handle(data, addr)
        if artnet.opcode(data) == artnet.OP_DMX and self.dmx_frames != n:
            self._check()

    def _check(self):
        now = time.time()
        job = self.engine.job if self.engine else None
        running = bool(job and job.get("kind") == "save" and job.get("state") == "running")
        if running:
            self._save_seen = now
        allowed = running or now - self._save_seen < 0.3
        with self.lock:
            frame = self.last_dmx
            self.frames.append((now, frame))
            for m in self.modules:
                if m.mode == 7 and 1 <= m.address <= 512:
                    v = frame[m.address - 1]
                    if v in (5, 6) or (v in (1, 2) and not allowed):
                        self.violations.append((round(now, 3), m.uid.hex(), m.address, v))


class FakeBox:
    """The box's REAP web page, backed by the simulator's modules. Can go offline two ways:
    'refuse' (connection dropped at once) or 'blackhole' (no answer: client times out)."""

    def __init__(self, sim: RecSim):
        self.sim = sim
        self.offline = None
        self.delay = 0.0              # every request
        self.discover_delay = 0.0     # each ds_get_device
        self.confirm_delay = 0.0      # ds_get_setup answers only this long after the setup
        self.drop_next = set()        # d_uids the next discovery misses
        self.restart_s = 1.5
        self.ic_od = "enabled"
        self.queue, self.pending = [], None
        self.after = None             # hook(path, q, out) run before each answer
        self.log = []                 # (time, path, q)
        self.saw = {}                 # path -> threading.Event
        box = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if box.offline == "refuse":
                    self.close_connection = True
                    return
                if box.offline == "blackhole":
                    t0 = time.time()
                    while box.offline == "blackhole" and time.time() - t0 < 8:
                        time.sleep(0.05)
                    self.close_connection = True
                    return
                n = int(self.headers.get("Content-Length") or 0)
                q = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode(), keep_blank_values=True))
                path = self.path.strip("/")
                box.log.append((time.time(), path, q))
                box.saw.setdefault(path, threading.Event()).set()
                if box.delay:
                    time.sleep(box.delay)
                out = box.answer(path, q)
                if box.after:
                    box.after(path, q, out)
                body = json.dumps(out).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def mod(self, d_uid):
        return next(m for m in self.sim.modules if m.uid[2:].hex() == d_uid.lower())

    def answer(self, path, q):
        if path == "status_i":
            return {"rdmu": "52:53:00:e8:1f:0b"}
        if path == "rdm_test":
            with self.sim.lock:
                self.queue = [{"d_uid": m.uid[2:].hex(), "u_l": m.label, "d_l": "Calumma", "dmx_a": m.address - 1,
                               "dmx_p": m.mode, "dmx_p_c": 16, "sp": 3, "dmx_t": "off", "b_f": "off",
                               "pwr": "low", "err": 0}
                              for m in self.sim.modules if m.uid[2:].hex() not in self.drop_next]
            self.drop_next = set()
            return {"status": "rdm discovery"}
        if path == "ds_get_device":
            if self.discover_delay:
                time.sleep(self.discover_delay)
            return dict(self.queue.pop(0), disc_done=0) if self.queue else {"disc_done": 1}
        if path == "ds_setup_device":
            m = self.mod(q["uid"])
            with self.sim.lock:
                m.address, m.mode, m.label = int(q["dmx_a"]) + 1, int(q["dmx_p"]) + 1, q["u_l"]
            self.pending = {"d_uid": q["uid"], "err": 0, "ready": time.time() + self.confirm_delay}
            return {"status": 0}
        if path == "rdm_identify":
            self.pending = {"d_uid": q["uid"], "err": 0, "ready": time.time()}
            return {"status": 0}
        if path == "ds_get_setup":
            p = self.pending
            if not p or time.time() < p["ready"]:
                return {}
            self.pending = None
            return {"d_uid": p["d_uid"], "err": p["err"]}
        if path == "oth_s":
            if "ic_od" in q:
                self.ic_od = "enabled" if q["ic_od"] == "1" else "disabled"
            return {"dmxh": "on", "ebm": "pass-through", "ic_od": self.ic_od, "status": 0}
        if path == "sys_res":
            self.go_offline("refuse")
            threading.Timer(self.restart_s, self.go_online).start()
            return {}
        return {}

    def go_offline(self, how="refuse"):
        self.offline = how
        self.sim.online = False

    def go_online(self):
        self.offline = None
        self.sim.online = True

    def stop(self):
        self.go_online()
        self.srv.shutdown()
        self.srv.server_close()


# ---------------------------------------------------------------------------- app harness
class Server:
    def __init__(self, directory):
        self.store = Store(Path(directory))
        self.ctl = ArtNetController(port=0).start()
        self.app = App(self.store, self.ctl)
        self.http = make_server(self.app, host="127.0.0.1", port=0)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.http.server_address[1]

    def stop(self):
        for t in list(self.app._identify_timers.values()):
            t.cancel()
        self.http.shutdown()
        self.http.server_close()
        self.app.engine.stop()
        self.ctl.stop()
        self.store.close()


class Resp:
    def __init__(self, code, body, elapsed):
        self.code, self.body, self.elapsed = code, body, elapsed

    @property
    def error(self):
        return (self.body or {}).get("error", "") if isinstance(self.body, dict) else ""

    def __repr__(self):
        return "<%s %.1fs %s>" % (self.code, self.elapsed, json.dumps(self.body)[:300])


A, B, C, D = "012e2a01", "012e2a02", "012e2a03", "012e2a04"
UID = {x: "5253:" + x.upper() for x in (A, B, C, D)}


class StressBase(unittest.TestCase):
    def setUp(self):
        self._settle = engine_mod.SETTLE_S
        engine_mod.SETTLE_S = 0.5  # shorter save job; the 4.5 s save hold stays real
        mods = [VirtualModule(int(A, 16), "RGBW", 1, 1), VirtualModule(int(B, 16), "RGBW", 1, 16),
                VirtualModule(int(C, 16), "RGBW", 1, 31), VirtualModule(int(D, 16), "RGBW", 1, 50)]
        self.sim = RecSim(mods).start()
        self.box = FakeBox(self.sim)
        self._web_port = reap_mod.WEB_PORT
        reap_mod.WEB_PORT = self.box.port
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = Server(self.tmp.name)
        self.sim.engine = self.srv.app.engine
        fl = self.call("POST", "/api/floats", {"code": "S1", "name": "Stress float"}).body
        fl["boxes"][0].update(ip="127.0.0.1", udp_port=self.sim.port)
        bid = fl["boxes"][0]["id"]
        fl["fixtures"] = [{"id": "fx" + x[-1], "label": "L" + x[-1], "variant": "RGBW", "mode": 1, "box_id": bid,
                           "address": addr, "uid": UID[x]} for x, addr in ((A, 1), (B, 16), (C, 31), (D, 50))]
        fl = self.call("PUT", "/api/floats/" + fl["id"], fl).body
        self.fid, self.bid = fl["id"], bid
        self.ids = [f["id"] for f in fl["fixtures"]]
        # Dim red looks: in Mode 1 the red byte is 1 or 5, so if the app ever renders a stale
        # patch onto a light that is really in Mode 7, it lands on the Special channel.
        self.call("POST", "/api/floats/%s/live" % self.fid, {"changes": {
            "fx1": {"dim": 0.005, "kind": "color", "hue": 0, "sat": 1},
            "fx2": {"dim": 0.02, "kind": "color", "hue": 0, "sat": 1},
            "fx3": {"dim": 0.005, "kind": "color", "hue": 0, "sat": 1},
            "fx4": {"dim": 0.02, "kind": "color", "hue": 0, "sat": 1}}})
        self.check_special = True

    def tearDown(self):
        app = self.srv.app
        self.box.go_online()
        if app.scan_lock.acquire(timeout=30):   # let a straggling request finish before the
            app.scan_lock.release()             # next test's box takes over reap.WEB_PORT
        t0 = time.time()
        while app.engine.job and app.engine.job.get("state") == "running" and time.time() - t0 < 15:
            time.sleep(0.1)
        violations = list(self.sim.violations)
        self.srv.stop()
        self.box.stop()
        self.sim.stop()
        reap_mod.WEB_PORT = self._web_port
        engine_mod.SETTLE_S = self._settle
        self.tmp.cleanup()
        if self.check_special:
            self.assertEqual(violations, [], "Special-functions byte hit 1-2 outside a save or 5-6")

    # --------------------------------------------------------------- helpers
    def call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.srv.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Resp(r.status, json.loads(r.read()), time.time() - t0)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except ValueError:
                body = None
            return Resp(e.code, body, time.time() - t0)
        except OSError as e:  # timed out: what the iPad sees as a hung page
            return Resp(None, {"error": "client timeout: %s" % e}, time.time() - t0)

    def bg(self, fn, *a):
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", fn(*a)), daemon=True)
        t.start()
        return t, out

    def status(self):
        return self.call("GET", "/api/status").body["engine"]

    def float(self):
        return self.call("GET", "/api/floats/" + self.fid).body

    def apply(self, ids=None, timeout=60):
        return self.call("POST", "/api/floats/%s/apply" % self.fid, {"to_mode7": ids or self.ids}, timeout=timeout)

    def scan(self):
        return self.call("POST", "/api/floats/%s/scan" % self.fid, {"box_id": self.bid})

    def go_live(self):
        return self.call("POST", "/api/output", {"action": "activate", "float_id": self.fid})

    def press_release_button(self):
        """web/app.js #releaseBtn: the button reads 'Clear hold' when held, but sends
        'release' only if output is on, else 'resume'."""
        e = self.status()
        return self.call("POST", "/api/output", {"action": "release" if (e["hold"] or e["output"]) else "resume"})

    def save(self, verify=False):
        return self.call("POST", "/api/floats/%s/save" % self.fid, {"fixture_ids": self.ids, "verify": verify})

    def wait_job(self, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = self.status()["job"]
            if j and j["state"] != "running":
                return j
            time.sleep(0.1)
        self.fail("save job stuck in 'running'")

    def mismatches(self):
        out = []
        for fx in self.float()["fixtures"]:
            m = self.box.mod(fx["uid"].split(":")[1])
            if fx["address"] is None:
                continue  # unknown after a failed change: the app doesn't drive it until a Scan fills it in
            if (m.mode, m.address) != (fx["mode"], fx["address"]):
                out.append("%s: store Mode %s @%s, light Mode %s @%s" % (fx["label"], fx["mode"], fx["address"],
                                                                          m.mode, m.address))
        return out

    def assert_idle(self):
        app = self.srv.app
        self.assertTrue(app.scan_lock.acquire(timeout=1), "scan_lock never released")
        app.scan_lock.release()
        e = self.status()
        self.assertFalse(e["paused_for_rdm"], "output still paused for RDM")
        self.assertFalse(e["job"] and e["job"]["state"] == "running", "job stuck running")

    def drop_after_first_confirm(self, how="refuse", back_after=None):
        """Box drops off the network right after light A confirms its change."""
        def hook(path, q, out):
            if path == "ds_get_setup" and out.get("d_uid") == A:
                self.box.after = None
                self.box.go_offline(how)
                if back_after:
                    threading.Timer(back_after, self.box.go_online).start()
        self.box.after = hook

    def failed_apply_consistent(self):
        """A mode-7 apply that fails after light A: hold on, store still matches lights."""
        self.drop_after_first_confirm()
        r = self.apply()
        self.box.go_online()
        self.assertEqual(r.code, 200, r)
        self.assertTrue(r.body["held"], r)
        self.assertTrue(self.status()["hold"])
        self.assertEqual(self.mismatches(), [])
        return r


# ---------------------------------------------------------------------------- 1. box goes offline
class BoxOfflineTests(StressBase):
    def test_box_drops_mid_apply_then_user_recovers_with_ui_buttons(self):
        r = self.failed_apply_consistent()
        self.assertLess(r.elapsed, BLOCK_LIMIT_S)
        self.assertEqual(r.body["done"], ["L1"])
        self.assertEqual(self.press_release_button().code, 200)   # 'Clear hold' with output on
        self.assertFalse(self.status()["hold"])
        s = self.scan()
        self.assertEqual(len(s.body["devices"]), 4, s)
        r = self.apply()
        self.assertTrue(r.body["ok"], r)
        self.assertFalse(self.status()["hold"])
        self.assertEqual(self.mismatches(), [])
        self.assertEqual(self.go_live().code, 200)
        j = self.save()
        self.assertEqual(j.code, 200, j)
        self.assertEqual(self.wait_job()["state"], "done")
        for m in self.sim.modules:
            self.assertIsNotNone(m.saved_initial, m.uid.hex())
            self.assertEqual(m.saved_initial[0], 1)
        self.assert_idle()

    def test_clear_hold_pressed_while_scan_runs(self):
        self.failed_apply_consistent()
        self.box.discover_delay = 0.4
        t, out = self.bg(self.scan)
        t0 = time.time()
        while not self.status()["paused_for_rdm"] and time.time() - t0 < 5:
            time.sleep(0.05)
        e = self.status()
        self.assertTrue(e["hold"] and not e["output"], e)   # iPad shows 'Clear hold'
        self.press_release_button()
        t.join(30)
        problems = []
        if self.status()["hold"]:
            problems.append("hold still on after pressing Clear hold")
        s = self.save()                         # user moves on to Save
        if "held dark" in s.error:
            problems.append("Save refused: " + s.error)
        self.wait_job() if s.code == 200 else None
        self.assertEqual(problems, [])

    def test_box_drops_after_accepting_a_change(self):
        def hook(path, q, out):
            if path == "ds_setup_device" and q["uid"] == B:
                self.box.after = None
                self.box.go_offline("refuse")
                threading.Timer(1.0, self.box.go_online).start()
        self.box.after = hook
        r = self.apply()
        self.assertTrue(r.body["held"], r)
        problems = ["store != lights: %s" % m for m in self.mismatches()]
        time.sleep(1.1)
        self.press_release_button()             # what the error dialog tells the user to do
        self.go_live()
        time.sleep(0.4)
        if self.sim.violations:
            problems.append("after Release + Go live the light saw Special=%s" % self.sim.violations[0][3])
        self.check_special = False              # reported above
        self.call("POST", "/api/output", {"action": "release"})
        self.assertEqual(problems, [])

    def test_apply_again_keeps_output_dark(self):
        self.failed_apply_consistent()
        t_press = time.time()
        r = self.apply()
        self.assertTrue(r.body["ok"], r)
        early = [f for t, f in self.sim.frames if t_press <= t <= t_press + 0.35]
        self.assertTrue(early, "no frames seen during the hold phase")
        lit = [f for f in early if any(f)]
        self.assertEqual(len(lit), 0, "%d non-zero frame(s) sent while 'held dark'" % len(lit))

    def test_box_unplugged_mid_apply_blocks_request(self):
        self.drop_after_first_confirm("blackhole")
        r = self.apply(timeout=40)
        self.box.go_online()
        self.assertTrue(self.status()["hold"])
        self.assertEqual(self.mismatches(), [])
        self.press_release_button()
        self.assertEqual(len(self.scan().body["devices"]), 4)
        self.assertTrue(self.apply().body["ok"])
        self.assert_idle()
        self.assertLess(r.elapsed, BLOCK_LIMIT_S, "apply blocked %.1f s with the box unplugged" % r.elapsed)

    def test_scan_with_box_unplugged_then_back(self):
        self.box.go_offline("blackhole")
        r = self.scan()
        self.box.go_online()
        self.assertEqual(r.code, 400, r)
        self.assertLess(r.elapsed, BLOCK_LIMIT_S)
        self.assert_idle()
        self.assertEqual(len(self.scan().body["devices"]), 4)

    def test_box_drops_mid_scan_discovery(self):
        n = {"k": 0}

        def hook(path, q, out):
            if path == "ds_get_device":
                n["k"] += 1
                if n["k"] == 2:
                    self.box.go_offline("refuse")
        self.box.after = hook
        r = self.scan()
        self.box.after = None
        self.box.go_online()
        self.assertEqual(r.code, 400, r)
        self.assert_idle()
        self.assertEqual(len(self.scan().body["devices"]), 4)

    def test_apply_survives_a_missed_light_in_discovery(self):
        self.box.drop_next = {B}
        r = self.apply()
        self.press_release_button() if r.body.get("held") else None
        self.assertTrue(r.body["ok"], r)

    def test_identify_spam_and_identify_while_offline(self):
        self.go_live()
        self.assertEqual(len(self.scan().body["devices"]), 4)
        res = []
        ts = [threading.Thread(target=lambda u=u: res.append(self.call(
            "POST", "/api/floats/%s/rdm" % self.fid, {"box_id": self.bid, "uid": UID[u], "action": "identify", "on": True})))
            for u in (A, B, C, D, A, B, C, D)]
        for t in ts:                             # taps, not one burst (burst: see test_burst_of_requests)
            t.start()
            time.sleep(0.03)
        [t.join(30) for t in ts]
        self.assertEqual([r for r in res if r.code not in (200, 400)], [])
        self.assertTrue(any(r.code == 200 for r in res))
        self.assert_idle()
        self.assertTrue(self.status()["output"], "output not restored after identify")
        self.box.go_offline("refuse")
        r = self.call("POST", "/api/floats/%s/rdm" % self.fid, {"box_id": self.bid, "uid": UID[B], "action": "identify", "on": True})
        self.assertEqual(r.code, 400, r)
        self.assertLess(r.elapsed, BLOCK_LIMIT_S)
        self.assert_idle()
        self.assertTrue(self.status()["output"])

    def test_output_data_switch_and_restart(self):
        self.go_live()
        r = self.call("POST", "/api/floats/%s/box" % self.fid, {"box_id": self.bid, "action": "output_data", "enabled": False})
        self.assertEqual(r.code, 200, r)
        self.assertFalse(self.status()["output"])
        r = self.call("POST", "/api/floats/%s/box" % self.fid, {"box_id": self.bid})   # box mid-restart
        self.assertEqual(r.code, 400, r)
        time.sleep(self.box.restart_s + 0.3)
        self.assertEqual(self.call("POST", "/api/floats/%s/box" % self.fid, {"box_id": self.bid}).body["output_data"], "disabled")
        self.assertTrue(self.apply().body["ok"])
        self.go_live()
        self.assertIn("Play saved look", self.save().error)
        r = self.call("POST", "/api/floats/%s/box" % self.fid, {"box_id": self.bid, "action": "output_data", "enabled": True})
        self.assertEqual(r.code, 200, r)
        time.sleep(self.box.restart_s + 0.3)
        self.assertEqual(self.call("POST", "/api/floats/%s/box" % self.fid, {"box_id": self.bid}).body["output_data"], "enabled")
        n = self.sim.dmx_frames
        self.go_live()
        time.sleep(0.3)
        self.assertGreater(self.sim.dmx_frames, n)
        self.assert_idle()


# ---------------------------------------------------------------------------- 2. clicks and concurrency
class ConcurrencyTests(StressBase):
    def test_go_live_during_apply(self):
        self.box.confirm_delay = 1.0
        ev = threading.Event()

        def hook(path, q, out):
            if path == "ds_setup_device" and q["uid"] == B:
                ev.set()
        self.box.after = hook
        t, out = self.bg(self.apply)
        self.assertTrue(ev.wait(20))
        g = self.go_live()                      # a second iPad presses Go live
        t.join(60)
        self.check_special = False              # reported below
        problems = []
        if g.code == 200:
            problems.append("Go live accepted while lights were being re-moded")
        if self.sim.violations:
            problems.append("%d frame(s) put Special=%s on a light mid-change" % (
                len(self.sim.violations), sorted({v[3] for v in self.sim.violations})))
        self.assertEqual(problems, [])

    def test_release_during_apply(self):
        self.box.confirm_delay = 0.5
        ev = threading.Event()
        self.box.after = lambda path, q, out: ev.set() if path == "ds_setup_device" else None
        t, out = self.bg(self.apply)
        self.assertTrue(ev.wait(20))
        r = self.press_release_button()
        self.assertIn("being changed", r.error)   # refused mid-change instead of half-undoing it
        t.join(60)
        e = self.status()
        self.assertFalse(e["hold"])
        self.assertEqual(self.mismatches(), [])
        self.assert_idle()

    def test_edit_float_during_apply(self):
        self.box.discover_delay = 0.4
        t, out = self.bg(self.apply)
        self.assertTrue(self.box.saw.setdefault("rdm_test", threading.Event()).wait(10))
        fl = self.float()
        fl["name"] = "Renamed on the other iPad"
        p = self.call("PUT", "/api/floats/" + self.fid, fl)
        t.join(60)
        self.assertEqual(p.code, 409, p)        # refused while its lights are being changed
        self.assertTrue(out["r"].body["ok"], out["r"])
        self.check_special = False              # reported below
        problems = ["store != lights: %s" % m for m in self.mismatches()]
        if self.sim.violations:
            problems.append("stale patch sent: Special=%s" % sorted({v[3] for v in self.sim.violations}))
        self.assertEqual(problems, [])

    def test_apply_while_scan_runs_and_double_apply(self):
        self.box.discover_delay = 0.8
        t, out = self.bg(self.scan)
        self.assertTrue(self.box.saw.setdefault("rdm_test", threading.Event()).wait(10))
        r = self.apply()
        self.assertEqual(r.code, 400, r)
        self.assertIn("Busy", r.error)
        t.join(30)
        self.assertEqual(len(out["r"].body["devices"]), 4)
        res = []
        ts = [threading.Thread(target=lambda: res.append(self.apply())) for _ in range(2)]
        [x.start() for x in ts]
        [x.join(60) for x in ts]
        self.assertEqual(sorted(r.code for r in res), [200, 400], res)
        self.assertFalse(self.status()["hold"])
        self.assertEqual(self.mismatches(), [])
        self.assert_idle()

    def test_two_clients_editing(self):
        errors = []

        def editor(tag):
            for i in range(12):
                fl = self.float()
                fl["name"] = "%s %d" % (tag, i)
                fl.pop("live", None)
                fl.pop("looks", None)
                r = self.call("PUT", "/api/floats/" + self.fid, fl)
                if r.code not in (200, 409):
                    errors.append(r)

        def looker():
            for i in range(12):
                for r in (self.call("POST", "/api/floats/%s/live" % self.fid, {"changes": {"fx1": {"dim": i / 20}}}),
                          self.call("POST", "/api/floats/%s/looks" % self.fid, {"name": "L%d" % i}),
                          self.call("GET", "/api/state")):
                    if r.code != 200:
                        errors.append(r)

        def merger():
            other = {"floats": [{"id": "fl_other", "code": "X", "name": "Other Mac", "boxes": [], "fixtures": [],
                                 "looks": [], "live": {}}], "palette": []}
            for r in (self.call("POST", "/api/project/merge_plan", other),
                      self.call("POST", "/api/project/merge_apply", {"incoming": other, "resolutions": {}})):
                if r.code != 200:
                    errors.append(r)
        ts = [threading.Thread(target=editor, args=("A",)), threading.Thread(target=editor, args=("B",)),
              threading.Thread(target=looker), threading.Thread(target=merger)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(errors, [])
        fl = self.float()
        self.assertEqual(len(fl["looks"]), 12)
        self.assertEqual(len(fl["fixtures"]), 4)
        self.assertIn("fl_other", [f["id"] for f in self.call("GET", "/api/project").body["floats"]])


    def test_burst_of_requests(self):
        res = []
        ts = [threading.Thread(target=lambda: res.append(self.call("GET", "/api/status"))) for _ in range(16)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual([r for r in res if r.code != 200], [])


# ---------------------------------------------------------------------------- 3. save correctness
class SaveTests(StressBase):
    def test_double_press_save_and_interference(self):
        self.assertTrue(self.apply().body["ok"])
        self.go_live()
        res = []
        ts = [threading.Thread(target=lambda: res.append(self.save())) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(sorted(r.code for r in res), [200, 400], res)
        time.sleep(1.0)
        self.assertEqual(self.call("POST", "/api/floats/%s/rdm" % self.fid,
                                   {"box_id": self.bid, "uid": UID[A], "action": "identify", "on": True}).code, 400)
        self.assertEqual(self.call("POST", "/api/output", {"action": "blackout", "on": True}).code, 400)
        self.assertEqual(self.wait_job()["state"], "done")
        for m in self.sim.modules:
            self.assertEqual(m.saved_initial[0], 1, m.uid.hex())
        # Release mid-hold: job reports an error, Special goes back to 0 on the next Go live
        self.assertEqual(self.save().code, 200)
        time.sleep(1.5)
        self.press_release_button()
        self.assertEqual(self.wait_job()["state"], "error")
        self.go_live()
        time.sleep(0.3)
        self.assertEqual([self.sim.last_dmx[m.address - 1] for m in self.sim.modules], [0, 0, 0, 0])
        self.assert_idle()

    def test_save_with_box_offline_is_not_reported_done(self):
        self.assertTrue(self.apply().body["ok"])
        self.go_live()
        self.box.go_offline("refuse")
        r = self.save()
        state = self.wait_job()["state"] if r.code == 200 else "refused"
        self.box.go_online()
        self.assertTrue(all(m.saved_initial is None for m in self.sim.modules))
        self.assertNotEqual(state, "done", "Save said done while the box was off the network")


# ---------------------------------------------------------------------------- 4. server restart
class RestartTests(StressBase):
    def test_restart_after_failed_apply(self):
        self.failed_apply_consistent()
        self.srv.stop()
        self.srv = Server(self.tmp.name)
        self.sim.engine = self.srv.app.engine
        e = self.status()
        self.assertFalse(e["hold"])
        self.assertFalse(e["output"])
        self.assertIsNone(e["job"])
        self.assertEqual(self.mismatches(), [])       # A's Mode 7 write-back survived
        self.assertEqual(self.float()["live"]["fx2"]["dim"], 0.02)
        self.assertTrue(self.apply().body["ok"])
        self.assertEqual(self.mismatches(), [])

    def test_writeback_reaches_disk_before_apply_returns(self):
        st = self.srv.store
        st.flush()
        st._stop.set()                                # simulate the process dying: no more saver ticks
        time.sleep(1.2)
        self.assertTrue(self.apply().body["ok"])
        on_disk = json.loads(st.path.read_text())
        fl = next(f for f in on_disk["floats"] if f["id"] == self.fid)
        self.assertEqual([f["mode"] for f in fl["fixtures"]], [7, 7, 7, 7])


if __name__ == "__main__":
    unittest.main()
