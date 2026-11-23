"""DSU (cemuhook) motion server on UDP 26760.

The phone's "DSU" pad / 3D-gesture mode sends 68-byte reports (category 0,
sub 0x08) whose bytes 4..67 match the DSU pad-data layout from the button
bytes onward (buttons, sticks, analog buttons, touch, timestamp, accel, gyro).
Emulators such as Dolphin (Wii Remote motion), Cemu and Ryujinx subscribe
here and receive the phone's motion like from a real controller.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
import zlib

log = logging.getLogger("dsu")

PORT = 26760
VERSION = 1001
MSG_VERSION, MSG_PORTS, MSG_DATA = 0x100000, 0x100001, 0x100002
SERVER_ID = 0x4D4F4E45  # "MONE"


class DSUServer(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport = None
        self.slots: dict[int, bytes] = {}          # slot -> last 64-byte body (from buttons on)
        self.counters = [0, 0, 0, 0]
        self.clients: dict[tuple, dict] = {}       # addr -> {"t": last request, "slots": set}

    # ---- packet helpers --------------------------------------------------------
    @staticmethod
    def _packet(msg_type: int, payload: bytes) -> bytes:
        body = struct.pack("<I", msg_type) + payload
        hdr = b"DSUS" + struct.pack("<HHII", VERSION, len(body), 0, SERVER_ID)
        pkt = bytearray(hdr + body)
        struct.pack_into("<I", pkt, 8, zlib.crc32(pkt) & 0xFFFFFFFF)
        return bytes(pkt)

    def _shared(self, slot: int) -> bytes:
        connected = slot in self.slots
        # slot, state(2=connected), model(2=full gyro), conn(2=bluetooth), mac, battery(5=full)
        return struct.pack("<BBBB6sB", slot, 2 if connected else 0, 2 if connected else 0,
                           2 if connected else 0, bytes([0x4D, 0x4F, 0x4E, 0, 0, slot]),
                           5 if connected else 0)

    # ---- protocol --------------------------------------------------------------
    def connection_made(self, transport) -> None:
        self.transport = transport
        log.info("DSU motion server on udp/%d (use as cemuhook/DSU client in Dolphin, Cemu...)", PORT)

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 20 or data[:4] != b"DSUC":
            return
        msg_type = struct.unpack_from("<I", data, 16)[0]
        if msg_type == MSG_VERSION:
            self.transport.sendto(self._packet(MSG_VERSION, struct.pack("<H", VERSION)), addr)
        elif msg_type == MSG_PORTS and len(data) >= 24:
            n = min(struct.unpack_from("<i", data, 20)[0], 4)
            for i in range(n):
                if 24 + i < len(data):
                    slot = data[24 + i]
                    if slot < 4:
                        self.transport.sendto(self._packet(MSG_PORTS, self._shared(slot) + b"\x00"),
                                              addr)
        elif msg_type == MSG_DATA:
            c = self.clients.setdefault(addr, {"t": 0.0, "slots": set()})
            c["t"] = time.monotonic()
            flags = data[20] if len(data) > 20 else 0
            slot = data[21] if len(data) > 21 else 0
            c["slots"] = {slot} if flags & 1 else {0, 1, 2, 3}
            if addr not in getattr(self, "_seen", set()):
                self._seen = getattr(self, "_seen", set()) | {addr}
                log.info("DSU client subscribed: %s:%d", *addr)

    # ---- feed from the phone ---------------------------------------------------------
    def update(self, report: bytes) -> None:
        """report = full 68-byte phone message (starting with 0x00 0x08)."""
        if len(report) < 68:
            return
        slot = report[2] & 3
        first = slot not in self.slots
        self.slots[slot] = bytes(report[4:68])
        if first:
            log.info("DSU slot %d active (phone motion)", slot)
        self.counters[slot] = (self.counters[slot] + 1) & 0xFFFFFFFF
        payload = (self._shared(slot) + b"\x01" + struct.pack("<I", self.counters[slot])
                   + self.slots[slot])
        pkt = self._packet(MSG_DATA, payload)
        log.debug("pad data slot %d -> %d clients", slot, len(self.clients))
        now = time.monotonic()
        for addr, c in list(self.clients.items()):
            if now - c["t"] > 5.0:
                del self.clients[addr]
                continue
            if slot in c["slots"]:
                self.transport.sendto(pkt, addr)

    def unplug(self, slot: int) -> None:
        self.slots.pop(slot, None)


async def start() -> DSUServer:
    loop = asyncio.get_running_loop()
    srv = DSUServer()
    try:
        await loop.create_datagram_endpoint(lambda: srv, local_addr=("0.0.0.0", PORT))
    except OSError as ex:
        log.warning("DSU server not started: %s", ex)
    return srv
