"""Media-file projector: the phone shares a video/music file over its own FTP server
(ftp://admin:admin@phone:28456/…) and the PC plays it.

UDP 28454 (phone -> PC; 0x7F prefix = reliable, answer one 0x7F byte):
    00 00 len(BE u32) url(UTF-16 with BOM)   play
    00 01 percent(BE u32)                     seek
    00 02                                     play / pause
    00 03                                     toggle full screen
    03                                        stop
PC -> phone (to the sender's port):  02 position(f32 LE, s) duration(BE u32, s) | 04 finished
UDP 28457: heartbeat, the phone sends FF every second and expects FF back.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import struct
import subprocess

log = logging.getLogger("vprojector")
PORT, HEARTBEAT_PORT = 28454, 28457


class _Heartbeat(asyncio.DatagramProtocol):
    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if data[:1] == b"\xff":
            self.transport.sendto(b"\xff", addr)


class VideoProjector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport = None
        self.peer = None
        self.proc: asyncio.subprocess.Process | None = None
        self.ipc = None
        self.pos, self.dur = 0.0, 0.0
        self.tasks: list[asyncio.Task] = []
        self.sock = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "monect-mpv.sock")
        self.url = ""

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if not data:
            return
        if data[0] == 0x7F:
            self.transport.sendto(b"\x7f", addr)
            data = data[1:]
        if not data:
            return
        self.peer = addr
        if data[:2] == b"\x00\x00" and len(data) >= 6:
            n = struct.unpack(">I", data[2:6])[0]
            url = data[6:6 + n].decode("utf-16", "replace").lstrip("﻿").split("\x00")[0]
            asyncio.ensure_future(self.play(url))
        elif data[:2] == b"\x00\x01" and len(data) >= 6:
            pct = struct.unpack(">i", data[2:6])[0]
            self.command("seek", max(0, min(100, pct)), "absolute-percent")
        elif data[:2] == b"\x00\x02":
            self.command("cycle", "pause")
        elif data[:2] == b"\x00\x03":
            self.command("cycle", "fullscreen")
        elif data[:1] == b"\x03":
            asyncio.ensure_future(self.stop())
        elif data[:2] == b"\x04\x01":                    # legacy blackboard probe: use WebRTC
            self.transport.sendto(b"\x7e", addr)
        else:
            log.debug("vprojector %s", data[:16].hex(" "))

    # ---- player ----------------------------------------------------------------
    async def play(self, url: str) -> None:
        await self.stop(notify=False)
        self.url = url
        safe = url.split("@")[-1] if "@" in url else url
        log.info("playing %s", safe)
        mpv = shutil.which("mpv")
        if mpv:
            try:
                os.unlink(self.sock)
            except OSError:
                pass
            args = [mpv, "--force-window=yes", "--fs", "--keep-open=no", "--no-terminal",
                    f"--input-ipc-server={self.sock}", "--title=Monect Projector", url]
            if os.environ.get("MONECT_DRYRUN"):
                args[1:1] = ["--vo=null", "--ao=null", "--force-window=no"]
            self.proc = await asyncio.create_subprocess_exec(*args, stdin=subprocess.DEVNULL,
                                                             stdout=subprocess.DEVNULL,
                                                             stderr=subprocess.DEVNULL)
            self.tasks = [asyncio.ensure_future(self._ipc()), asyncio.ensure_future(self._wait())]
        elif shutil.which("ffplay"):
            self.proc = await asyncio.create_subprocess_exec(
                "ffplay", "-fs", "-autoexit", "-loglevel", "quiet", url,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.tasks = [asyncio.ensure_future(self._wait())]
        else:
            log.warning("no media player (install mpv)")
            self._send(b"\x04")

    async def _ipc(self) -> None:
        for _ in range(100):
            try:
                r, w = await asyncio.open_unix_connection(self.sock)
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            return
        self.ipc = w
        for i, prop in ((1, "time-pos"), (2, "duration")):
            w.write((json.dumps({"command": ["observe_property", i, prop]}) + "\n").encode())
        await w.drain()
        last = 0.0
        loop = asyncio.get_running_loop()
        while True:
            line = await r.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("event") == "property-change" and m.get("data") is not None:
                if m.get("name") == "time-pos":
                    self.pos = float(m["data"])
                elif m.get("name") == "duration":
                    self.dur = float(m["data"])
                if loop.time() - last >= 0.5:
                    last = loop.time()
                    self._send(b"\x02" + struct.pack("<f", self.pos) +
                               struct.pack(">I", int(self.dur)))
        self.ipc = None

    async def _wait(self) -> None:
        proc = self.proc
        if proc is None:
            return
        await proc.wait()
        if self.proc is proc:
            self.proc = None
            log.info("playback finished")
            self._send(b"\x04")

    def command(self, *args) -> None:
        if self.ipc is not None:
            try:
                self.ipc.write((json.dumps({"command": list(args)}) + "\n").encode())
            except Exception:
                pass

    def _send(self, data: bytes) -> None:
        if self.transport and self.peer:
            self.transport.sendto(data, self.peer)

    async def stop(self, notify: bool = True) -> None:
        proc, self.proc = self.proc, None
        for t in self.tasks:
            t.cancel()
        self.tasks = []
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
        if self.ipc:
            self.ipc.close()
            self.ipc = None
        if proc and notify:
            self._send(b"\x04")


async def start() -> VideoProjector | None:
    loop = asyncio.get_running_loop()
    vp = VideoProjector()
    try:
        await loop.create_datagram_endpoint(lambda: vp, local_addr=("0.0.0.0", PORT))
        await loop.create_datagram_endpoint(_Heartbeat, local_addr=("0.0.0.0", HEARTBEAT_PORT))
    except OSError as ex:
        log.warning("media projector ports unavailable: %s", ex)
        return None
    return vp
