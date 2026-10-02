"""End-to-end: HTTP API -> engine -> Art-Net over real UDP -> virtual E-Box."""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from scenebuilder import fixtures
from scenebuilder.node import ArtNetController
from scenebuilder.server import App, make_server
from scenebuilder.simulator import demo_box
from scenebuilder.store import Store


class Api:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"error": json.loads(e.read()).get("error"), "status": e.code}


def wait_for(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.tmp.name))
        cls.ctl = ArtNetController(bind_ip="0.0.0.0", port=0).start()
        cls.app = App(cls.store, cls.ctl)
        cls.sim = demo_box(port=0).start()
        cls.app.sim = cls.sim
        cls.srv = make_server(cls.app, host="127.0.0.1", port=0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = Api("http://127.0.0.1:%d" % cls.srv.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.sim.stop()
        cls.ctl.stop()
        cls.store.close()
        cls.tmp.cleanup()

    def make_float(self):
        fl = self.api.call("POST", "/api/floats", {"code": "T1", "name": "Test float"})
        fl["boxes"][0].update(ip="127.0.0.1", udp_port=self.sim.port)
        return self.api.call("PUT", "/api/floats/" + fl["id"], fl)

    def test_01_static_and_state(self):
        with urllib.request.urlopen(self.api.base + "/") as r:
            self.assertIn(b"DMX Scene Builder", r.read())
        st = self.api.call("GET", "/api/state")
        self.assertIn("modes", st)

    def test_02_discover(self):
        res = self.api.call("POST", "/api/discover", {"wait": 0.5})
        names = [n["long_name"] for n in res["nodes"]]
        self.assertIn("E-box Remote (SIM)", names)

    def test_03_scan_fix_patch_and_drive(self):
        fl = self.make_float()
        box = fl["boxes"][0]["id"]
        scan = self.api.call("POST", "/api/floats/%s/scan" % fl["id"], {"box_id": box})
        self.assertEqual(len(scan["devices"]), 6, scan)
        tw = [d for d in scan["devices"] if d["variant_guess"] == "TW"]
        rgb = [d for d in scan["devices"] if d["variant_guess"] == "RGBW"]
        self.assertEqual((len(tw), len(rgb)), (4, 2))
        self.assertEqual(tw[0]["mode"], 11)
        self.assertEqual(tw[0]["modes_available"], [7, 11, 12, 13])

        # Readdress every module via RDM: TW at 1,4,7,10; RGBW to Mode 7 at 13, 28
        addr = 1
        for d in tw:
            r = self.api.call("POST", "/api/floats/%s/rdm" % fl["id"], {"box_id": box, "uid": d["uid"], "action": "address", "address": addr})
            self.assertEqual(r["info"]["address"], addr, r)
            addr += 3
        for d in rgb:
            r = self.api.call("POST", "/api/floats/%s/rdm" % fl["id"], {"box_id": box, "uid": d["uid"], "action": "mode", "mode": 7})
            self.assertEqual(r["info"]["mode"], 7)
            self.api.call("POST", "/api/floats/%s/rdm" % fl["id"], {"box_id": box, "uid": d["uid"], "action": "address", "address": addr})
            addr += 15
        # TW modules accept Mode 7 (field-tested), and can go back to Mode 11
        r = self.api.call("POST", "/api/floats/%s/rdm" % fl["id"], {"box_id": box, "uid": tw[0]["uid"], "action": "mode", "mode": 7})
        self.assertEqual(r["info"]["mode"], 7, r)
        r = self.api.call("POST", "/api/floats/%s/rdm" % fl["id"], {"box_id": box, "uid": tw[0]["uid"], "action": "mode", "mode": 11})
        self.assertEqual(r["info"]["mode"], 11, r)
        # identify
        r = self.api.call("POST", "/api/floats/%s/rdm" % fl["id"], {"box_id": box, "uid": tw[1]["uid"], "action": "identify", "on": True})
        self.assertTrue(self.sim.module(bytes.fromhex(tw[1]["uid"].replace(":", ""))).identify)

        # Build the float's patch from the scan
        fl = self.api.call("GET", "/api/floats/" + fl["id"])
        fl["fixtures"] = []
        a = 1
        for i, d in enumerate(tw):
            fl["fixtures"].append({"id": "tw%d" % i, "label": "TW %d" % i, "variant": "TW", "mode": 11, "box_id": box, "address": a, "uid": d["uid"]})
            a += 3
        for i, d in enumerate(rgb):
            fl["fixtures"].append({"id": "rgb%d" % i, "label": "RGB %d" % i, "variant": "RGBW", "mode": 7, "box_id": box, "address": a, "uid": d["uid"]})
            a += 15
        fl = self.api.call("PUT", "/api/floats/" + fl["id"], fl)
        self.assertNotIn("error", fl)

        # Go live and set a look
        self.api.call("POST", "/api/output", {"action": "activate", "float_id": fl["id"]})
        self.api.call("POST", "/api/floats/%s/live" % fl["id"], {"changes": {
            "tw0": {"dim": 1.0, "cct": 2700}, "tw1": {"dim": 0.5, "cct": 6500},
            "rgb0": {"dim": 1.0, "kind": "color", "hue": 240, "sat": 1.0}}})
        m0 = self.sim.module(bytes.fromhex(tw[0]["uid"].replace(":", "")))
        m1 = self.sim.module(bytes.fromhex(tw[1]["uid"].replace(":", "")))
        r0 = self.sim.module(bytes.fromhex(rgb[0]["uid"].replace(":", "")))
        self.assertTrue(wait_for(lambda: m0.dmx == bytes([0, 255, 255])), m0.dmx)
        self.assertTrue(wait_for(lambda: m1.dmx[0] == 255 and m1.dmx[1] == 128), m1.dmx)
        self.assertTrue(wait_for(lambda: len(r0.dmx) == 15 and r0.dmx[5] == 255 and r0.dmx[1] == 0), r0.dmx)

        # Save a look, change, recall
        look = self.api.call("POST", "/api/floats/%s/looks" % fl["id"], {"name": "Parade"})
        self.api.call("POST", "/api/floats/%s/live" % fl["id"], {"changes": {"tw0": {"dim": 0.0}}})
        self.assertTrue(wait_for(lambda: m0.dmx[1] == 0))
        self.api.call("POST", "/api/floats/%s/looks/%s/recall" % (fl["id"], look["id"]))
        self.assertTrue(wait_for(lambda: m0.dmx == bytes([0, 255, 255])))

        # Save into fixtures: only Mode 7 fixtures accepted
        job = self.api.call("POST", "/api/floats/%s/save" % fl["id"], {"fixture_ids": ["rgb0", "rgb1", "tw0"]})
        self.assertEqual(sorted(job["fixtures"]), ["rgb0", "rgb1"])
        self.assertEqual(job["skipped"][0]["id"], "tw0")
        self.assertTrue(wait_for(lambda: self.api.call("GET", "/api/status")["engine"]["job"]["state"] == "done", 15))
        self.assertIsNotNone(r0.saved_initial)
        self.assertEqual(r0.saved_initial[0], 1)          # special fn held at "save"
        self.assertEqual(r0.saved_initial[5], 255)        # blue saved
        # after the job, special channel back to 0
        self.assertTrue(wait_for(lambda: r0.dmx[0] == 0))

        # Patch sheet renders
        with urllib.request.urlopen(self.api.base + "/patch/" + fl["id"]) as r:
            self.assertIn(b"TW 0", r.read())

        # Release sends black for ~1 s, then stops output
        self.api.call("POST", "/api/output", {"action": "release"})
        time.sleep(1.4)
        n = self.sim.dmx_frames
        time.sleep(0.4)
        self.assertLessEqual(self.sim.dmx_frames - n, 1)

    def test_04_autopatch_and_problems(self):
        fl = self.make_float()
        box = fl["boxes"][0]["id"]
        fl["fixtures"] = [
            {"id": "a", "label": "A", "variant": "TW", "mode": 11, "box_id": box, "address": 1},
            {"id": "b", "label": "B", "variant": "TW", "mode": 11, "box_id": box, "address": 2},
            {"id": "c", "label": "C", "variant": "RGBW", "mode": 7, "box_id": box, "address": None},
        ]
        self.api.call("PUT", "/api/floats/" + fl["id"], fl)
        st = self.api.call("GET", "/api/state")
        mine = next(f for f in st["project"]["floats"] if f["id"] == fl["id"])
        texts = " ".join(p["text"] for p in mine["problems"])
        self.assertIn("overlap", texts)
        self.assertIn("no DMX address", texts)
        r = self.api.call("POST", "/api/floats/%s/autopatch" % fl["id"], {"box_id": box, "start": 1})
        self.assertEqual([c["address"] for c in r["changed"]], [1, 4, 7])
        st = self.api.call("GET", "/api/state")
        mine = next(f for f in st["project"]["floats"] if f["id"] == fl["id"])
        self.assertEqual(mine["problems"], [])

    def test_05_validation(self):
        fl = self.make_float()
        fl["fixtures"] = [{"id": "x", "label": "X", "variant": "TW", "mode": 3, "address": 1}]
        r = self.api.call("PUT", "/api/floats/" + fl["id"], fl)
        self.assertIn("isn't valid", r["error"])
        fl["fixtures"] = [{"id": "x", "label": "X", "variant": "TW", "mode": 11, "address": 600}]
        self.assertIn("1-512", self.api.call("PUT", "/api/floats/" + fl["id"], fl)["error"])

    def test_06_sweep(self):
        fl = self.make_float()
        box = fl["boxes"][0]["id"]
        sw = self.api.call("POST", "/api/floats/%s/sweep" % fl["id"], {"box_id": box, "variant": "TW", "mode": 11, "start": 1, "count": 3, "seconds": 0.3})
        self.assertEqual(sw["addresses"], [1, 4, 7])
        self.assertTrue(wait_for(lambda: self.sim.last_dmx[1:3] == b"\xff\xff" or self.sim.last_dmx[4:6] == b"\xff\xff", 3))
        self.api.call("POST", "/api/floats/%s/sweep" % fl["id"], {"action": "stop"})
        self.assertIsNone(self.api.call("GET", "/api/status")["engine"]["sweep"])

    def test_07_verify_blacks_out_then_shows_saved(self):
        fl = self.make_float()
        box = fl["boxes"][0]["id"]
        fl["fixtures"] = [{"id": "r", "label": "R", "variant": "RGBW", "mode": 7, "box_id": box, "address": 100}]
        self.api.call("PUT", "/api/floats/" + fl["id"], fl)
        self.api.call("POST", "/api/output", {"action": "activate", "float_id": fl["id"]})
        self.api.call("POST", "/api/floats/%s/save" % fl["id"], {"fixture_ids": ["r"], "verify": True})
        seen = set()
        t0 = time.time()
        while time.time() - t0 < 12:
            d = self.sim.last_dmx[99:114]
            seen.add((d[0], d[13]))  # (special, dimmer)
            if self.api.call("GET", "/api/status")["engine"]["job"]["state"] == "done":
                break
            time.sleep(0.1)
        self.assertIn((3, 255), seen)   # "show saved", colours off but dimmer full so the saved look can show
        self.api.call("POST", "/api/output", {"action": "release"})


class RdmSaveTests(IntegrationTests):
    """Reuses the harness; only the tests below run here (inherited ones are skipped)."""

    def test_01_static_and_state(self): pass
    def test_02_discover(self): pass
    def test_03_scan_fix_patch_and_drive(self): pass
    def test_04_autopatch_and_problems(self): pass
    def test_05_validation(self): pass
    def test_06_sweep(self): pass
    def test_07_verify_blacks_out_then_shows_saved(self): pass

    def test_10_params_and_rdm_save_tw(self):
        fl = self.make_float()
        box = fl["boxes"][0]["id"]
        scan = self.api.call("POST", "/api/floats/%s/scan" % fl["id"], {"box_id": box})
        d = next(x for x in scan["devices"] if x["variant_guess"] == "TW")
        pr = self.api.call("POST", "/api/floats/%s/rdm" % fl["id"], {"box_id": box, "uid": d["uid"], "action": "params"})
        names = [p["description"] for p in pr["params"]]
        self.assertIn("Init position LEDs", names)
        self.assertEqual(pr["save_pid"], 0x8101)
        # set a param through the generic editor
        r = self.api.call("POST", "/api/floats/%s/rdm" % fl["id"], {"box_id": box, "uid": d["uid"], "action": "set_param", "pid": 0x8102, "value": 1})
        self.assertTrue(r["ok"], r)
        m = self.sim.module(bytes.fromhex(d["uid"].replace(":", "")))
        self.assertEqual(m.terminator, 1)
        # patch + live look, then save over RDM (works for a TW fixture in Mode 11)
        fl = self.api.call("GET", "/api/floats/" + fl["id"])
        fl["fixtures"] = [{"id": "t", "label": "T", "variant": "TW", "mode": 11, "box_id": box, "address": m.address, "uid": d["uid"]},
                          {"id": "u", "label": "U", "variant": "TW", "mode": 11, "box_id": box, "address": 300}]
        self.api.call("PUT", "/api/floats/" + fl["id"], fl)
        self.api.call("POST", "/api/output", {"action": "activate", "float_id": fl["id"]})
        self.api.call("POST", "/api/floats/%s/live" % fl["id"], {"changes": {"t": {"dim": 0.25, "cct": 6500}}})
        self.assertTrue(wait_for(lambda: m.dmx[:2] == bytes([255, 64])), m.dmx)
        res = self.api.call("POST", "/api/floats/%s/save_rdm" % fl["id"], {})
        self.assertEqual(res["saved"], 6, res)   # every module on the box, no linking needed
        self.assertEqual(m.saved_via, "rdm")
        self.assertEqual(m.saved_initial[:2], bytes([255, 64]))
        self.api.call("POST", "/api/output", {"action": "release"})

    def test_11_palette(self):
        c = self.api.call("POST", "/api/palette", {"name": "Deep blue", "state": {"kind": "color", "hue": 240, "sat": 1, "white": 0, "dim": 0.5}, "include_dim": False})
        self.assertNotIn("dim", c["state"])
        self.assertEqual(c["state"]["hue"], 240.0)
        w = self.api.call("POST", "/api/palette", {"name": "Warm", "state": {"kind": "white", "cct": 2700, "dim": 0.8}, "include_dim": True})
        self.assertEqual(w["state"]["dim"], 0.8)
        pal = self.api.call("GET", "/api/palette")
        self.assertEqual([x["name"] for x in pal][-2:], ["Deep blue", "Warm"])
        self.api.call("PUT", "/api/palette/" + c["id"], {"name": "Navy"})
        self.api.call("DELETE", "/api/palette/" + w["id"])
        names = [x["name"] for x in self.api.call("GET", "/api/palette")]
        self.assertIn("Navy", names)
        self.assertNotIn("Warm", names)


class ReviewFixTests(unittest.TestCase):
    """Regression tests for the 2026-09-27 code review findings (no network needed)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.ctl = ArtNetController(port=0)  # not started: we only render frames
        from scenebuilder.engine import Engine
        self.eng = Engine(self.store, self.ctl)
        from scenebuilder.store import new_float
        fl = new_float("T", "T")
        fl["boxes"][0]["ip"] = "127.0.0.1"
        self.box = fl["boxes"][0]["id"]
        fl["fixtures"] = [
            {"id": "m7", "label": "M7", "variant": "RGBW", "mode": 7, "box_id": self.box, "address": 16},
            {"id": "m1", "label": "M1", "variant": "RGBW", "mode": 1, "box_id": self.box, "address": 14},  # overlaps m7 ch1..3
        ]
        self.store.put_float(fl)
        self.fl = fl
        self.eng.active_float = fl["id"]

    def tearDown(self):
        self.eng.stop()
        self.store.close()
        self.tmp.cleanup()

    def frame(self):
        return list(self.eng.frames().values())[0]

    def test_special_channel_written_last(self):
        self.fl["live"]["m1"] = {"dim": 1, "kind": "color", "hue": 30, "sat": 1}
        self.assertEqual(self.frame()[15], 0)       # m7 ch1 stays 0 despite m1 overlapping it
        self.eng.special["m7"] = 1
        self.assertEqual(self.frame()[15], 1)

    def test_hold_sends_zeros(self):
        self.eng.set_hold(True)
        self.assertEqual(self.frame(), bytes(512))
        self.eng.set_hold(False)
        self.assertNotEqual(self.frame(), bytes(512))

    def test_release_sticks_through_rdm(self):
        self.ctl.output_enabled = True
        self.eng.pause_for_rdm()
        self.eng.release()
        self.eng.resume_after_rdm()
        self.assertFalse(self.ctl.output_enabled)

    def test_save_clears_dark_overlays(self):
        self.fl["fixtures"] = [self.fl["fixtures"][0]]
        self.eng.blackout = True
        self.eng.sweep = {"target": None, "current": 1, "variant": "TW", "mode": 11, "addresses": [1], "index": 0,
                          "seconds": 1, "footprint": 3, "paused": True, "token": 0}
        self.eng.start_save(self.fl["id"], ["m7"])
        self.assertFalse(self.eng.blackout)
        self.assertIsNone(self.eng.sweep)
        self.eng.release()
        wait_for(lambda: self.eng.job["state"] != "running", 5)
        self.assertEqual(self.eng.job["state"], "error")   # Release mid-save is reported, not "done"

    def test_mode2_white_is_not_dark(self):
        self.assertEqual(fixtures.render("RGBW", 2, {"dim": 1, "kind": "white"}), b"\xff\xff\xff")

    def test_autopatch_avoids_others(self):
        from scenebuilder.server import autopatch
        self.fl["fixtures"] = [
            {"id": "a", "label": "A", "variant": "TW", "mode": 11, "box_id": self.box, "address": 1},
            {"id": "b", "label": "B", "variant": "RGBW", "mode": 7, "box_id": self.box, "address": 1},
        ]
        r = autopatch(self.store, self.fl, self.box, 1, ["b"])
        self.assertEqual(r["changed"], [{"id": "b", "address": 4}])

    def test_import_is_validated(self):
        with self.assertRaises(ValueError):
            self.store.replace_project({"floats": [{"id": "x", "boxes": [], "fixtures": [{"label": "bad", "variant": "ZZ"}], "looks": []}]})
        self.assertEqual(len(self.store.data["floats"]), 1)


class PutKeepsServerStateTests(IntegrationTests):
    def test_01_static_and_state(self): pass
    def test_02_discover(self): pass
    def test_03_scan_fix_patch_and_drive(self): pass
    def test_04_autopatch_and_problems(self): pass
    def test_05_validation(self): pass
    def test_06_sweep(self): pass
    def test_07_verify_blacks_out_then_shows_saved(self): pass

    def test_20_stale_put(self):
        fl = self.make_float()
        box = fl["boxes"][0]["id"]
        fl["fixtures"] = [{"id": "a", "label": "A", "variant": "TW", "mode": 11, "box_id": box, "address": 1}]
        fl = self.api.call("PUT", "/api/floats/" + fl["id"], fl)
        self.api.call("POST", "/api/floats/%s/live" % fl["id"], {"changes": {"a": {"dim": 0.3}}})
        self.api.call("POST", "/api/floats/%s/looks" % fl["id"], {"name": "L"})
        stale = json.loads(json.dumps(fl))
        stale["live"] = {}
        stale["looks"] = []
        stale["name"] = "Renamed"
        r = self.api.call("PUT", "/api/floats/" + fl["id"], stale)   # same rev: accepted, but live/looks kept
        self.assertEqual(r["live"]["a"]["dim"], 0.3)
        self.assertEqual(len(r["looks"]), 1)
        r2 = self.api.call("PUT", "/api/floats/" + fl["id"], stale)  # now stale rev
        self.assertEqual(r2.get("status"), 409)


if __name__ == "__main__":
    unittest.main()


class ReleaseToBlackTests(IntegrationTests):
    def test_01_static_and_state(self): pass
    def test_02_discover(self): pass
    def test_03_scan_fix_patch_and_drive(self): pass
    def test_04_autopatch_and_problems(self): pass
    def test_05_validation(self): pass
    def test_06_sweep(self): pass
    def test_07_verify_blacks_out_then_shows_saved(self): pass

    def test_30_release_sends_black_then_stops(self):
        fl = self.make_float()
        box = fl["boxes"][0]["id"]
        fl["fixtures"] = [{"id": "a", "label": "A", "variant": "TW", "mode": 11, "box_id": box, "address": 1}]
        self.api.call("PUT", "/api/floats/" + fl["id"], fl)
        self.api.call("POST", "/api/output", {"action": "activate", "float_id": fl["id"]})
        self.api.call("POST", "/api/floats/%s/live" % fl["id"], {"changes": {"a": {"dim": 1.0}}})
        self.assertTrue(wait_for(lambda: self.sim.last_dmx[1] == 255))
        self.api.call("POST", "/api/output", {"action": "release"})
        self.assertTrue(wait_for(lambda: self.sim.last_dmx[:3] == bytes(3), 2))   # black reached the box
        time.sleep(1.4)
        n = self.sim.dmx_frames
        time.sleep(0.4)
        self.assertLessEqual(self.sim.dmx_frames - n, 1)                          # then it stopped sending
        self.assertEqual(self.sim.last_dmx[:3], bytes(3))                         # and the last thing sent was black
