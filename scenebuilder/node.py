"""Art-Net controller: discovery, continuous DMX output, and RDM over Art-Net (ArtRdm)."""
from __future__ import annotations

import logging
import platform
import re
import socket
import struct
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import artnet, rdm

log = logging.getLogger("scenebuilder.node")

Target = Tuple[str, int, int]  # (ip, udp port, port-address)


class RdmError(Exception):
    pass


def _broadcast_for(ip: str, mask: str) -> Optional[str]:
    try:
        ipn = struct.unpack(">I", socket.inet_aton(ip))[0]
        mn = struct.unpack(">I", socket.inet_aton(mask))[0]
        return socket.inet_ntoa(struct.pack(">I", (ipn & mn) | (~mn & 0xFFFFFFFF)))
    except OSError:
        return None


def _unix_interfaces() -> List[dict]:
    """IPv4 interfaces from `ifconfig` (macOS/Linux). Each: name, ip, netmask, broadcast."""
    out = []
    try:
        text = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return out
    name = None
    for line in text.splitlines():
        m = re.match(r"^([a-zA-Z0-9]+):", line)
        if m:
            name = m.group(1)
            continue
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+|\d+\.\d+\.\d+\.\d+)(?: broadcast (\d+\.\d+\.\d+\.\d+))?", line)
        if m and name:
            ip, mask, bcast = m.group(1), m.group(2), m.group(3)
            if mask.startswith("0x"):
                n = int(mask, 16)
                mask = ".".join(str((n >> s) & 0xFF) for s in (24, 16, 8, 0))
            if not bcast and ip != "127.0.0.1":
                bcast = _broadcast_for(ip, mask)
            out.append({"name": name, "ip": ip, "netmask": mask, "broadcast": bcast})
    return out


_MASK_OCTETS = {"0", "128", "192", "224", "240", "248", "252", "254", "255"}


def _looks_like_mask(ip: str) -> bool:
    parts = ip.split(".")
    return len(parts) == 4 and all(p in _MASK_OCTETS for p in parts) and ip != "0.0.0.0"


def _parse_ipconfig(text: str) -> List[dict]:
    """Parse `ipconfig /all` text into [{name, ip, netmask, broadcast}, ...].

    Tries the English field labels first (fast, exact). If that finds nothing, e.g. on a
    non-English Windows install, falls back to a label-independent pass: every blank-line
    separated block that has a header line ending in ':' and contains a plausible IPv4
    address followed later by a plausible subnet mask is treated as an adapter."""
    out = []
    blocks = re.split(r"\r?\n(?=\S)", text)
    for block in blocks:
        first = block.splitlines()[0] if block else ""
        m = re.match(r"^(.*? adapter .*?):\s*$", first, re.M)
        if not m:
            continue
        name = m.group(1).strip()
        ip_m = re.search(r"IPv4 Address[.\s]*:\s*([\d.]+)", block)
        mask_m = re.search(r"Subnet Mask[.\s]*:\s*([\d.]+)", block)
        if ip_m and mask_m:
            ip, mask = ip_m.group(1), mask_m.group(1)
            out.append({"name": name, "ip": ip, "netmask": mask, "broadcast": _broadcast_for(ip, mask)})
    if out:
        return out
    for block in blocks:
        lines = block.splitlines()
        first = lines[0] if lines else ""
        if not re.match(r"^\S.*:\s*$", first):
            continue  # adapter headers start at column 0 and end with a bare colon
        ips = re.findall(r":\s*(\d{1,3}(?:\.\d{1,3}){3})\s*(?:\([^)]*\))?\s*$", block, re.M)
        addr = next((x for x in ips if not _looks_like_mask(x) and not x.startswith("0.")), None)
        mask = next((x for x in ips if _looks_like_mask(x)), None)
        if addr and mask:
            name = first.rstrip(": \t")
            out.append({"name": name, "ip": addr, "netmask": mask, "broadcast": _broadcast_for(addr, mask)})
    return out


def _windows_interfaces() -> List[dict]:
    """IPv4 interfaces from `ipconfig /all`. Each: name, ip, netmask, broadcast."""
    try:
        # cp437/oem encoding varies by locale; decode leniently rather than raise.
        raw = subprocess.run(["ipconfig", "/all"], capture_output=True, timeout=10).stdout
        text = raw.decode("oem", "replace") if isinstance(raw, bytes) else raw
    except Exception:
        return []
    return _parse_ipconfig(text)


def local_interfaces() -> List[dict]:
    """This machine's IPv4 network adapters. Each: name, ip, netmask, broadcast."""
    return _windows_interfaces() if platform.system() == "Windows" else _unix_interfaces()


def _hosts(ip: str, mask: str, limit: int = 1022) -> List[str]:
    """Every host address on ip/mask, if the network is small enough to poll one by one."""
    try:
        ipn = struct.unpack(">I", socket.inet_aton(ip))[0]
        mn = struct.unpack(">I", socket.inet_aton(mask))[0]
    except OSError:
        return []
    size = (~mn & 0xFFFFFFFF) + 1
    if size - 2 > limit or size < 4:
        return []
    net = ipn & mn
    return [socket.inet_ntoa(struct.pack(">I", net + i)) for i in range(1, size - 1) if net + i != ipn]


class ArtNetController:
    def __init__(self, bind_ip: str = "0.0.0.0", port: int = artnet.ARTNET_PORT, fps: float = 30.0):
        # The listening socket always binds to 0.0.0.0 (see start()): binding to one adapter's
        # unicast address stops the OS delivering broadcast replies to it on macOS/Linux, which
        # would silently break discovery. `bind_ip` is kept only for tests / advanced callers.
        self.bind_ip = bind_ip or "0.0.0.0"
        self.port = port
        self.fps = fps
        self.sock: Optional[socket.socket] = None
        self.bind_error: Optional[str] = None
        self._lock = threading.RLock()
        self.preferred_interface: Optional[str] = None  # adapter IP to prefer for discovery, or None
        self._buffers: Dict[Target, bytearray] = {}
        self._seq: Dict[Target, int] = {}
        self.output_enabled = False
        self.nodes: Dict[str, dict] = {}
        self._tod: Dict[Tuple[str, int], Dict[int, List[bytes]]] = {}
        self._tod_total: Dict[Tuple[str, int], int] = {}
        self._rdm_waiters: List[dict] = []
        self._tn = 0
        self._rdm_lock = threading.Lock()  # one RDM transaction at a time (boxes are slow)
        self._stop = threading.Event()
        self.packets_sent = 0
        self.packets_received = 0
        self.last_send_error: Optional[str] = None
        self.rdm_log: List[dict] = []
        self.raw_rdm_log: List[dict] = []  # temporary field diagnostics: every OP_RDM packet in, matched or not

    # ------------------------------------------------------------ lifecycle
    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        # No SO_REUSEPORT: if another Art-Net app holds 6454 we want to know, not silently lose replies.
        # On Windows SO_REUSEADDR would let a second copy (or xLights) bind the same port silently.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind((self.bind_ip, self.port))
        except OSError as e:
            self.bind_error = "Could not open Art-Net port %d (%s). Is another lighting app open?" % (self.port, e)
            log.warning(self.bind_error)
            s.bind((self.bind_ip, 0))
        s.settimeout(0.2)
        self.sock = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._rx_loop, name="artnet-rx", daemon=True).start()
        threading.Thread(target=self._tx_loop, name="artnet-tx", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        time.sleep(0.25)
        if self.sock:
            self.sock.close()

    def set_preferred_interface(self, ip: Optional[str]):
        """Prefer one adapter for discovery broadcasts (Setup > Network adapter). The listening
        socket is unaffected: it always stays on 0.0.0.0 so replies are never missed, and unicast
        sends to an already-known box IP are unaffected too (the OS routing table already gets
        those right for a directly-connected subnet)."""
        with self._lock:
            self.preferred_interface = ip or None

    # ------------------------------------------------------------ sending
    def _send(self, data: bytes, ip: str, port: int) -> bool:
        try:
            self.sock.sendto(data, (ip, port))
            self.packets_sent += 1
            self.last_send_error = None
            return True
        except OSError as e:
            self.last_send_error = "%s:%d - %s" % (ip, port, e)
            return False

    def set_universe(self, target: Target, data: bytes):
        with self._lock:
            buf = self._buffers.setdefault(target, bytearray(512))
            buf[:] = bytes(data[:512]).ljust(512, b"\x00")

    def get_universe(self, target: Target) -> bytes:
        with self._lock:
            return bytes(self._buffers.get(target, bytes(512)))

    def clear_targets(self):
        with self._lock:
            self._buffers.clear()

    def set_targets(self, frames: Dict[Target, bytes]):
        """Replace the whole output set atomically."""
        with self._lock:
            self._buffers = {t: bytearray(bytes(d[:512]).ljust(512, b"\x00")) for t, d in frames.items()}

    def send_now(self):
        with self._lock:
            items = [(t, bytes(b)) for t, b in self._buffers.items()]
        for (ip, port, pa), data in items:
            seq = self._seq.get((ip, port, pa), 0) % 255 + 1
            self._seq[(ip, port, pa)] = seq
            self._send(artnet.build_dmx(pa, data, seq), ip, port)

    def _tx_loop(self):
        period = 1.0 / self.fps
        nxt = time.monotonic()
        while not self._stop.is_set():
            if self.output_enabled:
                self.send_now()
            nxt += period
            delay = nxt - time.monotonic()
            if delay < -1:
                nxt = time.monotonic()
            elif delay > 0:
                time.sleep(delay)

    # ------------------------------------------------------------ receiving
    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            self.packets_received += 1
            try:
                self._handle(data, addr)
            except Exception:  # never let a bad packet kill the receiver
                log.exception("bad packet from %s", addr)

    def _handle(self, data: bytes, addr):
        op = artnet.opcode(data)
        if op == artnet.OP_POLL_REPLY:
            pr = artnet.parse_poll_reply(data)
            if pr:
                d = pr.to_dict()
                d["from"] = "%s:%d" % addr
                d["udp_port"] = addr[1]
                d["seen"] = time.time()
                key = "%s:%d/%d" % (addr[0], addr[1], pr.bind_index)
                with self._lock:
                    self.nodes[key] = d
        elif op == artnet.OP_TOD_DATA:
            td = artnet.parse_tod_data(data)
            if td:
                key = (addr[0], td.port_address)
                with self._lock:
                    self._tod.setdefault(key, {})[td.block] = td.uids
                    self._tod_total[key] = td.total
        elif op == artnet.OP_RDM:
            p = artnet.parse_rdm(data)
            if not p:
                with self._lock:
                    self.raw_rdm_log.append({"t": time.time(), "from": "%s:%d" % addr, "note": "OP_RDM but parse_rdm() rejected it", "hex": data.hex()})
                    del self.raw_rdm_log[:-40]
                return
            msg = rdm.parse(p["rdm"])
            with self._lock:
                self.raw_rdm_log.append({"t": time.time(), "from": "%s:%d" % addr, "port_address": p["port_address"],
                                         "rdm_hex": p["rdm"].hex(), "parsed": None if not msg else
                                         {"dest": rdm.uid_str(msg.dest), "src": rdm.uid_str(msg.src), "tn": msg.tn,
                                          "response_type": msg.response_type, "cc": msg.cc, "pid": "0x%04X" % msg.pid,
                                          "checksum_ok": msg.checksum_ok, "pd_hex": msg.pd.hex()}})
                del self.raw_rdm_log[:-40]
            if not msg:
                return
            with self._lock:
                for w in self._rdm_waiters:
                    if (w["ip"] == addr[0] and msg.src == w["uid"] and msg.tn == w["tn"]
                            and msg.cc == w["cc"] + 1 and msg.pid == w["pid"]):
                        w["msg"] = msg
                        w["event"].set()

    # ------------------------------------------------------------ discovery
    def poll(self, extra_targets: List[Tuple[str, int]] = (), wait: float = 2.0) -> List[dict]:
        pkt = artnet.build_poll()
        # Directed broadcast per interface (2.255.255.255 on a Robe-default Ethernet port);
        # 255.255.255.255 would only leave via the primary interface on macOS.
        dests = set()
        itfs = local_interfaces()
        preferred = self.preferred_interface
        if preferred and preferred not in {i["ip"] for i in itfs}:
            preferred = None  # the pinned adapter is unplugged right now: search on every adapter instead
        for itf in itfs:
            if preferred and itf["ip"] != preferred:
                continue  # a specific adapter is preferred: only broadcast on it
            is_link_local = itf["ip"].startswith("169.254.")
            if itf.get("broadcast") and not itf["ip"].startswith("127.") and (not is_link_local or itf["ip"] == preferred):
                dests.add((itf["broadcast"], artnet.ARTNET_PORT))
                # Also ask every address on small networks directly: a box whose netmask differs from
                # the network's (e.g. Robe's factory 255.0.0.0 on a 255.255.255.0 router) ignores the
                # broadcast but always answers a poll sent straight to it.
                for host in _hosts(itf["ip"], itf.get("netmask") or ""):
                    dests.add((host, artnet.ARTNET_PORT))
        for t in extra_targets:
            dests.add((t[0], int(t[1])))
        started = time.time()
        for d in dests:
            self._send(pkt, d[0], d[1])
        time.sleep(wait)
        with self._lock:
            return [n for n in self.nodes.values() if n["seen"] >= started]

    # ------------------------------------------------------------ RDM
    def tod(self, ip: str, port: int, pa: int, flush: bool = False, wait: float = 3.0) -> List[bytes]:
        key = (ip, pa)
        with self._lock:
            self._tod.pop(key, None)
            self._tod_total.pop(key, None)
        if flush:
            self._send(artnet.build_tod_control(pa), ip, port)
            time.sleep(min(wait, 4.0))
        self._send(artnet.build_tod_request(pa), ip, port)
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(0.1)
            with self._lock:
                blocks = self._tod.get(key)
                total = self._tod_total.get(key)
                if blocks is not None and total is not None and sum(len(u) for u in blocks.values()) >= total:
                    break
        with self._lock:
            blocks = self._tod.get(key)
            if blocks is None:
                raise RdmError("The box at %s did not answer an RDM device-list request (ArtTodRequest)." % ip)
            uids = []
            for b in sorted(blocks):
                uids.extend(blocks[b])
            seen, out = set(), []
            for u in uids:
                if u not in seen:
                    seen.add(u)
                    out.append(u)
            return out

    def rdm(self, ip: str, port: int, pa: int, uid: bytes, cc: int, pid: int, pd: bytes = b"",
            timeout: float = 1.0, retries: int = 2) -> rdm.Message:
        with self._rdm_lock:
            overflow_pd = b""
            attempts = 0
            while True:
                self._tn = (self._tn + 1) & 0xFF
                w = {"ip": ip, "uid": uid, "tn": self._tn, "cc": cc, "pid": pid, "event": threading.Event(), "msg": None}
                with self._lock:
                    self._rdm_waiters.append(w)
                try:
                    self._send(artnet.build_rdm(pa, rdm.build_request(uid, self._tn, cc, pid, pd)), ip, port)
                    got = w["event"].wait(timeout)
                finally:
                    with self._lock:
                        self._rdm_waiters.remove(w)
                msg = w["msg"]
                self._log_rdm(ip, uid, cc, pid, pd, msg)
                if not got or msg is None:
                    attempts += 1
                    if attempts > retries:
                        raise RdmError("No RDM reply from %s (%s)." % (rdm.uid_str(uid), _pid_name(pid)))
                    continue
                if not msg.checksum_ok:
                    attempts += 1
                    if attempts > retries:
                        raise RdmError("Corrupt RDM reply from %s." % rdm.uid_str(uid))
                    continue
                if msg.response_type == rdm.ACK:
                    if overflow_pd:
                        msg.pd = overflow_pd + msg.pd
                    return msg
                if msg.response_type == rdm.ACK_OVERFLOW:
                    overflow_pd += msg.pd
                    attempts = 0
                    continue
                if msg.response_type == rdm.ACK_TIMER:
                    time.sleep(min(5.0, max(0.1, msg.ack_timer_seconds())))
                    attempts += 1
                    if attempts > retries + 3:
                        raise RdmError("%s kept saying 'busy' (%s)." % (rdm.uid_str(uid), _pid_name(pid)))
                    continue
                if msg.response_type == rdm.NACK:
                    raise RdmError("%s refused %s: %s." % (rdm.uid_str(uid), _pid_name(pid), msg.nack_reason()))
                raise RdmError("Unexpected RDM response type %d." % msg.response_type)

    def _log_rdm(self, ip, uid, cc, pid, pd, msg):
        entry = {"t": time.time(), "ip": ip, "uid": rdm.uid_str(uid), "cc": "GET" if cc == rdm.GET else "SET",
                 "pid": _pid_name(pid), "reply": None}
        if msg is not None:
            entry["reply"] = {0: "ACK", 1: "ACK_TIMER", 2: "NACK", 3: "ACK_OVERFLOW"}.get(msg.response_type, "?")
        self.rdm_log.append(entry)
        del self.rdm_log[:-200]

    # convenience wrappers
    def device_info(self, ip, port, pa, uid) -> dict:
        return rdm.decode_device_info(self.rdm(ip, port, pa, uid, rdm.GET, rdm.PID_DEVICE_INFO).pd)

    def get_text(self, ip, port, pa, uid, pid) -> Optional[str]:
        try:
            return rdm.decode_text(self.rdm(ip, port, pa, uid, rdm.GET, pid).pd)
        except RdmError:
            return None

    def personality_description(self, ip, port, pa, uid, n) -> Optional[dict]:
        try:
            return rdm.decode_personality_description(
                self.rdm(ip, port, pa, uid, rdm.GET, rdm.PID_DMX_PERSONALITY_DESCRIPTION, bytes([n])).pd)
        except (RdmError, ValueError):
            return None

    def supported_parameters(self, ip, port, pa, uid) -> list:
        return rdm.decode_supported_parameters(
            self.rdm(ip, port, pa, uid, rdm.GET, rdm.PID_SUPPORTED_PARAMETERS).pd)

    def parameter_description(self, ip, port, pa, uid, pid) -> dict:
        return rdm.decode_parameter_description(
            self.rdm(ip, port, pa, uid, rdm.GET, rdm.PID_PARAMETER_DESCRIPTION, struct.pack(">H", pid)).pd)

    def get_param(self, ip, port, pa, uid, pid, size, signed=False):
        return rdm.decode_value(self.rdm(ip, port, pa, uid, rdm.GET, pid).pd, size, signed)

    def set_param(self, ip, port, pa, uid, pid, value, size, signed=False):
        self.rdm(ip, port, pa, uid, rdm.SET, pid, rdm.encode_value(value, size, signed))

    def set_address(self, ip, port, pa, uid, address: int):
        if not 1 <= address <= 512:
            raise RdmError("Address must be 1-512")
        self.rdm(ip, port, pa, uid, rdm.SET, rdm.PID_DMX_START_ADDRESS, struct.pack(">H", address))

    def set_personality(self, ip, port, pa, uid, mode: int):
        self.rdm(ip, port, pa, uid, rdm.SET, rdm.PID_DMX_PERSONALITY, bytes([mode]))

    def set_label(self, ip, port, pa, uid, label: str):
        self.rdm(ip, port, pa, uid, rdm.SET, rdm.PID_DEVICE_LABEL, label.encode("ascii", "replace")[:32])

    def identify(self, ip, port, pa, uid, on: bool):
        self.rdm(ip, port, pa, uid, rdm.SET, rdm.PID_IDENTIFY_DEVICE, bytes([1 if on else 0]))


_PID_NAMES = {v: k[4:] for k, v in vars(rdm).items() if k.startswith("PID_")}


def _pid_name(pid: int) -> str:
    return _PID_NAMES.get(pid, "PID 0x%04X" % pid)
