"""Art-Net 4 packet encode/decode (the subset DMX Scene Builder needs).

Port-Address is the 15-bit Art-Net address: Net (7 bits) << 8 | Sub-Net (4 bits) << 4 | Universe (4 bits).
All multi-byte fields are little-endian except where the spec says Hi/Lo (ProtVer, Length, UidTotal).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional

ARTNET_PORT = 6454
HEADER = b"Art-Net\x00"
PROT_VER = 14

OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
OP_DMX = 0x5000
OP_TOD_REQUEST = 0x8000
OP_TOD_DATA = 0x8100
OP_TOD_CONTROL = 0x8200
OP_RDM = 0x8300

TOD_FULL = 0x00
TOD_NAK = 0xFF
ATC_FLUSH = 0x01


def port_address(net: int, subnet: int, universe: int) -> int:
    if not (0 <= net <= 127 and 0 <= subnet <= 15 and 0 <= universe <= 15):
        raise ValueError("net 0-127, subnet 0-15, universe 0-15")
    return (net << 8) | (subnet << 4) | universe


def split_port_address(pa: int):
    return (pa >> 8) & 0x7F, (pa >> 4) & 0x0F, pa & 0x0F


def opcode(data: bytes) -> Optional[int]:
    if len(data) < 10 or not data.startswith(HEADER):
        return None
    return struct.unpack_from("<H", data, 8)[0]


def _head(op: int) -> bytes:
    return HEADER + struct.pack("<H", op) + struct.pack(">H", PROT_VER)


# ---------------------------------------------------------------- ArtPoll

def build_poll(flags: int = 0x00) -> bytes:
    # Flags, DiagPriority, then Art-Net 4 targeted-mode fields (unused, zero), EstaMan, Oem
    return _head(OP_POLL) + bytes([flags, 0x10]) + b"\x00" * 8


# ---------------------------------------------------------------- ArtPollReply

@dataclass
class PollReply:
    ip: str
    port: int
    firmware: int
    net: int
    subnet: int
    oem: int
    esta: int
    short_name: str
    long_name: str
    node_report: str
    num_ports: int
    port_types: List[int]
    sw_in: List[int]
    sw_out: List[int]
    good_output: List[int]
    mac: str
    bind_ip: str
    bind_index: int
    status2: int
    style: int
    status1: int = 0

    @property
    def output_port_addresses(self) -> List[int]:
        out = []
        for i in range(min(self.num_ports, 4)):
            if self.port_types[i] & 0x80:  # can output data from Art-Net
                out.append((self.net << 8) | (self.subnet << 4) | (self.sw_out[i] & 0x0F))
        return out

    @property
    def rdm_capable(self) -> bool:
        return bool(self.status1 & 0x02)  # Status1 bit 1: capable of RDM

    def to_dict(self):
        d = self.__dict__.copy()
        d["output_port_addresses"] = self.output_port_addresses
        d["rdm_capable"] = self.rdm_capable
        return d


def _cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()


def parse_poll_reply(data: bytes) -> Optional[PollReply]:
    if opcode(data) != OP_POLL_REPLY or len(data) < 207:
        return None
    ip = ".".join(str(x) for x in data[10:14])
    port = struct.unpack_from("<H", data, 14)[0]
    firmware = struct.unpack_from(">H", data, 16)[0]
    net, subnet = data[18] & 0x7F, data[19] & 0x0F
    oem = struct.unpack_from(">H", data, 20)[0]
    esta = struct.unpack_from("<H", data, 24)[0]
    num_ports = struct.unpack_from(">H", data, 172)[0]
    mac = ":".join("%02x" % x for x in data[201:207])
    bind_ip = ".".join(str(x) for x in data[207:211]) if len(data) >= 211 else ip
    bind_index = data[211] if len(data) > 211 else 0
    status2 = data[212] if len(data) > 212 else 0
    return PollReply(
        ip=ip, port=port, firmware=firmware, net=net, subnet=subnet, oem=oem, esta=esta,
        short_name=_cstr(data[26:44]), long_name=_cstr(data[44:108]), node_report=_cstr(data[108:172]),
        num_ports=num_ports, port_types=list(data[174:178]), sw_in=list(data[186:190]),
        sw_out=list(data[190:194]), good_output=list(data[182:186]), mac=mac, bind_ip=bind_ip,
        bind_index=bind_index, status2=status2, style=data[200], status1=data[23],
    )


def build_poll_reply(ip: str, net: int, subnet: int, universes: List[int], short_name: str,
                     long_name: str, mac: bytes = b"\x00" * 6, esta: int = 0x5253, oem: int = 0,
                     bind_index: int = 1, node_report: str = "#0001 [0000] OK") -> bytes:
    """Used by the simulator. universes = list of 4-bit universe numbers (max 4)."""
    b = bytearray(239)
    b[0:8] = HEADER
    struct.pack_into("<H", b, 8, OP_POLL_REPLY)
    b[10:14] = bytes(int(x) for x in ip.split("."))
    struct.pack_into("<H", b, 14, ARTNET_PORT)
    struct.pack_into(">H", b, 16, 0x0100)
    b[18], b[19] = net & 0x7F, subnet & 0x0F
    struct.pack_into(">H", b, 20, oem)
    b[23] = 0xE0 | 0x02  # Status1: indicators normal, RDM capable
    struct.pack_into("<H", b, 24, esta)
    sn, ln = short_name.encode()[:17], long_name.encode()[:63]
    b[26:26 + len(sn)] = sn
    b[44:44 + len(ln)] = ln
    nr = node_report.encode()[:63]
    b[108:108 + len(nr)] = nr
    n = min(len(universes), 4)
    struct.pack_into(">H", b, 172, n)
    for i in range(n):
        b[174 + i] = 0x80  # output from Art-Net, DMX512
        b[182 + i] = 0x80  # data is being output
        b[190 + i] = universes[i] & 0x0F
    b[200] = 0x00  # StNode
    b[201:207] = mac[:6]
    b[207:211] = b[10:14]
    b[211] = bind_index
    b[212] = 0x08  # Status2: 15-bit port-address supported
    return bytes(b)


# ---------------------------------------------------------------- ArtDmx

def build_dmx(pa: int, data: bytes, sequence: int = 0, physical: int = 0) -> bytes:
    n = len(data)
    if n < 2:
        data = bytes(data) + b"\x00" * (2 - n)
    elif n % 2:
        data = bytes(data) + b"\x00"
    data = bytes(data[:512])
    return (_head(OP_DMX) + bytes([sequence & 0xFF, physical, pa & 0xFF, (pa >> 8) & 0x7F])
            + struct.pack(">H", len(data)) + data)


def parse_dmx(data: bytes):
    if opcode(data) != OP_DMX or len(data) < 18:
        return None
    seq, phys, sub, net = data[12], data[13], data[14], data[15]
    length = struct.unpack_from(">H", data, 16)[0]
    return {"sequence": seq, "physical": phys, "port_address": (net << 8) | sub,
            "data": data[18:18 + length]}


# ---------------------------------------------------------------- RDM over Art-Net

def build_tod_request(pa: int) -> bytes:
    return (_head(OP_TOD_REQUEST) + b"\x00\x00" + b"\x00" * 7
            + bytes([(pa >> 8) & 0x7F, TOD_FULL, 1, pa & 0xFF]))


def parse_tod_request(data: bytes):
    if opcode(data) != OP_TOD_REQUEST or len(data) < 24:
        return None
    net, count = data[21], data[23]
    return [(net << 8) | a for a in data[24:24 + count]]


def build_tod_control(pa: int, command: int = ATC_FLUSH) -> bytes:
    return (_head(OP_TOD_CONTROL) + b"\x00\x00" + b"\x00" * 7
            + bytes([(pa >> 8) & 0x7F, command, pa & 0xFF]))


def parse_tod_control(data: bytes):
    if opcode(data) != OP_TOD_CONTROL or len(data) < 24:
        return None
    return {"port_address": (data[21] << 8) | data[23], "command": data[22]}


def build_tod_data(pa: int, uids: List[bytes], block: int = 0, total: Optional[int] = None,
                   port: int = 1, bind_index: int = 1) -> bytes:
    total = len(uids) if total is None else total
    return (_head(OP_TOD_DATA) + bytes([0x01, port]) + b"\x00" * 6
            + bytes([bind_index, (pa >> 8) & 0x7F, TOD_FULL, pa & 0xFF])
            + struct.pack(">H", total) + bytes([block, len(uids)]) + b"".join(uids))


@dataclass
class TodData:
    port_address: int
    total: int
    block: int
    uids: List[bytes] = field(default_factory=list)
    bind_index: int = 0


def parse_tod_data(data: bytes) -> Optional[TodData]:
    if opcode(data) != OP_TOD_DATA or len(data) < 28:
        return None
    pa = ((data[21] & 0x7F) << 8) | data[23]
    total = struct.unpack_from(">H", data, 24)[0]
    block, count = data[26], data[27]
    uids = [bytes(data[28 + 6 * i:34 + 6 * i]) for i in range(count) if 34 + 6 * i <= len(data)]
    return TodData(pa, total, block, uids, data[20])


def build_rdm(pa: int, rdm_packet_without_startcode: bytes) -> bytes:
    return (_head(OP_RDM) + bytes([0x01, 0x00]) + b"\x00" * 7
            + bytes([(pa >> 8) & 0x7F, 0x00, pa & 0xFF]) + rdm_packet_without_startcode)


def parse_rdm(data: bytes):
    if opcode(data) != OP_RDM or len(data) < 25:
        return None
    return {"port_address": ((data[21] & 0x7F) << 8) | data[23], "rdm": data[24:]}
