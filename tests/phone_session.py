"""Minimal fake-phone session helper for protocol tests against a running receiver."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiortc import RTCPeerConnection, RTCSessionDescription
from monect_rx import signaling as sig

H = ("127.0.0.1", 28451)


class _P(asyncio.DatagramProtocol):
    def __init__(self): self.q = asyncio.Queue()
    def datagram_received(self, d, a): self.q.put_nowait(d)


class Phone:
    async def connect(self, peer_id: bytes = None):
        import os
        loop = asyncio.get_running_loop()
        self.udp, self.p = await loop.create_datagram_endpoint(_P, local_addr=("127.0.0.1", 0))
        pid = peer_id or os.urandom(8)
        self.udp.sendto(b"\x35" + b"\x11" * 8 + pid + bytes.fromhex("06fffe5400650000") + b"\x059.1.4\x0aAndroid_15", H)
        assert await asyncio.wait_for(self.p.q.get(), 3) == b"\x06"
        self.pc = RTCPeerConnection()
        self.mbus = self.pc.createDataChannel("mbus")
        self.got = asyncio.Queue()
        self.mbus.on("message", self.got.put_nowait)
        op = asyncio.Event(); self.mbus.on("open", op.set)
        await self.pc.setLocalDescription(await self.pc.createOffer())
        self.udp.sendto(b"\x33" + sig.build_sdp(1, "offer", self.pc.localDescription.sdp), H)
        while True:
            m = sig.parse((await asyncio.wait_for(self.p.q.get(), 10))[1:])
            if isinstance(m, sig.SdpMsg):
                self.udp.sendto(b"\x33" + sig.build_ack(m.msg_id), H)
                await self.pc.setRemoteDescription(RTCSessionDescription(m.sdp, m.sdp_type))
                break
        await asyncio.wait_for(op.wait(), 10)
        return self

    async def ask(self, msg: bytes, timeout=8) -> bytes:
        while not self.got.empty():
            self.got.get_nowait()
        self.mbus.send(msg)
        return bytes(await asyncio.wait_for(self.got.get(), timeout))

    async def close(self):
        self.udp.sendto(b"\x33" + sig.build_bye(), H)
        await self.pc.close()
