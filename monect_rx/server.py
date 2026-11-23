"""Monect PC Remote receiver for Linux — LAN server."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import subprocess
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from aiortc import rtcsctptransport as _sctp

# LAN tuning: start SCTP with a large congestion window so video never queues
# behind slow-start (aiortc begins with 3 MTUs).
_orig_sctp_init = _sctp.RTCSctpTransport.__init__


def _sctp_init(self, *a, **kw):
    _orig_sctp_init(self, *a, **kw)
    self._cwnd = 32 * _sctp.USERDATA_MAX_LENGTH
    self._ssthresh = 256 * _sctp.USERDATA_MAX_LENGTH


_sctp.RTCSctpTransport.__init__ = _sctp_init

from . import signaling as sig
from .audio import ADPCM, MIC_OPUS, OPUS, PCM, AudioStreamer, format_reply
from .media import H264CameraSink, VirtualCamera, VirtualMic, consume
from .taskmgr import TaskManager
from . import dsu
from .control import ControlServer
import hashlib
from .files import FileExplorer
from .projector import Projector
from .games import GameCenter
from . import helpers
from .inputdev import InputHub
from .packetizer import Packetizer
from .screen import ScreenStreamer, StartRequest, monitors_payload

log = logging.getLogger("monect")

CONTROL_PORT = 28451
NOTIFY_PORT = 28458
PROTOCOL_VERSION = 0x2B
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "monect-linux"

CAPABILITIES = {
    "touchpad": {"relativeMove": True, "absolutePositionV1": True, "absolutePositionV2": True,
                 "wheel": True, "threeButtons": True},
    "keyboard": {"hidBootReport": True, "scanCodes": True},
    "remoteDesktop": {"videoH264": True, "videoHevc": True, "monitorsInfo": True,
                      "audioPcm": True, "audioOpus": True},
    "gamepad": {"management": True, "x360Input": True, "dinputInput": True, "dsuInput": True},
    "microphone": {"streamToHost": True},
    "camera": {"uvcForward": True},
    "taskManager": {"webRtc": True},
    "gameCenter": {"listGames": True, "launchGame": True},
    "projector": {"screen": True, "audio": True},
    "clipboard": {"pasteText": True},
    "mediaKeys": {"consumerControl": True},
    "fileExplorer": {"browse": True},
    "webrtc": {"inputChannel": True},
    "platform": {"windows": True},
}
AUTH_OK = b"\x09\x00\x01" + PROTOCOL_VERSION.to_bytes(4, "big")


def hexdump(b: bytes, n: int = 64) -> str:
    s = b[:n].hex(" ")
    return s + (f" …(+{len(b) - n})" if len(b) > n else "")


def load_settings() -> dict:
    """~/.config/monect-linux/settings.json (bitrate, password, ...)."""
    p = CONFIG_DIR / "settings.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def load_hwid() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    p = CONFIG_DIR / "hwid"
    if p.exists():
        v = p.read_text().strip()
        if len(v) == 32:
            return v
    v = uuid.uuid4().hex.upper()
    p.write_text(v + "\n")
    return v


@dataclass
class Client:
    addr: tuple[str, int]
    client_id: bytes = b""
    name: str = "?"
    version: str = "?"
    extra: str = ""
    peer_id: bytes = b""
    connected_at: float = field(default_factory=time.time)
    session: "RtcSession | None" = None


class RtcSession:
    """One WebRTC peer (the phone) tunnelled through UDP 0x33 signaling."""

    def __init__(self, server: "MonectServer", client: Client) -> None:
        self.server = server
        self.client = client
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.seen_ids: set[bytes] = set()
        self.pending: dict[bytes, asyncio.Task] = {}
        self.next_id = 1
        self.early_candidates: list[sig.CandidateMsg] = []
        self.channels = []
        self.paste_buf: dict[int, bytearray] = {}
        self.input_seq: dict[int, int] = {}

        @self.pc.on("datachannel")
        def on_datachannel(ch):
            log.info("data channel opened: label=%r id=%s ordered=%s", ch.label, ch.id, ch.ordered)
            self.channels.append(ch)

            @ch.on("message")
            def on_message(msg):
                data = msg.encode() if isinstance(msg, str) else bytes(msg)
                try:
                    self.server.dispatch(self, ch, data)
                except Exception:  # never let one bad packet kill the channel
                    log.exception("dispatch failed for %s", hexdump(data))

        @self.pc.on("track")
        def on_track(track):
            sink = VirtualCamera() if track.kind == "video" else VirtualMic()
            asyncio.ensure_future(consume(track, sink))

        @self.pc.on("connectionstatechange")
        async def on_state():
            log.info("peer %s connection state: %s", client.addr[0], self.pc.connectionState)
            if self.pc.connectionState in ("failed", "closed"):
                self.server.control.event("disconnected", ip=client.addr[0], device=client.name)
                self.server.input.release_all()
                await self.server.stop_streams()

    # --- outgoing signaling with retransmission until acked -------------------
    def _send_reliable(self, payload: bytes) -> None:
        raw_id = payload[1:5]

        async def resend():
            for _ in range(40):
                self.server.send(b"\x33" + payload, self.client.addr)
                await asyncio.sleep(0.25)
            log.warning("signaling message %s never acknowledged", raw_id.hex())

        self.pending[raw_id] = asyncio.ensure_future(resend())

    def _alloc_id(self) -> int:
        i = self.next_id
        self.next_id += 1
        return i

    async def handle_signal(self, buf: bytes) -> None:
        try:
            msg = sig.parse(buf)
        except ValueError as ex:
            log.warning("bad signaling packet (%s): %s", ex, hexdump(buf))
            return
        if isinstance(msg, sig.AckMsg):
            t = self.pending.pop(msg.msg_id, None)
            if t:
                t.cancel()
            return
        if isinstance(msg, sig.ByeMsg):
            log.info("peer sent bye")
            await self.close()
            return
        # SDP / candidate: acknowledge, then de-duplicate
        self.server.send(b"\x33" + sig.build_ack(msg.msg_id), self.client.addr)
        if msg.msg_id in self.seen_ids:
            return
        self.seen_ids.add(msg.msg_id)

        if isinstance(msg, sig.SdpMsg):
            log.info("remote %s (%d bytes sdp)", msg.sdp_type, len(msg.sdp))
            log.debug("remote sdp:\n%s", msg.sdp)
            await self.pc.setRemoteDescription(RTCSessionDescription(msg.sdp, msg.sdp_type))
            for c in self.early_candidates:
                await self._add_candidate(c)
            self.early_candidates.clear()
            if msg.sdp_type == "offer":
                answer = await self.pc.createAnswer()
                await self.pc.setLocalDescription(answer)
                local = self.pc.localDescription
                log.info("sending answer (%d bytes sdp)", len(local.sdp))
                log.debug("local sdp:\n%s", local.sdp)
                self._send_reliable(sig.build_sdp(self._alloc_id(), "answer", local.sdp))
        elif isinstance(msg, sig.CandidateMsg):
            if self.pc.remoteDescription is None:
                self.early_candidates.append(msg)
            else:
                await self._add_candidate(msg)

    async def _add_candidate(self, c: sig.CandidateMsg) -> None:
        text = c.candidate.strip()
        if not text:
            return
        if text.startswith("candidate:"):
            text = text[len("candidate:"):]
        try:
            cand = candidate_from_sdp(text)
        except Exception:
            log.warning("cannot parse candidate %r", c.candidate)
            return
        cand.sdpMid = c.mid or None
        cand.sdpMLineIndex = c.mline_index
        log.debug("remote candidate %s", text)
        await self.pc.addIceCandidate(cand)

    async def close(self) -> None:
        for t in self.pending.values():
            t.cancel()
        self.pending.clear()
        await self.pc.close()
        self.server.input.release_all()
        await self.server.stop_streams()


class MonectServer(asyncio.DatagramProtocol):
    def __init__(self, hostname: str, sec_mode: int = 0) -> None:
        self.hostname = hostname
        self.sec_mode = sec_mode
        self.hwid = load_hwid()
        self.clients: dict[str, Client] = {}
        self.input = InputHub()
        self.transport: asyncio.DatagramTransport | None = None
        self.files = FileExplorer()
        self.screen: ScreenStreamer | None = None
        self.audio: AudioStreamer | None = None
        self.media_ch = None          # channel used for streams
        self.mic: AudioStreamer | None = None
        self.taskmgr: TaskManager | None = None
        self.cameras: dict[int, dict] = {}
        self.pad_flags = [1, 1, 1]
        self.dsu: dsu.DSUServer | None = None
        self.projector = Projector()
        self.settings = load_settings()
        if self.settings.get("name"):
            self.hostname = self.settings["name"]
        self.control = ControlServer(self)
        self.games = GameCenter()

    # --- UDP --------------------------------------------------------------------
    def connection_made(self, transport) -> None:
        self.transport = transport
        self.loop = asyncio.get_running_loop()
        log.info("listening on udp/%d as %r (hwid %s)", CONTROL_PORT, self.hostname, self.hwid)

    def send(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport:
            self.transport.sendto(data, addr)

    def datagram_received(self, data: bytes, addr) -> None:
        if not data:
            return
        if data[0] == 0x7F:
            self.send(b"\x7f", addr)
            data = data[1:]
            if not data:
                return
        op = data[0]
        try:
            if op == 0x10:
                self.on_discovery(data, addr)
            elif op == 0x12:
                self.on_connect(data, addr)
            elif op == 0x35:
                self.on_connect_v2(data, addr)
            elif op == 0x33:
                self.on_signal(data[1:], addr)
            elif op == 0x0F and len(data) >= 2:
                if addr[0] in self.clients:                  # only paired devices
                    helpers.run(data[1])
                else:
                    log.warning("helper action from unpaired %s ignored", addr[0])
            else:
                log.info("udp %s:%d op=0x%02x %s", addr[0], addr[1], op, hexdump(data))
        except Exception:
            log.exception("error handling udp packet from %s: %s", addr, hexdump(data))

    def on_discovery(self, data: bytes, addr) -> None:
        if len(data) < 10:
            return
        mode = data[1]
        name = self.hostname.encode("utf-16-le")[:254]
        reply = (bytes([0x05, self.sec_mode, len(name)]) + name
                 + struct.pack(">I", PROTOCOL_VERSION) + self.hwid.encode()[:32])
        dest = (addr[0], CONTROL_PORT) if mode == 1 else addr
        log.info("discovery from %s:%d (mode %d, id %s) -> reply to %s:%d",
                 addr[0], addr[1], mode, data[2:10].hex(), *dest)
        self.send(reply, dest)

    @staticmethod
    def _parse_connect(data: bytes):
        pos = 10
        n1 = data[pos]; pos += 1
        name = data[pos:pos + n1].decode("utf-16-le", "replace"); pos += n1
        n2 = data[pos]; pos += 1
        version = data[pos:pos + n2].decode("ascii", "replace"); pos += n2
        n3 = data[pos]; pos += 1
        extra = data[pos:pos + n3].decode("ascii", "replace")
        return name, version, extra

    def on_connect(self, data: bytes, addr) -> None:
        if len(data) < 13:
            return
        mode = data[1]
        try:
            name, version, extra = self._parse_connect(data)
        except IndexError:
            log.warning("truncated connect request: %s", hexdump(data))
            name = version = extra = "?"
        log.info("connect request from %s:%d mode=%d device=%r app=%s extra=%r",
                 addr[0], addr[1], mode, name, version, extra)
        old = self.clients.get(addr[0])
        if old and old.session:
            asyncio.ensure_future(old.session.close())
        self.clients[addr[0]] = Client(addr, data[2:10], name, version, extra)
        self.send(b"\x06", addr)
        log.info("accepted %r (%s)", name, addr[0])

    def on_connect_v2(self, data: bytes, addr) -> None:
        """0x35: clientId(8) peerId(8) n1 name(UTF-16) n2 appVersion n3 platform."""
        if len(data) < 20:
            return
        try:
            pos = 17
            n1 = data[pos]; pos += 1
            raw = data[pos:pos + n1]; pos += n1
            name = raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") \
                else raw.decode("utf-16-le", "replace")
            n2 = data[pos]; pos += 1
            version = data[pos:pos + n2].decode("ascii", "replace"); pos += n2
            n3 = data[pos]; pos += 1
            extra = data[pos:pos + n3].decode("ascii", "replace")
        except IndexError:
            log.warning("truncated 0x35 connect: %s", hexdump(data))
            return
        old = self.clients.get(addr[0])
        if (old and old.client_id == data[1:9] and old.peer_id == data[9:17]
                and time.time() - old.connected_at < 5 and old.session is None):
            self.send(b"\x06", addr)                       # retransmitted request
            return
        log.info("connect(v2) from %s:%d device=%r app=%s platform=%r", addr[0], addr[1],
                 name, version, extra)
        if old and old.session:
            asyncio.ensure_future(old.session.close())
        c = Client(addr, data[1:9], name, version, extra)
        c.peer_id = data[9:17]
        self.clients[addr[0]] = c
        self.send(b"\x06", addr)
        log.info("accepted %r (%s)", name, addr[0])

    def on_signal(self, payload: bytes, addr) -> None:
        client = self.clients.get(addr[0])
        if client is None:
            log.warning("signaling from unknown peer %s, ignoring", addr[0])
            return
        client.addr = addr
        if client.session is not None and client.session.pc.connectionState == "closed":
            client.session = None
        if client.session is None:
            client.session = RtcSession(self, client)
        asyncio.ensure_future(client.session.handle_signal(payload))

    # --- data channel -------------------------------------------------------------
    def _buffered(self) -> int:
        ch = self.media_ch
        try:
            return int(ch.bufferedAmount) if ch is not None else 0
        except Exception:
            return 0

    def _stream_send(self, data: bytes) -> None:
        ch = self.media_ch
        if ch is not None and ch.readyState == "open":
            ch.send(data)

    def dispatch(self, sess: RtcSession, ch, data: bytes) -> None:
        cat = data[0]
        body = data[1:]
        send = ch.send
        if cat == 0x00 and body:
            self.on_input(sess, ch, body)
        elif cat in (0x0D, 0xFE) and len(data) > 5:
            # input channel: seq(BE u32) + category-0 input message; unordered,
            # so drop anything older than what we already applied
            seq = struct.unpack(">I", data[1:5])[0]
            last = sess.input_seq.get(id(ch))
            if last is not None and 0 < (last - seq) & 0xFFFFFFFF < 0x80000000:
                return
            sess.input_seq[id(ch)] = seq
            self.on_input(sess, ch, data[5:])
        elif cat == 0x01 and body:
            asyncio.ensure_future(self.on_screen(ch, data))
        elif cat == 0x02 and body:
            asyncio.ensure_future(self.on_audio(ch, body))
        elif cat == 0x04 and body:
            self.on_auth(ch, body)
        elif cat == 0x06 and body:
            asyncio.ensure_future(self.files.handle(body, send))
        elif cat == 0x03 and body:
            self.on_camera(ch, body)
        elif cat == 0x05 and body:
            self.projector.handle(body, send)
        elif cat == 0x0F and body:
            helpers.run(body[0])
        elif cat == 0x07 and body:
            asyncio.get_running_loop().run_in_executor(
                None, self.games.handle, body, lambda d: self._threadsafe(send, d))
        elif cat == 0x0B and body:
            if self.taskmgr is None:
                self.taskmgr = TaskManager()
            asyncio.get_running_loop().run_in_executor(
                None, self.taskmgr.handle, body, lambda d: self._threadsafe(send, d))
        elif cat == 0x08 and body:
            send(b"\x11\x00" + self.shell_execute(body).to_bytes(4, "big", signed=True))
        elif cat == 0x0A:
            reply = b"\x14\x01" + json.dumps(CAPABILITIES, separators=(",", ":")).encode()
            log.info("capability query -> %d bytes", len(reply))
            send(reply)
        else:
            log.info("dc[%s] cat=0x%02x %s", ch.label, cat, hexdump(data))

    @staticmethod
    def shell_execute(b: bytes) -> int:
        """08 00 verb file params (UTF-16 with BOM, NUL-terminated).
        Returns a ShellExecute-style code: >32 success, 2 file not found, 31 no association."""
        from .files import to_linux
        import shlex

        def wstr(off):
            if b[off:off + 2] in (b"\xff\xfe", b"\xfe\xff"):
                off += 2
            end = off
            while end + 1 < len(b) and (b[end] or b[end + 1]):
                end += 2
            return b[off:end].decode("utf-16-le", "replace"), end + 2

        if not b or b[0] != 0:
            return 31
        verb, o = wstr(1)
        target, o = wstr(o)
        params, _ = wstr(o) if o < len(b) else ("", o)
        log.info("shell %s %r %r", verb, target, params)
        path = to_linux(target) if (":" in target[:3] or target.startswith("\\")) else target
        args = [to_linux(a) if (len(a) > 2 and a[1] == ":") else a
                for a in shlex.split(params.replace("\\", "/"), posix=True)] if params else []
        try:
            if path.lower().endswith((".exe", ".bat", ".msi")):
                if not shutil.which("wine"):
                    return 31
                cmd = ["wine", path, *args]
            elif os.path.isfile(path) and os.access(path, os.X_OK):
                cmd = [path, *args]
            elif os.path.exists(path) or "://" in target:
                cmd = ["xdg-open", path if os.path.exists(path) else target]
            else:
                return 2
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, cwd=os.path.dirname(path) or None)
            return 42
        except OSError:
            return 2

    def _threadsafe(self, fn, data: bytes) -> None:
        self.loop.call_soon_threadsafe(fn, data)

    def on_camera(self, ch, b: bytes) -> None:
        sub = b[0]
        if sub == 0x05 and len(b) >= 9:                    # frame for a virtual camera
            handle = int.from_bytes(b[1:9], "big")
            cam = self.cameras.get(handle)
            if cam is not None:
                cam["sink"].feed(bytes(b[9:]))
        elif sub == 0x02:                                  # create phone->PC virtual camera
            try:
                req = json.loads(b[1:].decode("utf-8", "replace") or "{}")
            except ValueError:
                req = {}
            for c in self.cameras.values():
                c["sink"].close()
            self.cameras.clear()
            handle = 0x4D4F000000000000 | (int(time.time() * 1000) & 0xFFFFFFFF)
            self.cameras[handle] = {"info": {"handle": handle, "name": req.get("name", "Monect"),
                                             "width": req.get("width", 1280),
                                             "height": req.get("height", 720)},
                                    "sink": H264CameraSink()}
            log.info("virtual camera %s -> handle %x", req, handle)
            ch.send(b"\x03\x05" + handle.to_bytes(8, "big"))
        elif sub in (0x03, 0x04):                          # list virtual cameras
            ch.send(b"\x03\x08" + json.dumps([c["info"] for c in self.cameras.values()]).encode())
        elif sub == 0x06 or sub == 0x01:                   # stop
            for c in self.cameras.values():
                c["sink"].close()
            self.cameras.clear()
            ch.send(b"\x03\x06" if sub == 0x06 else b"\x03\x03")
        elif sub == 0x00:                                  # PC webcam -> phone
            log.info("PC camera requested %s; no capture device", hexdump(b))
            ch.send(b"\x03\x04")                         # "no camera found"
        else:
            log.info("camera sub=0x%02x %s", sub, hexdump(b))

    def on_auth(self, ch, b: bytes) -> None:
        asyncio.ensure_future(self._auth(ch, b))

    async def _auth(self, ch, b: bytes) -> None:
        """security: open (anyone), confirm (ask in the UI), password (SHA-256 of UTF-8)."""
        sub = b[0]
        mode = self.settings.get("security", "open")
        sess = self._session_for(ch)
        name = sess.client.name if sess else "?"
        ip = sess.client.addr[0] if sess else "?"
        if sub == 0x02:                                    # hello: role, name
            if len(b) >= 6:
                ln = struct.unpack(">I", b[2:6])[0]
                name = b[6:6 + ln].decode("utf-16", "replace").lstrip("\ufeff") or name
            trusted = sess and sess.client.client_id.hex() in self.settings.get("trusted", [])
            if mode == "password" and not trusted:
                log.info("auth hello from %r -> password required", name)
                ch.send(b"\x09\x01\x00")
                return
            if mode == "confirm" and not trusted:
                ch.send(b"\x09\x00\x00")                 # waiting for confirmation
                ok = await self.control.ask_confirm(name, ip)
                if not ok:
                    log.info("connection from %r rejected", name)
                    ch.send(b"\x09\x00\x02")
                    return
            self._authorized(sess, ch, name)
        elif sub == 0x01:                                  # password hash
            ln = struct.unpack(">I", b[1:5])[0] if len(b) >= 5 else 0
            got = bytes(b[5:5 + ln])
            want = bytes.fromhex(self.settings.get("password_sha256", "")) or \
                hashlib.sha256(self.settings.get("password", "").encode()).digest()
            if mode != "password" or got == want:
                self._authorized(sess, ch, name)
            else:
                log.info("wrong password from %r", name)
                ch.send(b"\x09\x00\x05")
        else:
            log.info("auth sub=0x%02x %s", sub, hexdump(b))

    def _authorized(self, sess, ch, name: str) -> None:
        log.info("auth: %r accepted", name)
        if sess:
            sess.authorized = True
            sess.client.name = name
            if self.settings.get("remember_devices", True):
                t = self.settings.setdefault("trusted", [])
                cid = sess.client.client_id.hex()
                if cid not in t:
                    t.append(cid)
                    self.save_settings()
        ch.send(AUTH_OK)
        self.control.event("connected", device=name, ip=sess.client.addr[0] if sess else "")

    def _session_for(self, ch):
        for c in self.clients.values():
            if c.session and ch in c.session.channels:
                return c.session
        return None

    # --- UI / control ------------------------------------------------------------
    def status(self) -> dict:
        import socket as _s
        ips = []
        try:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                                 capture_output=True, text=True, timeout=2).stdout
            ips = [l.split()[3].split("/")[0] for l in out.splitlines()]
        except Exception:
            pass
        devices = []
        for ip, c in self.clients.items():
            st = c.session.pc.connectionState if c.session else "new"
            if st in ("closed", "failed"):
                continue
            devices.append({"ip": ip, "name": c.name, "app": c.version, "platform": c.extra,
                            "state": st, "since": c.connected_at,
                            "remote_desktop": bool(self.screen and self.screen.running)})
        return {"name": self.hostname, "ips": ips, "port": CONTROL_PORT, "hwid": self.hwid,
                "devices": devices, "dsu_port": 26760}

    def public_settings(self) -> dict:
        s = dict(self.settings)
        s.setdefault("security", "open")
        s.setdefault("bitrate", 8_000_000)
        s.setdefault("max_fps", 60)
        s.setdefault("remember_devices", True)
        s["name"] = self.hostname
        s["has_password"] = bool(s.pop("password_sha256", "") or s.pop("password", ""))
        return s

    def update_settings(self, new: dict) -> None:
        for k in ("security", "bitrate", "max_fps", "remember_devices", "password", "name",
                  "language"):
            if k in new:
                if k == "name":
                    self.hostname = str(new[k]) or self.hostname
                    self.settings["name"] = self.hostname
                elif k == "password":
                    if new[k]:
                        self.settings["password_sha256"] = hashlib.sha256(
                            str(new[k]).encode()).hexdigest()
                        self.settings.pop("password", None)
                    continue
                else:
                    self.settings[k] = new[k]
        if new.get("clear_trusted"):
            self.settings["trusted"] = []
        self.save_settings()
        log.info("settings updated: %s", {k: v for k, v in self.settings.items() if not k.startswith("password") and k != "trusted"})

    def save_settings(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        p = CONFIG_DIR / "settings.json"
        p.write_text(json.dumps(self.settings, indent=2, ensure_ascii=False))
        os.chmod(p, 0o600)

    async def disconnect(self, ip: str) -> None:
        c = self.clients.pop(ip, None)
        if c and c.session:
            try:
                self.send(b"\x33" + sig.build_bye(), c.addr)
            except Exception:
                pass
            await c.session.close()
            self.control.event("disconnected", ip=ip, device=c.name)

    async def on_screen(self, ch, m: bytes) -> None:
        sub = m[1]
        if sub == 0x00:
            req = StartRequest.parse(m)
            req.fps = min(req.fps, int(self.settings.get("max_fps", 60)))
            log.info("remote desktop start: %s", hexdump(m, 64))
            self.media_ch = ch
            pk = Packetizer(0x05, self._stream_send)
            if self.screen is not None:
                await self.screen.stop()
            self.screen = ScreenStreamer(pk.push, self.settings, CONFIG_DIR / "screencast.token",
                                         buffered=self._buffered)
            req.codec = 0                                  # stream H.264 (universally decodable)
            ok = await self.screen.start(req)
            if ok:
                # started: [2] codec actually used (always H.264 here), [4]=1 would mean AV1
                ch.send(bytes([0x01, 0x00, 0x00, req.flags_tail, 0]))
            else:
                ch.send(b"\x01\x02")
        elif sub == 0x01:
            running = self.screen is not None and self.screen.running
            if self.screen:
                await self.screen.stop()
            ch.send(b"\x01\x03")                            # stopped
        elif sub == 0x02 and self.screen:
            self.screen.paused = True
        elif sub == 0x03 and self.screen:
            self.screen.paused = False
        elif sub == 0x04:
            ch.send(b"\x01\x05" + monitors_payload())
        elif sub == 0x06:
            if self.screen:
                self.screen.request_keyframe()
        elif sub == 0x05:
            idx = m[2] if len(m) > 2 else 0
            ch.send(bytes([0x01, 0x07, idx]))             # switching not supported (portal picks)
        else:
            log.info("screen sub=0x%02x %s", sub, hexdump(m))

    async def on_audio(self, ch, b: bytes) -> None:
        sub = b[0]
        if self.audio is None:
            self.audio = AudioStreamer(self._stream_send)
        if sub == 0x00:
            ch.send(format_reply())
        elif sub in (0x01, 0x05, 0x06):
            codec = {0x01: PCM, 0x05: ADPCM, 0x06: OPUS}[sub]
            if codec == ADPCM:
                codec = PCM                               # ADPCM encoder not implemented
            if self.audio.running:
                ch.send(b"\x02\x04")
                return
            self.media_ch = self.media_ch or ch
            ok = await self.audio.start(codec)
            ch.send(b"\x02" + bytes([3 if ok else 1]))
        elif sub == 0x02:
            running = self.audio.running
            await self.audio.stop()
            ch.send(b"\x02" + bytes([2 if running else 5]))
        elif sub == 0x03:
            ch.send(b"\x02\x06" + self.audio.get_volume().to_bytes(4, "big"))
        elif sub == 0x04 and len(b) >= 5:
            self.audio.set_volume(struct.unpack(">I", b[1:5])[0])
        elif sub == 0x07:                                  # PC microphone format
            ch.send(b"\x02\x07" + format_reply()[2:])
        elif sub == 0x08:                                  # start PC microphone -> phone
            if self.mic is None:
                self.mic = AudioStreamer(self._stream_send)
            self.media_ch = self.media_ch or ch
            ok = await self.mic.start(OPUS, "@DEFAULT_SOURCE@", MIC_OPUS)
            ch.send(b"\x02" + bytes([0x0B if ok else 0x08]))
        elif sub == 0x09:
            if self.mic:
                await self.mic.stop()
        else:
            log.info("audio sub=0x%02x %s", sub, hexdump(b))

    def on_input(self, sess: RtcSession, ch, b: bytes) -> None:
        sub, p = b[0], b[1:]
        if not getattr(sess, "input_seen", False):
            sess.input_seen = True
            log.info("first input from %s (sub 0x%02x) - remote control active", sess.client.name, sub)
        hub = self.input
        if sub == 0x00 and len(p) >= 4:                       # mouse boot report
            hub.mouse.report(p[0], struct.unpack("b", p[1:2])[0],
                             struct.unpack("b", p[2:3])[0], struct.unpack("b", p[3:4])[0])
        elif sub == 0x01 and len(p) >= 8:                     # keyboard boot report
            hub.kbd.boot_report(p[:8])
        elif sub == 0x02 and len(p) >= 4:                     # absolute pos on streamed monitor
            nx, ny = struct.unpack(">HH", p[:4])
            fx, fy = nx / 65535, ny / 65535
            if self.screen:
                fx, fy = self.screen.map_abs(fx, fy)
            hub.absptr.move(fx, fy)
        elif sub == 0x07 and p:                               # multi-touch on streamed monitor
            n = min(p[0], 10)
            pts = []
            for i in range(n):
                o = 1 + 16 * i
                if o + 16 > len(p):
                    break
                cid, flags, _extra, x, y = struct.unpack_from(">IHHff", p, o)
                pts.append((cid, flags, x, y))
            if pts:
                hub.touch.update(pts)
        elif sub == 0x0C and len(p) >= 8:                     # absolute pos on whole desktop
            x, y = struct.unpack(">ii", p[:8])
            hub.absptr.move(x / 65535, y / 65535)
        elif sub == 0x03 and len(p) >= 14:                    # xbox 360 report
            if p[1] == 0:
                hub.pad(p[0]).report(p[2:14])
        elif sub == 0x08 and len(p) >= 66:                    # DSU motion report
            if self.dsu:
                self.dsu.update(b"\x00" + b)
        elif sub == 0x04 and len(p) >= 23:                    # directinput report
            if p[1] == 0:
                hub.dpad(p[0]).report(p[2:23])
        elif sub == 0x05 and p:                               # gamepad management
            self.on_gamepad_mgmt(ch, p)
        elif sub == 0x06 and len(p) >= 4:                     # paste text
            self.on_paste(sess, b)
        elif sub == 0x09 and len(p) >= 2:                     # consumer keys
            hub.kbd.consumer_bits(p[0] | (p[1] << 8))
        elif sub == 0x0D and len(p) >= 2:                     # single scancode
            hub.kbd.scancode(p[0], bool(p[1] & 1), bool(p[1] & 2))
        else:
            log.info("input sub=0x%02x %s", sub, hexdump(b))

    def on_gamepad_mgmt(self, ch, p: bytes) -> None:
        """op 0: (type, maxIndex) pairs -> plug up to that index;
        op 1: (type, count) pairs -> keep exactly `count` pads of that type.
        types: 2 DirectInput, 3 Xbox 360, 4 DSU motion slot, 1 (ignored by Windows too)."""
        op = p[0]
        if op in (0x00, 0x01) and len(p) >= 2:
            count = p[1]
            pairs = [(p[2 + 2 * i], p[3 + 2 * i]) for i in range(count) if 3 + 2 * i < len(p)]
            log.info("gamepad %s: %s", "plug" if op == 0 else "set counts", pairs)
            for kind, val in pairs:
                n = val + 1 if op == 0 else val
                if kind == 3:
                    for i in range(n):
                        self.input.pad(i)
                    if op == 1:
                        for k in [k for k in self.input.pads if k < 100 and k >= n]:
                            self.input.unplug(k)
                elif kind == 2:
                    for i in range(n):
                        self.input.dpad(i)
                    if op == 1:
                        for k in [k for k in self.input.pads if k >= 100 and k - 100 >= n]:
                            self.input.unplug(k)
                elif kind == 4 and self.dsu:
                    if op == 0:
                        self.dsu.slots.setdefault(val & 3, bytes(64))
                    else:
                        for sl in [sl for sl in self.dsu.slots if sl >= val]:
                            self.dsu.unplug(sl)
            if op == 0:
                ch.send(b"\x04" + bytes(256))
            else:
                ch.send(self._pad_status())
        elif op == 0x02:                                     # status query
            ch.send(self._pad_status())
        elif op == 0x03 and len(p) >= 2:
            if p[1] == 0x00:                                 # get unplug-on-disconnect flags
                ch.send(bytes([0x04, 0x03]) + bytes(self.pad_flags))
            elif p[1] == 0x01 and len(p) >= 5:               # set flags
                self.pad_flags = list(p[2:5])
                ch.send(b"\x04\x04\x01")
        else:
            log.info("gamepad mgmt op=0x%02x %s", op, hexdump(p))

    def _pad_status(self) -> bytes:
        nx = sum(1 for k in self.input.pads if k < 100)
        nd = sum(1 for k in self.input.pads if k >= 100)
        ns = len(self.dsu.slots) if self.dsu else 0
        return bytes([0x04, 0x02, 0x03, nx, 0x02, 0x01, nd, 0x04, ns])

    def on_paste(self, sess: RtcSession, b: bytes) -> None:
        # b = [0x06, BE u32 total, (2 header bytes on the first fragment), text…]
        total = struct.unpack(">I", b[1:5])[0]
        text_len = total - 2
        if text_len <= 0 or text_len > 1 << 24:
            return
        key = id(sess)
        buf = sess.paste_buf.get(key)
        if buf is None:
            buf = bytearray(b[7:])
            sess.paste_buf[key] = buf
        else:
            buf += b[5:]
        if len(buf) >= text_len:
            text = bytes(buf[:text_len]).decode("utf-16-le", "replace").split("\x00", 1)[0]
            del sess.paste_buf[key]
            log.info("paste text (%d chars)", len(text))
            self.input.paste_text(text)

    async def stop_streams(self) -> None:
        if self.screen:
            await self.screen.stop()
        if self.audio:
            await self.audio.stop()
        if self.mic:
            await self.mic.stop()
        self.projector.stop()
        for c in self.cameras.values():
            c["sink"].close()
        self.cameras.clear()
        self.media_ch = None

    def close(self) -> None:
        self.input.close()


async def run(hostname: str | None = None) -> None:
    loop = asyncio.get_running_loop()
    server = MonectServer(hostname or socket.gethostname())
    transport, _ = await loop.create_datagram_endpoint(
        lambda: server, local_addr=("0.0.0.0", CONTROL_PORT), allow_broadcast=True)
    server.dsu = await dsu.start()
    await server.control.start()
    try:
        await asyncio.Event().wait()
    finally:
        transport.close()
        for c in server.clients.values():
            if c.session:
                await c.session.close()
        server.close()
