#!/usr/bin/env python3
"""Fake E-Box for the iPad app's end-to-end tests (ios/Tests/EndToEndTests.swift).

Runs the same fake hardware the Python stress tests use (tests/test_stress.py): an Art-Net
simulator with Calumma modules and a fake REAP web page backed by the same modules, all on
127.0.0.1 with ephemeral ports. The iOS simulator shares the Mac's network, so the Swift engine
in the simulator talks to it directly. A small control API lets the Swift tests reset the box,
make it fail, and read back what the lights received:

  GET  /info                 {"artnet_port", "web_port"}
  POST /reset {"modules": [[hex serial, variant, mode, address], ...]}
  POST /set   {"offline": "refuse"|"blackhole"|null, "ic_od": ..., "fail_uid": ..., "confirm_delay": s,
               "restart_s": s, "drop_next": [d_uid, ...]}
  GET  /state                modules, frame count, last frame, Special-byte transitions, REAP log

Prints one JSON line with the control port, then serves until killed.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scenebuilder import artnet  # noqa: E402
from scenebuilder.simulator import VirtualModule  # noqa: E402
from tests.test_stress import FakeBox, RecSim  # noqa: E402


class LogSim(RecSim):
    """Records every change of each Mode 7 light's Special-functions byte (time, d_uid, address, value)."""

    def __init__(self, modules):
        super().__init__(modules)
        self.special_log = []
        self._last_special = {}

    def _check(self):
        now = time.time()
        with self.lock:
            frame = self.last_dmx
            self.frames.append((now, None))
            for m in self.modules:
                if m.mode == 7 and 1 <= m.address <= 512:
                    v = frame[m.address - 1]
                    key = (m.uid, m.address)
                    if self._last_special.get(key) != v:
                        self._last_special[key] = v
                        self.special_log.append([now, m.uid[2:].hex(), m.address, v])


class FailBox(FakeBox):
    fail_uid = None

    def answer(self, path, q):
        if path == "ds_setup_device" and q.get("uid") == self.fail_uid:
            self.pending = {"d_uid": q["uid"], "err": 3, "ready": time.time()}
            return {"status": 0}
        return super().answer(path, q)


DEFAULT = [["012e2a01", "RGBW", 1, 1], ["012e2a02", "RGBW", 1, 16], ["012e2a03", "TW", 11, 31], ["012e2a04", "TW", 11, 50]]
sim = LogSim([]).start()
box = FailBox(sim)
lock = threading.Lock()


def reset(mods):
    with sim.lock:
        sim.modules[:] = [VirtualModule(int(h, 16), v, int(m), int(a)) for h, v, m, a in mods]
        sim.frames.clear()
        sim.special_log.clear()
        sim._last_special.clear()
        sim.violations.clear()
        sim.last_dmx = bytes(512)
    box.go_online()
    box.ic_od = "enabled"
    box.fail_uid = None
    box.confirm_delay = box.delay = box.discover_delay = 0.0
    box.restart_s = 1.5
    box.drop_next = set()
    box.queue, box.pending = [], None
    box.log.clear()


reset(DEFAULT)


class Control(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/info":
            return self._send({"artnet_port": sim.port, "web_port": box.port})
        if self.path == "/state":
            with sim.lock:
                mods = [{"d_uid": m.uid[2:].hex(), "variant": m.variant, "mode": m.mode, "address": m.address,
                         "label": m.label, "dmx": list(m.dmx),
                         "saved_initial": list(m.saved_initial) if m.saved_initial else None} for m in sim.modules]
                frames = len(sim.frames)
                last_t = sim.frames[-1][0] if sim.frames else 0
                return self._send({"modules": mods, "frames": frames, "last_frame_time": last_t, "now": time.time(),
                                   "last_dmx": list(sim.last_dmx), "special_log": list(sim.special_log),
                                   "ic_od": box.ic_od, "offline": box.offline,
                                   "reap_log": [[t, p] for t, p, _ in box.log]})
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        b = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/reset":
            reset(b.get("modules") or DEFAULT)
            return self._send({"ok": True})
        if self.path == "/set":
            for k, v in b.items():
                if k == "offline":
                    box.go_offline(v) if v else box.go_online()
                elif k == "drop_next":
                    box.drop_next = set(v)
                else:
                    setattr(box, k, v)
            return self._send({"ok": True})
        self.send_error(404)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Control)
srv.daemon_threads = True
print(json.dumps({"control_port": srv.server_address[1], "artnet_port": sim.port, "web_port": box.port}), flush=True)
srv.serve_forever()
