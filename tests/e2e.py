"""End-to-end test: plays the phone exactly like the PC Remote app (9.x) does.

Run against a receiver started with:  MONECT_SCREEN_TEST=1 MONECT_PROJECTOR_DUMP=<file> \
    python -m monect_rx --dry-run  > <logfile>
    python tests/e2e.py <logfile> <dumpfile>
"""
import asyncio
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import av  # noqa: E402
from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: E402

from monect_rx import signaling as sig  # noqa: E402
from monect_rx.packetizer import Depacketizer, Packetizer  # noqa: E402

H = ("127.0.0.1", 28451)
LOG = Path(sys.argv[1]) if len(sys.argv) > 1 else None
DUMP = Path(sys.argv[2]) if len(sys.argv) > 2 else None
SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "monect-linux.sock"
results: list[tuple[str, bool, str]] = []


def check(name, ok, info=""):
    results.append((name, bool(ok), info))
    print(("  ✓ " if ok else "  ✗ ") + name + (f"  ({info})" if info else ""), flush=True)


def wstr(s):                         # what the app sends: UTF-16LE with BOM + NUL
    return b"\xff\xfe" + s.encode("utf-16-le") + b"\0\0"


def log_has(text, since=0):
    return LOG is not None and text in LOG.read_text(errors="replace")[since:]


class _P(asyncio.DatagramProtocol):
    def __init__(self):
        self.q = asyncio.Queue()

    def datagram_received(self, d, a):
        self.q.put_nowait(d)


class Phone:
    async def connect(self, name="E2E Phone"):
        loop = asyncio.get_running_loop()
        self.udp, self.p = await loop.create_datagram_endpoint(_P, local_addr=("127.0.0.1", 0))
        self.udp.sendto(bytes([0x10, 4]) + os.urandom(8), H)
        self.disc = await asyncio.wait_for(self.p.q.get(), 3)
        nm = b"\xff\xfe" + name.encode("utf-16-le")
        self.udp.sendto(b"\x35" + os.urandom(8) + os.urandom(8) + bytes([len(nm)]) + nm
                        + b"\x059.1.4\x0aAndroid_15", H)
        self.conn = await asyncio.wait_for(self.p.q.get(), 3)
        self.pc = RTCPeerConnection()
        self.mbus = self.pc.createDataChannel("mbus")
        self.inp = self.pc.createDataChannel("input", ordered=False)
        self.got = asyncio.Queue()
        self.mbus.on("message", lambda m: self.got.put_nowait(bytes(m)))
        op = asyncio.Event()
        self.inp.on("open", op.set)
        await self.pc.setLocalDescription(await self.pc.createOffer())
        self.udp.sendto(b"\x33" + sig.build_sdp(7, "offer", self.pc.localDescription.sdp), H)
        while True:
            m = sig.parse((await asyncio.wait_for(self.p.q.get(), 10))[1:])
            if isinstance(m, sig.SdpMsg):
                self.udp.sendto(b"\x33" + sig.build_ack(m.msg_id), H)
                await self.pc.setRemoteDescription(RTCSessionDescription(m.sdp, m.sdp_type))
                break
        await asyncio.wait_for(op.wait(), 10)
        self.seq = 0
        return self

    def drain(self):
        while not self.got.empty():
            self.got.get_nowait()

    async def ask(self, msg, timeout=8, want=None):
        self.drain()
        self.mbus.send(msg)
        return await self.wait(want, timeout)

    async def wait(self, want=None, timeout=8):
        end = time.monotonic() + timeout
        while True:
            m = await asyncio.wait_for(self.got.get(), max(0.1, end - time.monotonic()))
            if want is None or m[:len(want)] == want:
                return m

    def input(self, cat0_msg):          # like pe5.w(): 0D + seq + message without the 0x00
        self.seq += 1
        self.inp.send(b"\x0d" + self.seq.to_bytes(4, "big") + cat0_msg)

    async def close(self):
        self.udp.sendto(b"\x33" + sig.build_bye(), H)
        await self.pc.close()


def h264_frames(n=8, w=320, h=240):
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    from fractions import Fraction
    enc.time_base = Fraction(1, 30)
    enc.options = {"preset": "ultrafast", "tune": "zerolatency"}
    enc.open()
    out = []
    import numpy as np
    for i in range(n):
        f = av.VideoFrame.from_ndarray(np.full((h, w, 3), i * 20, np.uint8), format="rgb24")
        f = f.reformat(format="yuv420p")
        f.pts = i
        out += [bytes(p) for p in enc.encode(f)]
    out += [bytes(p) for p in enc.encode(None)]
    return out


def opus_packets(n=20):
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate, enc.layout, enc.format = 48000, "stereo", "s16"
    enc.open()
    import numpy as np
    out = []
    for i in range(n):
        fr = av.AudioFrame.from_ndarray(np.zeros((1, 960 * 2), "<i2"), format="s16", layout="stereo")
        fr.sample_rate = 48000
        fr.pts = i * 960
        out += [bytes(p) for p in enc.encode(fr)]
    return out


async def ctl(obj):
    r, w = await asyncio.open_unix_connection(str(SOCK))
    w.write((json.dumps(obj) + "\n").encode())
    await w.drain()
    line = await r.readline()
    w.close()
    return json.loads(line)


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="monect-e2e-", dir=os.environ.get("TMPDIR")))
    await ctl({"cmd": "set_settings", "settings": {"security": "open", "clear_trusted": True}})
    print("Connection")
    ph = await Phone().connect()
    n = ph.disc[2]
    check("discovery reply (0x10 → 0x05, name, version 0x2b)",
          ph.disc[0] == 5 and struct.unpack(">I", ph.disc[3 + n:7 + n])[0] == 0x2B)
    check("connect 0x35 → 06", ph.conn == b"\x06")
    nm = "E2E Phone".encode("utf-16-le")
    a = await ph.ask(bytes([4, 2, 0]) + struct.pack(">I", len(nm)) + nm)
    check("auth hello → 09 00 01 + version 43", a == b"\x09\x00\x01\x00\x00\x00\x2b")
    caps = await ph.ask(b"\x0a\x01")
    c = json.loads(caps[2:])
    check("capabilities 14 01 + JSON with client keys",
          caps[:2] == b"\x14\x01" and c["taskManager"].get("webRtc")
          and c["gameCenter"].get("listGames") and c["remoteDesktop"].get("videoH264")
          and c["gamepad"].get("dsuInput") and c["projector"].get("screen"))
    st = await ctl({"cmd": "status"})
    check("UI status lists the phone", any(d["name"] == "E2E Phone" for d in st["devices"]))

    print("Input (unordered input channel)")
    mark = len(LOG.read_text(errors="replace"))
    ph.input(bytes([0, 0, 5, 0xFB, 1]))                       # mouse
    ph.input(bytes([1, 2, 0, 0x0B, 0, 0, 0, 0, 0]))           # shift+h
    ph.input(bytes([1, 0, 0, 0, 0, 0, 0, 0, 0]))
    ph.input(bytes([9, 2, 0]))                                # vol+
    ph.input(bytes([0x0D, 0x1E, 0x02]))                       # scancode
    ph.input(bytes([0x0C]) + struct.pack(">ii", 30000, 20000))  # abs pointer
    ph.input(bytes([7, 1]) + struct.pack(">IHHff", 0, 1, 6, 0.5, 0.5) + bytes(144))  # touch
    txt = "Merhaba ğüş@".encode("utf-16-le")
    ph.mbus.send(bytes([0, 6]) + struct.pack(">I", len(txt) + 2) + b"\0\0" + txt)
    await asyncio.sleep(2.5)
    for what, needle in (("mouse report", "mouse.report"), ("keyboard boot report", "kbd.boot_report"),
                         ("media keys", "kbd.consumer_bits"), ("scancode", "kbd.scancode"),
                         ("absolute pointer", "absptr.move"), ("multi-touch", "touch.update"),
                         ("typed text (pasteText)", "paste_text('Merhaba ğüş@'")):
        check(what, log_has(needle, mark))

    print("Gamepads + DSU motion")
    r = await ph.ask(bytes([0, 5, 0, 3, 3, 0, 2, 0, 4, 0]), want=b"\x04")
    check("plug x360 + dinput + DSU → 04 + 256", len(r) == 257)
    s = await ph.ask(bytes([0, 5, 2]), want=b"\x04\x02")
    check("pad status 04 02 03 nX 02 01 nD 04 nDSU", s == bytes([4, 2, 3, 1, 2, 1, 1, 4, 1]), s.hex())
    f = await ph.ask(bytes([0, 5, 3, 0]), want=b"\x04\x03")
    check("unplug flags get", f[:2] == b"\x04\x03" and len(f) == 5)
    f = await ph.ask(bytes([0, 5, 3, 1, 0, 1, 0]), want=b"\x04\x04")
    check("unplug flags set", f == b"\x04\x04\x01")
    mark = len(LOG.read_text(errors="replace"))
    ph.input(bytes([3, 0, 0]) + struct.pack("<HBBhhhh", 0x1000, 0, 255, 1000, -2000, 0, 0))
    ph.input(bytes([4, 0, 0, 0x41]) + struct.pack("<I8h", 5, 100, -100, 0, 0, 0, 0, 0, 0))
    d = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    d.settimeout(3)

    def dsuc(msg, payload=b""):
        body = struct.pack("<I", msg) + payload
        pkt = bytearray(b"DSUC" + struct.pack("<HHII", 1001, len(body), 0, 99) + body)
        struct.pack_into("<I", pkt, 8, zlib.crc32(pkt))
        return bytes(pkt)
    d.sendto(dsuc(0x100002, bytes(8)), ("127.0.0.1", 26760))
    rep = bytearray(67)
    rep[0] = 8
    rep[7:11] = b"\x80" * 4
    struct.pack_into("<Q", rep, 35, 4242)
    struct.pack_into("<3f", rep, 43, 0.1, -1.0, 0.2)
    struct.pack_into("<3f", rep, 55, 11.0, 22.0, 33.0)
    await asyncio.sleep(0.3)
    ph.input(bytes(rep))
    await asyncio.sleep(1.5)
    check("x360 report", log_has("pad.report", mark))
    check("dinput report", log_has("dpad.report", mark))
    try:
        pkt = await asyncio.get_running_loop().run_in_executor(None, d.recv, 200)
        check("DSU pad data to emulator (accel/gyro/timestamp)",
              len(pkt) == 100 and struct.unpack_from("<3f", pkt, 88) == (11.0, 22.0, 33.0)
              and struct.unpack_from("<Q", pkt, 68)[0] == 4242)
    except OSError:
        check("DSU pad data to emulator", False, "no packet")

    print("Remote desktop")
    start = bytearray(50)
    start[0:2] = b"\x01\x00"
    start[2] = 6
    start[4:8] = (2400).to_bytes(4, "big")
    start[8:12] = (1080).to_bytes(4, "big")
    start[19] = 60
    start[37] = 1
    start[40:44] = (1280).to_bytes(4, "big")
    start[44:48] = (720).to_bytes(4, "big")
    r = await ph.ask(bytes(start), timeout=15, want=b"\x01")
    check("start → 01 00 (H.264)", r[:3] == b"\x01\x00\x00", r.hex())
    mon = await ph.ask(b"\x01\x04", want=b"\x01\x05")
    cnt = mon[2]
    rects = [struct.unpack_from(">6i", mon, 3 + 24 * i) for i in range(cnt)]
    check("monitors info (client parser)", cnt >= 1 and rects[0][4] > rects[0][2], str(rects))
    dec = av.CodecContext.create("h264", "r")
    dp = Depacketizer()
    frames, t0, sizes = 0, time.monotonic(), []
    while time.monotonic() - t0 < 4:
        m = await ph.wait(b"\x05", 5)
        au = dp.push(m[1:])
        if au:
            sizes.append(len(au))
            for p in dec.parse(au):
                frames += len(dec.decode(p))
    check("video decodes (client depacketizer)", frames > 20, f"{frames} frames in 4 s")
    ph.mbus.send(b"\x01\x06")
    idr = False
    for _ in range(30):
        au = dp.push((await ph.wait(b"\x05", 5))[1:])
        if au and (b"\x00\x00\x01\x65" in au or b"\x00\x00\x00\x01\x65" in au):
            idr = True
            break
    check("keyframe on request (01 06 → IDR)", idr)
    fmt = await ph.ask(b"\x02\x00", want=b"\x02\x00")
    check("audio format 02 00", len(fmt) == 24 and fmt[2:4] == b"\xff\xfe")
    r = await ph.ask(b"\x02\x06", want=b"\x02\x03")
    check("audio start (opus) → 02 03", r == b"\x02\x03")
    od = av.CodecContext.create("libopus", "r")
    od.sample_rate, od.layout = 48000, "stereo"
    od.open()
    adp, samples = Depacketizer(), 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2:
        m = await ph.wait(None, 5)
        if m[0] == 0x0E:
            pk = adp.push(m[1:])
            if pk:
                samples += sum(fr.samples for fr in od.decode(av.Packet(pk)))
    check("PC audio decodes (opus 0x0e)", samples > 48000, f"{samples} samples")
    r = await ph.ask(b"\x02\x02", want=b"\x02\x02")
    check("audio stop → 02 02", r == b"\x02\x02")
    r = await ph.ask(b"\x01\x01", want=b"\x01\x03")
    check("remote desktop stop → 01 03", r == b"\x01\x03")

    print("PC microphone → phone")
    r = await ph.ask(b"\x02\x07", want=b"\x02\x07")
    check("mic format 02 07", len(r) == 24)
    r = await ph.ask(b"\x02\x08", want=b"\x02\x0b")
    check("mic start → 02 0b", r == b"\x02\x0b")
    m = await ph.wait(b"\x0f", 5)
    check("mic packets (0x0f)", m[0] == 0x0F)
    ph.mbus.send(b"\x02\x09")

    print("File explorer")
    (tmp / "hello.txt").write_bytes(os.urandom(50_000))
    for i in range(40):
        (tmp / f"f{i:02}.dat").write_text("x")
    wdir = "L:" + str(tmp).replace("/", "\\")
    ph.drain()
    ph.mbus.send(b"\x06\x00" + wstr("\\"))
    root = await ph.wait(b"\x0b\x00")
    names = [root[6 + 548 * i + 28:6 + 548 * i + 548].decode("utf-16-le").split("\0")[0]
             for i in range((len(root) - 6) // 548)]
    check("root listing (drive + special folders)", "L:\\" in names and len(names) >= 2, str(names[:3]))
    ph.drain()
    ph.mbus.send(b"\x06\x00" + wstr(wdir))
    total, got_entries = None, []
    while total is None or len(got_entries) < total:
        m = await ph.wait(b"\x0b\x00")
        total = struct.unpack(">I", m[2:6])[0]
        got_entries += [m[6 + 548 * i + 28:6 + 548 * i + 548].decode("utf-16-le").split("\0")[0]
                        for i in range((len(m) - 6) // 548)]
    check("folder listing across chunks", total == 41 and "hello.txt" in got_entries, f"{total} items")
    sp = await ph.ask(b"\x06\x07\x00\x02\x01\x04", want=b"\x0b\x06")
    check("special folders 0b 06", sp[2] == 2)
    dn = await ph.ask(b"\x06\x0b" + wstr(wdir + "\\hello.txt"), want=b"\x0b\x01")
    ln = struct.unpack(">I", dn[2:6])[0]
    l2 = struct.unpack(">I", dn[6 + ln:10 + ln])[0]
    check("display name 0b 01", dn[10 + ln:10 + ln + l2].decode("utf-16-le") == "hello.txt")
    ph.drain()
    ph.mbus.send(b"\x06\x01" + wstr(wdir + "\\hello.txt"))
    first = await ph.wait(b"\x0b\x04")
    size = struct.unpack(">Q", first[27:35])[0]
    nl = struct.unpack(">I", first[35:39])[0]
    o = 39 + nl
    cl = struct.unpack(">I", first[o:o + 4])[0]
    data = first[o + 4:o + 4 + cl]
    while len(data) < size:
        m = await ph.wait(b"\x0b\x04\x01")
        cl = struct.unpack(">I", m[11:15])[0]
        data += m[15:15 + cl]
    check("download to phone (content identical)", data == (tmp / "hello.txt").read_bytes())
    payload = os.urandom(40_000)
    req = (b"\x06\x08\x00" + hashlib.md5(payload).digest() + b"\x00"
           + struct.pack(">Q", len(payload)) + b"\x00\x00" + "up.bin".encode("utf-16-le")
           + b"\0\0" + b"\0\0" + wdir.encode("utf-16-le") + b"\0\0")
    r = await ph.ask(req, want=b"\x0b\x07")
    tid = struct.unpack(">Q", r[3:11])[0]
    check("upload start → 0b 07 00 id", r[2] == 0)
    for i in range(0, len(payload), 16000):
        ch = payload[i:i + 16000]
        ph.mbus.send(b"\x06\x08\x01" + struct.pack(">QI", tid, len(ch)) + ch)
    r = await ph.wait(b"\x0b\x07\x01", 10)
    check("upload done, MD5 verified", (tmp / "up.bin").read_bytes() == payload)
    r = await ph.ask(b"\x06\x05" + wstr(wdir + "\\up.bin") + wstr("renamed.bin"), want=b"\x0b\x02")
    check("rename → 0b 02 01", r[2] == 1 and (tmp / "renamed.bin").exists())
    r = await ph.ask(b"\x06\x02" + wstr(wdir + "\\renamed.bin"), want=b"\x0b\x03")
    check("delete → 0b 03 01", r[2] == 1 and not (tmp / "renamed.bin").exists())

    print("Task manager")
    r = await ph.ask(b"\x0b\x00", want=b"\x15\x00")
    st = json.loads(r[3:])
    check("stats", r[2] == 3 and st["cores"] > 0 and st["memTotal"] > 0)
    ph.drain()
    ph.mbus.send(b"\x0b\x01")
    buf = b""
    while True:
        m = await ph.wait(b"\x15\x01")
        buf += m[3:]
        if m[2] & 2:
            break
    procs = json.loads(buf)["processes"]
    check("process list (chunked)", len(procs) > 20, f"{len(procs)} processes")
    victim = subprocess.Popen(["sleep", "300"])
    r = await ph.ask(b"\x0b\x03" + json.dumps({"pid": victim.pid}).encode(), want=b"\x15\x03")
    check("process details", json.loads(r[3:])["name"] == "sleep")
    r = await ph.ask(b"\x0b\x02" + json.dumps({"pid": victim.pid, "name": "sleep"}).encode(),
                     want=b"\x15\x02")
    time.sleep(0.3)
    check("kill process", json.loads(r[3:])["ok"] and victim.poll() is not None)

    print("Games, emulator launcher, power widget")
    r = await ph.ask(b"\x07\x00", want=b"\x10\x00")
    pl = Depacketizer().push(r[2:])
    groups = struct.unpack(">I", pl[:4])[0] if pl else 0
    check("game list (10 00, seq framed)", pl is not None and groups >= 0, f"{groups} groups")
    r = await ph.ask(b"\x07\x01" + wstr("nope") + wstr("nothing"), want=b"\x10\x02")
    check("launch unknown game → error status", r == b"\x10\x02\x01")
    r = await ph.ask(b"\x08\x00" + wstr("open") + wstr("L:\\usr\\bin\\true") + wstr(""), want=b"\x11")
    check("ShellExecute open → 11 00 >32", r[:2] == b"\x11\x00" and struct.unpack(">i", r[2:6])[0] > 32)
    mark = len(LOG.read_text(errors="replace"))
    ph.udp.sendto(b"\x0f\x0a", H)
    await asyncio.sleep(0.5)
    check("power widget over UDP (lock, dry-run)", log_has("helper action 10", mark))

    print("Projector (phone screen on PC)")
    r = await ph.ask(b"\x05\x00", want=b"\x0a")
    check("start → 0a 00", r == b"\x0a\x00")
    r = await ph.ask(b"\x05\x01" + struct.pack("<HI", 0, 3) + b"\x02\x04\x03", want=b"\x0a")
    check("functions → 0a 02", r == b"\x0a\x02")
    afmt = bytes([3]) + struct.pack(">I", 48000) + bytes([2]) + struct.pack(">I", 3840)
    r = await ph.ask(b"\x05\x01" + struct.pack("<HI", 5, len(afmt)) + afmt, want=b"\x0a")
    check("audio format → 0a 05", r == b"\x0a\x05")
    vids = []
    Packetizer(0, lambda m: vids.append(m[1:])).push(b"".join(h264_frames()))
    for p in h264_frames():
        Packetizer(0, lambda m: vids.append(m[1:])).push(p)
    for v in vids:
        ph.mbus.send(b"\x05\x01" + struct.pack("<HI", 1, len(v)) + v)
    auds = []
    pk = Packetizer(0, lambda m: auds.append(m[1:]))
    for p in opus_packets():
        pk.push(p)
    for a_ in auds:
        body = b"\0\0\0\0" + a_
        ph.mbus.send(b"\x05\x01" + struct.pack("<HI", 6, len(body)) + body)
    await asyncio.sleep(1.5)
    r = await ph.ask(b"\x05\x02", want=b"\x0a")
    check("stop → 0a 03", r == b"\x0a\x03")
    if DUMP and DUMP.exists():
        dec = av.CodecContext.create("h264", "r")
        n = sum(len(dec.decode(p)) for p in dec.parse(DUMP.read_bytes()))
        check("projector video decodes on PC", n >= 8, f"{n} frames")
    await asyncio.sleep(0.3)
    txt = LOG.read_text(errors="replace")
    i = txt.rfind("audio samples played")
    played = int(txt[txt.rfind("(", 0, i) + 1:i].strip()) if i > 0 else 0
    check("projector audio decodes on PC", played > 0, f"{played} samples")

    print("Virtual camera (phone → webcam)")
    r = await ph.ask(b"\x03\x02" + json.dumps({"name": "E2E Cam", "width": 320, "height": 240,
                                                "frameRate": 30, "bitrate": 500000}).encode(),
                     want=b"\x03\x05")
    handle = r[2:10]
    check("create → 03 05 handle", len(r) == 10)
    lst = await ph.ask(b"\x03\x03", want=b"\x03\x08")
    check("camera list 03 08 JSON", json.loads(lst[2:])[0]["name"] == "E2E Cam")
    mark = len(LOG.read_text(errors="replace"))
    pk = Packetizer(0, lambda m: ph.mbus.send(b"\x03\x05" + handle + m[1:]))
    for p in h264_frames():
        pk.push(p)
    await asyncio.sleep(1.5)
    check("camera frames decode on PC", log_has("phone camera: first frame", mark))
    r = await ph.ask(b"\x03\x00\x00\x01\x00\x00\x04\x38\x00\x00\x02\xd0", want=b"\x03")
    check("PC webcam request answered (no camera → 03 04)", r == b"\x03\x04")

    await ph.close()
    await asyncio.sleep(1)
    st = await ctl({"cmd": "status"})
    check("disconnect removes the phone from the UI list",
          not any(d["name"] == "E2E Phone" for d in st["devices"]))

    print("Security modes")
    await ctl({"cmd": "set_settings", "settings": {"security": "password", "password": "e2e-pw",
                                                   "remember_devices": False, "clear_trusted": True}})
    ph = await Phone().connect("Pw Phone")
    nm = "Pw Phone".encode("utf-16-le")
    hello = bytes([4, 2, 0]) + struct.pack(">I", len(nm)) + nm
    check("password required → 09 01 00", await ph.ask(hello) == b"\x09\x01\x00")
    bad = bytes([4, 1]) + struct.pack(">I", 32) + hashlib.sha256(b"x").digest()
    check("wrong password → 09 00 05", await ph.ask(bad) == b"\x09\x00\x05")
    good = bytes([4, 1]) + struct.pack(">I", 32) + hashlib.sha256(b"e2e-pw").digest()
    check("right password → accepted", (await ph.ask(good))[:3] == b"\x09\x00\x01")
    await ph.close()
    await ctl({"cmd": "set_settings", "settings": {"security": "confirm"}})
    r_, w_ = await asyncio.open_unix_connection(str(SOCK))

    async def fake_ui():
        while True:
            m = json.loads(await r_.readline())
            if m.get("event") == "confirm":
                w_.write((json.dumps({"cmd": "confirm", "id": m["id"], "accept": True}) + "\n").encode())
                await w_.drain()
                return
    t = asyncio.ensure_future(fake_ui())
    ph = await Phone().connect("Ask Phone")
    ph.mbus.send(hello)
    a1, a2 = await ph.wait(), await ph.wait()
    check("ask-before-connect → 09 00 00 then accepted", a1 == b"\x09\x00\x00" and a2[:3] == b"\x09\x00\x01")
    t.cancel()
    w_.close()
    await ph.close()
    await ctl({"cmd": "set_settings", "settings": {"security": "open", "remember_devices": True,
                                                   "clear_trusted": True}})

    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    return 0 if not bad else 1


sys.exit(asyncio.run(main()))
