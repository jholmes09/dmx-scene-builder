"""A virtual Anolis E-Box Remote in Pass-Through mode with Calumma modules behind it.

It answers ArtPoll, ArtTodRequest/ArtTodControl and ArtRdm (DEVICE_INFO, labels, personality,
start address, identify), consumes ArtDmx, decodes each module's footprint, and emulates the
"hold Special functions at 1-2 for 3 s = save as initial DMX values" behaviour of Mode 7.
Used for automated tests and for training/demo mode in the app.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Dict, List, Optional

from . import artnet, fixtures, rdm

ROBE_ESTA = 0x5253  # manufacturer id used in simulated UIDs

# Simulated manufacturer PIDs. The real numbers are not published; the app finds them by description.
PID_INIT_POSITION = 0x8101
PID_TERMINATOR = 0x8102
MFR_PARAMS = {
    PID_INIT_POSITION: ("Init position LEDs", 1, 0x03, 0x03, 0, 1, 0),   # name, size, uint8, GET_SET, min, max, default
    PID_TERMINATOR: ("Terminator active", 1, 0x03, 0x03, 0, 1, 0),
}


class VirtualModule:
    def __init__(self, serial: int, variant: str, mode: int, address: int, label: str = ""):
        self.uid = struct.pack(">HI", ROBE_ESTA, serial)
        self.variant = variant
        self.mode = mode
        self.address = address
        self.label = label
        self.identify = False
        self.dmx = b""  # its current footprint bytes
        self.saved_initial: Optional[bytes] = None
        self.saved_mode: Optional[int] = None
        self.saved_via: Optional[str] = None
        self.terminator = 0
        self._special_since: Optional[float] = None
        self._special_val = 0

    @property
    def personalities(self) -> List[int]:
        # Real modules expose their variant's modes (TW Calumma also accept Mode 7, field-tested 2026-10-01)
        return sorted(fixtures.MODES[self.variant].keys())

    def footprint(self) -> int:
        try:
            return fixtures.footprint(self.variant if self.mode in fixtures.MODES[self.variant] else "RGBW", self.mode)
        except ValueError:
            return 0

    def mode_variant(self) -> str:
        return self.variant if self.mode in fixtures.MODES[self.variant] else "RGBW"

    def feed(self, universe: bytes, now: float):
        fp = self.footprint()
        start = self.address - 1
        self.dmx = bytes(universe[start:start + fp])
        roles = fixtures.mode_info(self.mode_variant(), self.mode)["roles"]
        if "special" in roles and self.dmx:
            val = self.dmx[roles.index("special")]
            if val != self._special_val:
                self._special_val = val
                self._special_since = now
            elif val in (1, 2) and self._special_since is not None and now - self._special_since >= 3.0:
                self.saved_initial = bytes(self.dmx)
                self.saved_mode = self.mode
                self.saved_via = "dmx"
                self._special_since = None  # one save per hold

    def to_dict(self) -> dict:
        roles = fixtures.mode_info(self.mode_variant(), self.mode)["roles"]
        vals = dict(zip(roles, self.dmx))
        return {"uid": rdm.uid_str(self.uid), "variant": self.variant, "mode": self.mode,
                "address": self.address, "label": self.label, "identify": self.identify,
                "dmx": list(self.dmx), "channels": vals, "rgb": self.preview(),
                "saved_initial": list(self.saved_initial) if self.saved_initial else None,
                "saved_mode": self.saved_mode, "saved_via": self.saved_via}

    def preview(self) -> List[int]:
        if self.identify:
            return [255, 255, 255] if int(time.time() * 4) % 2 else [0, 0, 0]
        roles = fixtures.mode_info(self.mode_variant(), self.mode)["roles"]
        if len(self.dmx) < len(roles):
            return [0, 0, 0]
        v = dict(zip(roles, self.dmx))
        dim = v.get("dim", 255) / 255.0
        if self.variant == "TW":
            if "wsel" in v:
                k = 2700 + 3800 * v["wsel"] / 255.0
                r, g, b = fixtures.kelvin_to_rgb(k)
            else:
                ww, cw = v.get("ww", 0) / 255.0, v.get("cw", 0) / 255.0
                a, c = fixtures.kelvin_to_rgb(2700), fixtures.kelvin_to_rgb(6500)
                r, g, b = [min(1.0, ww * a[i] + cw * c[i]) for i in range(3)]
        elif self.variant == "PW":
            r, g, b = fixtures.kelvin_to_rgb(3000)
        else:
            if v.get("ctc", 0) >= 21:
                k = fixtures.ctc_to_kelvin(v["ctc"])
                r, g, b = fixtures.kelvin_to_rgb(k)
                level = max(v.get("r", 0), v.get("g", 0), v.get("b", 0), v.get("w", 0)) / 255.0
                r, g, b = r * level, g * level, b * level
            else:
                wr, wg, wb = fixtures.kelvin_to_rgb(6500)
                w = v.get("w", 0) / 255.0
                r = min(1.0, v.get("r", 0) / 255.0 + w * wr)
                g = min(1.0, v.get("g", 0) / 255.0 + w * wg)
                b = min(1.0, v.get("b", 0) / 255.0 + w * wb)
        return [int(r * dim * 255), int(g * dim * 255), int(b * dim * 255)]


class EBoxSimulator:
    def __init__(self, host: str = "127.0.0.1", port: int = 6455, net: int = 0, subnet: int = 0,
                 universe: int = 0, name: str = "E-box Remote (SIM)", modules: Optional[List[VirtualModule]] = None,
                 rdm_enabled: bool = True, tw_has_mode7: bool = False):
        self.host, self.port = host, port
        self.net, self.subnet, self.universe = net, subnet, universe
        self.name = name
        self.rdm_enabled = rdm_enabled
        self.tw_has_mode7 = tw_has_mode7
        self.modules: List[VirtualModule] = modules if modules is not None else []
        self.last_dmx = bytes(512)
        self.last_dmx_time = 0.0
        self.dmx_frames = 0
        self.special_history: List[tuple] = []
        self._stop = threading.Event()
        self.sock: Optional[socket.socket] = None
        self.lock = threading.RLock()

    @property
    def pa(self) -> int:
        return artnet.port_address(self.net, self.subnet, self.universe)

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.settimeout(0.2)
        self.sock = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._loop, name="ebox-sim", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        time.sleep(0.25)
        if self.sock:
            self.sock.close()

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._handle(data, addr)
            except Exception:
                import traceback
                traceback.print_exc()

    def module(self, uid: bytes) -> Optional[VirtualModule]:
        for m in self.modules:
            if m.uid == uid:
                return m
        return None

    def _handle(self, data: bytes, addr):
        op = artnet.opcode(data)
        if op == artnet.OP_POLL:
            reply = artnet.build_poll_reply(self.host if self.host != "0.0.0.0" else "127.0.0.1",
                                            self.net, self.subnet, [self.universe], "E-box Remote", self.name,
                                            mac=b"\x00\x50\xc2\x12\x34\x56")
            self.sock.sendto(reply, addr)
        elif op == artnet.OP_DMX:
            p = artnet.parse_dmx(data)
            if p and p["port_address"] == self.pa:
                now = time.time()
                with self.lock:
                    self.last_dmx = bytes(p["data"]).ljust(512, b"\x00")
                    self.last_dmx_time = now
                    self.dmx_frames += 1
                    for m in self.modules:
                        m.feed(self.last_dmx, now)
        elif op == artnet.OP_TOD_REQUEST and self.rdm_enabled:
            pas = artnet.parse_tod_request(data) or []
            if self.pa in pas:
                self._send_tod(addr)
        elif op == artnet.OP_TOD_CONTROL and self.rdm_enabled:
            p = artnet.parse_tod_control(data)
            if p and p["port_address"] == self.pa:
                self._send_tod(addr)
        elif op == artnet.OP_RDM and self.rdm_enabled:
            p = artnet.parse_rdm(data)
            if not p or p["port_address"] != self.pa:
                return
            msg = rdm.parse(p["rdm"])
            if not msg or not msg.checksum_ok:
                return
            m = self.module(msg.dest)
            if not m:
                return
            resp = self._respond(m, msg)
            if resp is not None:
                self.sock.sendto(artnet.build_rdm(self.pa, resp), addr)

    def _send_tod(self, addr):
        uids = [m.uid for m in self.modules]
        blocks = [uids[i:i + 200] for i in range(0, len(uids), 200)] or [[]]
        for i, b in enumerate(blocks):
            self.sock.sendto(artnet.build_tod_data(self.pa, b, block=i, total=len(uids)), addr)

    def _respond(self, m: VirtualModule, msg: rdm.Message) -> Optional[bytes]:
        def reply(pd=b"", rtype=rdm.ACK):
            return rdm.build(msg.src, m.uid, msg.tn, rtype, msg.cc + 1, msg.pid, pd)

        def nack(code):
            return reply(struct.pack(">H", code), rdm.NACK)

        pers = m.personalities
        with self.lock:
            if msg.cc == rdm.GET:
                if msg.pid == rdm.PID_DEVICE_INFO:
                    return reply(rdm.encode_device_info(0x0C01, 0x0101, 0x00010203, m.footprint(),
                                                        m.mode, max(pers), m.address))
                if msg.pid == rdm.PID_DEVICE_LABEL:
                    return reply(m.label.encode()[:32])
                if msg.pid == rdm.PID_DEVICE_MODEL_DESCRIPTION:
                    return reply(("Calumma XS %s" % m.variant).encode())
                if msg.pid == rdm.PID_MANUFACTURER_LABEL:
                    return reply(b"Robe Lighting")
                if msg.pid == rdm.PID_SOFTWARE_VERSION_LABEL:
                    return reply(b"1.3 (sim)")
                if msg.pid == rdm.PID_DMX_START_ADDRESS:
                    return reply(struct.pack(">H", m.address))
                if msg.pid == rdm.PID_DMX_PERSONALITY:
                    return reply(bytes([m.mode, max(pers)]))
                if msg.pid == rdm.PID_DMX_PERSONALITY_DESCRIPTION:
                    n = msg.pd[0] if msg.pd else 0
                    if n not in pers:
                        return nack(0x0006)
                    v = m.variant if n in fixtures.MODES[m.variant] else "RGBW"
                    info = fixtures.mode_info(v, n)
                    return reply(bytes([n]) + struct.pack(">H", len(info["roles"])) + info["name"][:32].encode())
                if msg.pid == rdm.PID_IDENTIFY_DEVICE:
                    return reply(bytes([1 if m.identify else 0]))
                if msg.pid == rdm.PID_SUPPORTED_PARAMETERS:
                    pids = [rdm.PID_DEVICE_LABEL, rdm.PID_DMX_PERSONALITY, rdm.PID_DMX_PERSONALITY_DESCRIPTION,
                            rdm.PID_PARAMETER_DESCRIPTION] + list(MFR_PARAMS)
                    return reply(b"".join(struct.pack(">H", x) for x in pids))
                if msg.pid == rdm.PID_PARAMETER_DESCRIPTION:
                    pid = struct.unpack(">H", msg.pd[:2])[0] if len(msg.pd) >= 2 else 0
                    if pid not in MFR_PARAMS:
                        return nack(0x0006)
                    name, size, dt, cc, lo, hi, df = MFR_PARAMS[pid]
                    return reply(rdm.encode_parameter_description(pid, size, dt, cc, lo, hi, df, name))
                if msg.pid == PID_INIT_POSITION:
                    return reply(bytes([0]))
                if msg.pid == PID_TERMINATOR:
                    return reply(bytes([m.terminator]))
                return nack(0x0000)
            if msg.cc == rdm.SET:
                if msg.pid == PID_INIT_POSITION:
                    if msg.pd and msg.pd[0] == 1:
                        m.saved_initial = bytes(m.dmx)
                        m.saved_mode = m.mode
                        m.saved_via = "rdm"
                    return reply()
                if msg.pid == PID_TERMINATOR:
                    m.terminator = msg.pd[0] if msg.pd else 0
                    return reply()
                if msg.pid == rdm.PID_DMX_START_ADDRESS:
                    a = struct.unpack(">H", msg.pd[:2])[0] if len(msg.pd) >= 2 else 0
                    if not 1 <= a <= 512:
                        return nack(0x0006)
                    m.address = a
                    return reply()
                if msg.pid == rdm.PID_DMX_PERSONALITY:
                    n = msg.pd[0] if msg.pd else 0
                    if n not in pers:
                        return nack(0x0006)
                    m.mode = n
                    return reply()
                if msg.pid == rdm.PID_DEVICE_LABEL:
                    m.label = msg.pd.decode("latin-1")[:32]
                    return reply()
                if msg.pid == rdm.PID_IDENTIFY_DEVICE:
                    m.identify = bool(msg.pd and msg.pd[0])
                    return reply()
                return nack(0x0000)
        return None

    def state(self) -> dict:
        with self.lock:
            return {"name": self.name, "ip": self.host, "port": self.port, "port_address": self.pa,
                    "net": self.net, "subnet": self.subnet, "universe": self.universe,
                    "rdm_enabled": self.rdm_enabled, "dmx_frames": self.dmx_frames,
                    "receiving": (time.time() - self.last_dmx_time) < 1.0,
                    "modules": [m.to_dict() for m in self.modules]}


def demo_box(port: int = 6455) -> EBoxSimulator:
    """Mixed float: 4 tunable white + 2 RGBW modules, some unaddressed (all at 1) like out of the box."""
    mods = [
        VirtualModule(0x00A10001, "TW", 11, 1, ""),
        VirtualModule(0x00A10002, "TW", 11, 4, ""),
        VirtualModule(0x00A10003, "TW", 11, 7, ""),
        VirtualModule(0x00A10004, "TW", 11, 1, ""),   # duplicate address: needs fixing on site
        VirtualModule(0x00A10005, "RGBW", 1, 20, ""),
        VirtualModule(0x00A10006, "RGBW", 1, 24, ""),
    ]
    return EBoxSimulator(port=port, modules=mods)
