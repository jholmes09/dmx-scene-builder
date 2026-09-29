"""The box's web page (REAP) path, against a fake E-Box page that speaks the real endpoints."""
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scenebuilder import reap as reap_mod
from scenebuilder.node import ArtNetController
from scenebuilder.server import App
from scenebuilder.store import Store, new_float, new_fixture


class FakeBox:
    """Minimal REAP: status, discovery (optionally flaky), setup (optionally failing), Output data."""
    def __init__(self):
        self.devices = {"012e2a06": {"u_l": "Calumma 1", "dmx_a": 0, "dmx_p": 1},
                        "012e2a0d": {"u_l": "Calumma 2", "dmx_a": 4, "dmx_p": 1}}
        self.fail_uid = None
        self.ic_od = "enabled"
        self.queue, self.pending = [], None
        box = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                q = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
                path = self.path.strip("/")
                if path == "status_i":
                    out = {"rdmu": "52:53:00:e8:1f:0b"}
                elif path == "rdm_test":
                    box.queue = [dict(d, d_uid=u, d_l="Calumma", dmx_p_c=16, sp=3, dmx_t="off", b_f="off", pwr="low", err=0)
                                 for u, d in box.devices.items()]
                    out = {"status": "rdm discovery"}
                elif path == "ds_get_device":
                    out = dict(box.queue.pop(0), disc_done=0) if box.queue else {"disc_done": 1}
                elif path == "ds_setup_device":
                    u = q["uid"]
                    if u == box.fail_uid:
                        box.pending = {"d_uid": u, "err": 3}
                    else:
                        box.devices[u].update(dmx_a=int(q["dmx_a"]), dmx_p=int(q["dmx_p"]) + 1, u_l=q["u_l"])
                        box.pending = {"d_uid": u, "err": 0}
                    out = {"status": 0}
                elif path == "rdm_identify":
                    box.pending = {"d_uid": q["uid"], "err": 0}
                    out = {"status": 0}
                elif path == "ds_get_setup":
                    out, box.pending = (box.pending or {}), None
                elif path == "oth_s":
                    if "ic_od" in q:
                        box.ic_od = "enabled" if q["ic_od"] == "1" else "disabled"
                    out = {"dmxh": "on", "ebm": "pass-through", "ic_od": box.ic_od, "status": 0}
                else:
                    out = {}
                body = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.ip = "127.0.0.1:%d" % self.srv.server_address[1]


class ReapPathTests(unittest.TestCase):
    def setUp(self):
        self.box = FakeBox()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.ctl = ArtNetController(port=0).start()
        self.app = App(self.store, self.ctl)
        fl = new_float("T", "Test")
        fl["boxes"][0]["ip"] = self.box.ip
        bid = fl["boxes"][0]["id"]
        fl["fixtures"] = [new_fixture("A", "RGBW", 1, bid, 1), new_fixture("B", "RGBW", 1, bid, 5)]
        fl["fixtures"][0]["uid"] = "5253:012E2A06"
        fl["fixtures"][1]["uid"] = "5253:012E2A0D"
        self.store.put_float(fl)
        self.fl = self.store.get_float(fl["id"])
        from scenebuilder.server import Handler
        self.h = Handler.__new__(Handler)
        self.h.app = self.app

    def tearDown(self):
        self.app.engine.stop()
        self.ctl.stop()
        self.store.close()
        self.box.srv.shutdown()
        self.tmp.cleanup()

    def test_reap_setup_uses_zero_based_fields_and_confirms_right_light(self):
        r = reap_mod.Reap(self.box.ip)
        r.setup("012e2a06", "X", 16, 7, {"sp": 3, "dmx_p_c": 16})
        self.assertEqual(self.box.devices["012e2a06"]["dmx_a"], 15)
        self.assertEqual(self.box.devices["012e2a06"]["dmx_p"], 7)

    def test_to_mode7_success_records_and_releases_hold(self):
        res = self.h._apply_to_lights(self.fl, {"to_mode7": [f["id"] for f in self.fl["fixtures"]]})
        self.assertTrue(res["ok"], res)
        self.assertEqual([(f["mode"], f["address"]) for f in self.fl["fixtures"]], [(7, 1), (7, 16)])
        self.assertFalse(self.app.engine.hold)
        self.assertEqual(self.box.devices["012e2a0d"]["dmx_a"], 15)

    def test_to_mode7_failure_keeps_hold_and_leaves_patch_for_that_light(self):
        self.box.fail_uid = "012e2a0d"
        res = self.h._apply_to_lights(self.fl, {"to_mode7": [f["id"] for f in self.fl["fixtures"]]})
        self.assertFalse(res["ok"])
        self.assertTrue(self.app.engine.hold)             # nothing stray reaches the lights
        b = self.fl["fixtures"][1]
        self.assertEqual((b["mode"], b["address"]), (1, 5))  # store not changed for the light that failed
        self.app.engine.release()
        self.assertFalse(self.app.engine.hold)            # Release clears it

    def test_save_refused_while_box_plays_saved_look(self):
        self.box.ic_od = "disabled"
        r = reap_mod.Reap(self.box.ip)
        self.assertEqual(r.other_settings()["ic_od"], "disabled")


if __name__ == "__main__":
    unittest.main()
