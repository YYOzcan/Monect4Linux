"""Sequence-numbered framing used for video, audio and cursor streams.

Every data-channel message is  ``prefix | seq(BE u16) | payload-header | data``.
A buffer that fits in one message is sent as ``0x00 | buffer``.  Larger buffers
are split into chunks of ``CHUNK`` bytes with a two byte FU-style header:
``(b0 & 0xFC) | 0x1C`` followed by ``(b0 & 0x1F) | S(0x80) | E(0x40)`` where
``b0`` is the first byte of the whole buffer.  The receiver concatenates the
chunk payloads verbatim.
"""
from __future__ import annotations

SINGLE_MAX = 0x3FFC
CHUNK = 0x3FFB


class Packetizer:
    def __init__(self, prefix: int, send) -> None:
        self.prefix = prefix
        self.send = send            # callable(bytes) -> None
        self.seq = 0

    def _emit(self, body: bytes) -> None:
        self.send(bytes([self.prefix]) + (self.seq & 0xFFFF).to_bytes(2, "big") + body)
        self.seq = (self.seq + 1) & 0xFFFF

    def push(self, buf: bytes) -> None:
        if not buf:
            return
        if len(buf) <= SINGLE_MAX:
            self._emit(b"\x00" + buf)
            return
        b0 = buf[0]
        indicator = (b0 & 0xFC) | 0x1C
        n = (len(buf) + CHUNK - 1) // CHUNK
        for i in range(n):
            hdr = b0 & 0x1F
            if i == 0:
                hdr |= 0x80
            if i == n - 1:
                hdr |= 0x40
            self._emit(bytes([indicator, hdr]) + buf[i * CHUNK:(i + 1) * CHUNK])


class Depacketizer:
    """Inverse of Packetizer for streams the phone sends (projector, camera).

    Input: ``seq(BE u16) | b2 | ...`` without the stream prefix.
      b2 & 0x1F == 0      -> whole buffer follows b2
      b2 & 0x1F == 28     -> FU chunk: next byte has S(0x80)/E(0x40), data follows
      anything else       -> buffer starts at b2 (raw NAL)
    Returns a complete buffer or None.
    """

    def __init__(self) -> None:
        self.parts: list[bytes] = []
        self.last_seq: int | None = None
        self.in_fu = False

    def push(self, msg: bytes) -> bytes | None:
        if len(msg) < 3:
            return None
        seq = (msg[0] << 8) | msg[1]
        kind = msg[2] & 0x1F
        prev, self.last_seq = self.last_seq, seq
        if kind != 28:
            self.in_fu = False
            return bytes(msg[3:]) if kind == 0 else bytes(msg[2:])
        if len(msg) < 4:
            return None
        hdr = msg[3]
        if hdr & 0x80:                                   # start
            self.parts = [bytes(msg[4:])]
            self.in_fu = True
        elif self.in_fu and prev is not None and ((prev + 1) & 0xFFFF) == seq:
            self.parts.append(bytes(msg[4:]))
        else:                                            # lost a chunk; wait for next start
            self.in_fu = False
            self.parts = []
            return None
        if hdr & 0x40:                                   # end
            self.in_fu = False
            out = b"".join(self.parts)
            self.parts = []
            return out
        return None
