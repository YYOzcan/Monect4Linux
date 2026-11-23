"""Simulates the Android app against a running receiver (protocol self-test)."""
import asyncio
import json
import struct
import sys

from aiortc import RTCPeerConnection, RTCSessionDescription

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from monect_rx import signaling as sig  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = 28451


class Phone(asyncio.DatagramProtocol):
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    def connection_made(self, t):
        self.t = t

    def datagram_received(self, data, addr):
        self.q.put_nowait(data)


async def main():
    loop = asyncio.get_running_loop()
    t, ph = await loop.create_datagram_endpoint(Phone, local_addr=("0.0.0.0", 0))

    t.sendto(bytes([0x10, 4]) + b"\x11" * 8, (HOST, PORT))
    r = await asyncio.wait_for(ph.q.get(), 3)
    n = r[2]
    print("discovery reply: name=%r ver=0x%x hwid=%s" % (
        r[3:3 + n].decode("utf-16-le"), struct.unpack(">I", r[3 + n:7 + n])[0],
        r[7 + n:39 + n].decode()))

    name = "Test Phone".encode("utf-16-le")
    t.sendto(bytes([0x12, 4]) + b"\x22" * 8 + bytes([len(name)]) + name
             + bytes([5]) + b"7.9.3" + bytes([4]) + b"abcd", (HOST, PORT))
    r = await asyncio.wait_for(ph.q.get(), 3)
    print("connect reply:", r.hex())
    assert r == b"\x06"

    from aiortc import VideoStreamTrack
    pc = RTCPeerConnection()
    pc.addTrack(VideoStreamTrack())          # "phone camera"
    ch = pc.createDataChannel("input")
    got = asyncio.Queue()
    ch.on("message", lambda m: got.put_nowait(m))
    opened = asyncio.Event()
    ch.on("open", opened.set)

    await pc.setLocalDescription(await pc.createOffer())
    t.sendto(b"\x33" + sig.build_sdp(77, "offer", pc.localDescription.sdp), (HOST, PORT))

    while True:
        r = await asyncio.wait_for(ph.q.get(), 10)
        if r[0] != 0x33:
            continue
        m = sig.parse(r[1:])
        if isinstance(m, sig.AckMsg):
            print("ack for", m.msg_id.hex())
            continue
        if isinstance(m, sig.SdpMsg):
            print("got", m.sdp_type)
            t.sendto(b"\x33" + sig.build_ack(m.msg_id), (HOST, PORT))
            await pc.setRemoteDescription(RTCSessionDescription(m.sdp, m.sdp_type))
            break

    await asyncio.wait_for(opened.wait(), 15)
    print("data channel open")
    ch.send(b"\x0a")
    cap = await asyncio.wait_for(got.get(), 5)
    print("capabilities:", cap[:2].hex(), json.loads(cap[2:]))

    ch.send(bytes([0, 0, 0, 5, 0xFB, 1]))                    # mouse move + wheel
    ch.send(bytes([0, 1, 0x02, 0, 0x0B, 0, 0, 0, 0, 0]))    # shift + h
    ch.send(bytes([0, 1, 0, 0, 0, 0, 0, 0, 0, 0]))          # release
    ch.send(bytes([0, 9, 0x02, 0]))                         # volume up
    ch.send(bytes([0, 0x0D, 0x1E, 0x02]))                   # scancode 'a' down
    ch.send(bytes([0, 5, 0, 1, 3, 0]))                      # plug x360 #0
    plug = await asyncio.wait_for(got.get(), 5)
    print("plug reply:", plug[:4].hex(), len(plug))
    ch.send(bytes([0, 3, 0, 0]) + struct.pack("<HBBhhhh", 0x1000, 0, 255, 1000, -2000, 0, 0))
    txt = "Merhaba ğüşıöç".encode("utf-16-le")
    ch.send(bytes([0, 6]) + struct.pack(">I", len(txt) + 2) + b"\0\0" + txt)
    async def ask(msg, n=1, t=5):
        while not got.empty():
            got.get_nowait()
        ch.send(msg)
        return [await asyncio.wait_for(got.get(), t) for _ in range(n)]

    name = "Tester".encode("utf-16-le")
    print("auth:", (await ask(bytes([4, 2, 0]) + struct.pack(">I", len(name)) + name))[0].hex())
    print("monitors:", (await ask(b"\x01\x04"))[0].hex())
    print("audio fmt:", (await ask(b"\x02\x00"))[0].hex())
    r = (await ask(b"\x02\x01"))[0]
    print("audio start:", r.hex())
    pk = [await asyncio.wait_for(got.get(), 5) for _ in range(3)]
    print("audio pkts:", [(p[0], len(p)) for p in pk])
    ch.send(b"\x02\x02")
    while True:
        r = await asyncio.wait_for(got.get(), 5)
        if r[0] == 2:
            print("audio stop:", r.hex()); break
    root = (await ask(b"\x06\x00\x00\x00" + "\\".encode("utf-16-le") + b"\0\0"))[0]
    print("root: count", struct.unpack(">I", root[2:6])[0],
          [root[6 + i*548 + 28: 6 + i*548 + 28 + 80].decode("utf-16-le").split("\0")[0]
           for i in range((len(root) - 6) // 548)])
    home = "L:" + str(__import__("pathlib").Path.home()).replace("/", "\\")
    r = (await ask(b"\x06\x00\x00\x00" + home.encode("utf-16-le") + b"\0\0"))[0]
    print("home: total", struct.unpack(">I", r[2:6])[0], "in first msg", (len(r) - 6) // 548)
    await asyncio.sleep(0.3)
    sp = (await ask(b"\x06\x07\x00\x02\x01\x04"))[0]
    print("special:", sp[:3].hex(), sp[3:].decode("utf-16-le", "replace")[:80])
    tmp = "/tmp/claude-1000/monect-dl-test.txt"
    open(tmp, "w").write("x" * 40000)
    r = await ask(b"\x06\x01\x00\x00" + ("L:" + tmp.replace("/", "\\")).encode("utf-16-le") + b"\0\0", 3)
    print("download pkts:", [(p[:3].hex(), len(p)) for p in r])
    tm = (await ask(b"\x0b\x00"))[0]
    print("taskmgr stats:", tm[:3].hex(), json.loads(tm[3:]))
    ch.send(b"\x0b\x01")
    buf = b""
    while True:
        r = await asyncio.wait_for(got.get(), 10)
        if r[0] != 0x15:
            continue
        buf += r[3:]
        if r[2] & 2:
            break
    print("taskmgr processes:", len(json.loads(buf)["processes"]))
    print("pc mic fmt:", (await ask(b"\x02\x07"))[0].hex())
    cam = (await ask(b"\x03\x02" + json.dumps({"name": "Test", "width": 640, "height": 480}).encode()))[0]
    print("camera create:", cam.hex(), "list:", (await ask(b"\x03\x03"))[0][2:].decode())
    ch.send(bytes([0, 5, 0, 1, 2, 0]))                      # plug dinput #0
    await asyncio.sleep(0.2)
    ch.send(bytes([0, 4, 0, 0, 0x41]) + struct.pack("<I8h", 5, 100, -100, 0, 0, 0, 0, 0, 0))
    # remote desktop (needs MONECT_SCREEN_TEST=1 on the receiver to avoid the portal)
    start = bytearray(0x32); start[0:2] = b"\x01\x00"; start[3] = 30
    start[4:8] = (1280).to_bytes(4, "big"); start[8:12] = (720).to_bytes(4, "big")
    r = (await ask(bytes(start), t=15))[0]
    print("screen start:", r.hex())
    import av
    dec = av.CodecContext.create("h264", "r")
    frames, fu = 0, bytearray()
    while frames < 10:
        p = await asyncio.wait_for(got.get(), 10)
        if p[0] != 5:
            continue
        flag = p[3]
        if flag == 0:
            au = bytes(p[4:])
        else:
            if p[4] & 0x80:
                fu = bytearray()
            fu += p[5:]
            if not p[4] & 0x40:
                continue
            au = bytes(fu)
        for pkt in dec.parse(au):
            frames += len(dec.decode(pkt))
    print("decoded video frames:", frames, dec.width, "x", dec.height)
    print("screen stop:", (await ask(b"\x01\x01"))[0].hex())
    await asyncio.sleep(0.5)
    t.sendto(b"\x33" + sig.build_bye(), (HOST, PORT))
    await pc.close()
    print("SELFTEST OK")


asyncio.run(main())
