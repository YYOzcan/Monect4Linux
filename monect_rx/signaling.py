"""Binary WebRTC signaling messages carried inside UDP 0x33 packets."""
from __future__ import annotations

import struct
from dataclasses import dataclass

SDP, CANDIDATE, ACK, BYE = 0x02, 0x03, 0x04, 0x05
SDP_TYPES = {0: "offer", 1: "pranswer", 2: "answer"}
SDP_CODES = {v: k for k, v in SDP_TYPES.items()}


@dataclass
class SdpMsg:
    msg_id: bytes
    sdp_type: str
    sdp: str


@dataclass
class CandidateMsg:
    msg_id: bytes
    mline_index: int
    mid: str
    candidate: str


@dataclass
class AckMsg:
    msg_id: bytes


@dataclass
class ByeMsg:
    pass


def parse(buf: bytes):
    """Return a message object or raise ValueError."""
    if not buf:
        raise ValueError("empty")
    t = buf[0]
    if t == SDP:
        if len(buf) < 10:
            raise ValueError("short sdp")
        msg_id, kind, ln = buf[1:5], buf[5], struct.unpack(">I", buf[6:10])[0]
        if kind not in SDP_TYPES or ln > len(buf) - 10:
            raise ValueError("bad sdp header")
        return SdpMsg(msg_id, SDP_TYPES[kind], buf[10:10 + ln].decode("utf-8", "replace"))
    if t == CANDIDATE:
        if len(buf) < 13:
            raise ValueError("short candidate")
        msg_id = buf[1:5]
        mline, mid_len = struct.unpack(">II", buf[5:13])
        pos = 13
        mid = buf[pos:pos + mid_len].decode("utf-8", "replace")
        pos += mid_len
        (cand_len,) = struct.unpack(">I", buf[pos:pos + 4])
        pos += 4
        cand = buf[pos:pos + cand_len].decode("utf-8", "replace")
        return CandidateMsg(msg_id, mline, mid, cand)
    if t == ACK:
        if len(buf) < 5:
            raise ValueError("short ack")
        return AckMsg(buf[1:5])
    if t == BYE:
        return ByeMsg()
    raise ValueError(f"unknown signaling type 0x{t:02x}")


def build_sdp(msg_id: int, sdp_type: str, sdp: str) -> bytes:
    data = sdp.encode()
    return (bytes([SDP]) + struct.pack(">I", msg_id) + bytes([SDP_CODES[sdp_type]])
            + struct.pack(">I", len(data)) + data)


def build_candidate(msg_id: int, mline: int, mid: str, cand: str) -> bytes:
    m, c = mid.encode(), cand.encode()
    return (bytes([CANDIDATE]) + struct.pack(">III", msg_id, mline, len(m)) + m
            + struct.pack(">I", len(c)) + c)


def build_ack(raw_id: bytes) -> bytes:
    return bytes([ACK]) + raw_id


def build_bye() -> bytes:
    return bytes([BYE])
