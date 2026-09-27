"""ANSI E1.20 RDM message encode/decode (controller side + responder helpers for the simulator).

Packets here EXCLUDE the 0xCC start code (that is how Art-Net's ArtRdm carries them), but the
checksum is computed as if the start code were present, per E1.20.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

START_CODE = 0xCC
SUB_START = 0x01

GET = 0x20
GET_RESPONSE = 0x21
SET = 0x30
SET_RESPONSE = 0x31

ACK = 0x00
ACK_TIMER = 0x01
NACK = 0x02
ACK_OVERFLOW = 0x03

PID_SUPPORTED_PARAMETERS = 0x0050
PID_PARAMETER_DESCRIPTION = 0x0051
PID_DEVICE_INFO = 0x0060
PID_DEVICE_MODEL_DESCRIPTION = 0x0080
PID_MANUFACTURER_LABEL = 0x0081
PID_DEVICE_LABEL = 0x0082
PID_SOFTWARE_VERSION_LABEL = 0x00C0
PID_DMX_PERSONALITY = 0x00E0
PID_DMX_PERSONALITY_DESCRIPTION = 0x00E1
PID_DMX_START_ADDRESS = 0x00F0
PID_IDENTIFY_DEVICE = 0x1000

NACK_REASONS = {
    0x0000: "unknown PID", 0x0001: "format error", 0x0002: "hardware fault", 0x0003: "proxy reject",
    0x0004: "write protect", 0x0005: "unsupported command class", 0x0006: "data out of range",
    0x0007: "buffer full", 0x0008: "packet size unsupported", 0x0009: "sub-device out of range",
    0x000A: "proxy buffer full",
}

# Controller UID: ESTA prototype/experimental manufacturer range (0x7FF0-0x7FFF).
CONTROLLER_UID = bytes([0x7F, 0xF0, 0x46, 0x4C, 0x4B, 0x31])  # "FLK1"
BROADCAST_UID = b"\xff" * 6


def uid_str(uid: bytes) -> str:
    return "%04X:%08X" % (struct.unpack(">H", uid[:2])[0], struct.unpack(">I", uid[2:6])[0])


def uid_from_str(s: str) -> bytes:
    man, dev = s.split(":")
    return struct.pack(">HI", int(man, 16), int(dev, 16))


def checksum(body_without_startcode: bytes) -> int:
    return (START_CODE + sum(body_without_startcode)) & 0xFFFF


def build(dest: bytes, src: bytes, tn: int, port_or_resp: int, cc: int, pid: int,
          pd: bytes = b"", sub_device: int = 0, msg_count: int = 0) -> bytes:
    """Build an RDM message without start code, with checksum appended."""
    length = 24 + len(pd)  # start code .. end of PD
    body = (bytes([SUB_START, length]) + dest + src
            + bytes([tn & 0xFF, port_or_resp & 0xFF, msg_count & 0xFF])
            + struct.pack(">H", sub_device) + bytes([cc]) + struct.pack(">H", pid)
            + bytes([len(pd)]) + pd)
    return body + struct.pack(">H", checksum(body))


def build_request(dest: bytes, tn: int, cc: int, pid: int, pd: bytes = b"",
                  src: bytes = CONTROLLER_UID, port_id: int = 1) -> bytes:
    return build(dest, src, tn, port_id, cc, pid, pd)


@dataclass
class Message:
    dest: bytes
    src: bytes
    tn: int
    port_or_resp: int
    msg_count: int
    sub_device: int
    cc: int
    pid: int
    pd: bytes
    checksum_ok: bool

    @property
    def response_type(self) -> int:
        return self.port_or_resp

    def nack_reason(self) -> str:
        if len(self.pd) >= 2:
            code = struct.unpack(">H", self.pd[:2])[0]
            return NACK_REASONS.get(code, "reason 0x%04X" % code)
        return "no reason given"

    def ack_timer_seconds(self) -> float:
        if len(self.pd) >= 2:
            return struct.unpack(">H", self.pd[:2])[0] / 10.0
        return 0.1


def parse(data: bytes) -> Optional[Message]:
    """Parse an RDM message. Accepts with or without the leading 0xCC start code."""
    if data and data[0] == START_CODE:
        data = data[1:]
    if len(data) < 25 or data[0] != SUB_START:
        return None
    length = data[1]
    if length < 24 or len(data) < length - 1 + 2:
        return None
    body = data[:length - 1]
    pdl = body[22]
    pd = bytes(body[23:23 + pdl])
    ck = struct.unpack(">H", data[length - 1:length + 1])[0]
    return Message(
        dest=bytes(body[2:8]), src=bytes(body[8:14]), tn=body[14], port_or_resp=body[15],
        msg_count=body[16], sub_device=struct.unpack(">H", body[17:19])[0], cc=body[19],
        pid=struct.unpack(">H", body[20:22])[0], pd=pd, checksum_ok=(ck == checksum(body)),
    )


# ---------------------------------------------------------------- parameter data helpers

def decode_device_info(pd: bytes) -> dict:
    if len(pd) < 19:
        raise ValueError("DEVICE_INFO too short")
    (proto, model, category, sw, footprint, pers_cur, pers_count, start, subdev, sensors) = \
        struct.unpack(">HHHIHBBHHB", pd[:19])
    return {
        "rdm_version": proto, "model_id": model, "category": category, "software_id": sw,
        "footprint": footprint, "personality": pers_cur, "personality_count": pers_count,
        "address": start if start != 0xFFFF else None, "sub_devices": subdev, "sensors": sensors,
    }


def encode_device_info(model: int, category: int, sw: int, footprint: int, pers: int,
                       pers_count: int, start: int) -> bytes:
    return struct.pack(">HHHIHBBHHB", 0x0100, model, category, sw, footprint, pers, pers_count,
                       start, 0, 0)


def decode_personality_description(pd: bytes) -> dict:
    if len(pd) < 3:
        raise ValueError("PERSONALITY_DESCRIPTION too short")
    return {"personality": pd[0], "footprint": struct.unpack(">H", pd[1:3])[0],
            "description": pd[3:].split(b"\x00", 1)[0].decode("latin-1", "replace").strip()}


def decode_supported_parameters(pd: bytes) -> list:
    return [struct.unpack(">H", pd[i:i + 2])[0] for i in range(0, len(pd) - 1, 2)]


DATA_TYPES = {0x00: "not defined", 0x01: "bit field", 0x02: "ascii", 0x03: "uint8", 0x04: "int8",
              0x05: "uint16", 0x06: "int16", 0x07: "uint32", 0x08: "int32"}
CC_NAMES = {0x01: "GET", 0x02: "SET", 0x03: "GET_SET"}


def decode_parameter_description(pd: bytes) -> dict:
    if len(pd) < 20:
        raise ValueError("PARAMETER_DESCRIPTION too short")
    pid, size, dtype, cc, ptype, unit, prefix = struct.unpack(">HBBBBBB", pd[:8])
    lo, hi, default = struct.unpack(">iii", pd[8:20])
    return {"pid": pid, "size": size, "data_type": DATA_TYPES.get(dtype, "0x%02X" % dtype),
            "command_class": CC_NAMES.get(cc, "0x%02X" % cc), "can_get": bool(cc & 1), "can_set": bool(cc & 2),
            "unit": unit, "prefix": prefix, "min": lo, "max": hi, "default": default,
            "description": decode_text(pd[20:])}


def encode_parameter_description(pid, size, dtype, cc, lo, hi, default, description) -> bytes:
    return (struct.pack(">HBBBBBBiii", pid, size, dtype, cc, 0, 0, 0, lo, hi, default)
            + description.encode()[:32])


def encode_value(value: int, size: int, signed: bool = False) -> bytes:
    fmt = {1: "b" if signed else "B", 2: "h" if signed else "H", 4: "i" if signed else "I"}.get(size)
    if not fmt:
        raise ValueError("Unsupported parameter size %d" % size)
    return struct.pack(">" + fmt, int(value))


def decode_value(pd: bytes, size: int, signed: bool = False):
    fmt = {1: "b" if signed else "B", 2: "h" if signed else "H", 4: "i" if signed else "I"}.get(size)
    if not fmt or len(pd) < size:
        return None
    return struct.unpack(">" + fmt, pd[:size])[0]


def decode_text(pd: bytes) -> str:
    return pd.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()
