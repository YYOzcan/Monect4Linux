"""PC audio -> phone (what the PC is playing, captured from the default sink)."""
from __future__ import annotations

import asyncio
import logging
import struct
import subprocess

from .packetizer import Packetizer

log = logging.getLogger("audio")

RATE = 48000
CHANNELS = 2
PCM, ADPCM, OPUS = 0x06, 0x0C, 0x0E
MIC_OPUS = 0x0F             # PC microphone -> phone
FRAME_MS = 10


def format_reply(latency_ms: int = 20) -> bytes:
    """[0x02, 0x00] + description of the capture format (mirrors the Windows mix format)."""
    bits = 32
    return b"\x02\x00" + struct.pack(">HHIIHHHI", 0xFFFE, CHANNELS, RATE,
                                     (bits * RATE) >> 2, bits >> 2, bits, 22, latency_ms)


class AudioStreamer:
    def __init__(self, send) -> None:
        self.send = send
        self.proc: subprocess.Popen | None = None
        self.task: asyncio.Task | None = None
        self.volume = 100

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self, codec: int, source: str = "@DEFAULT_MONITOR@",
                    prefix: int | None = None) -> bool:
        await self.stop()
        cmd = ["parec", "-d", source, "--format=s16le", f"--rate={RATE}",
               f"--channels={CHANNELS}", "--raw", f"--latency-msec={FRAME_MS}"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        except OSError as ex:
            log.error("cannot start parec: %s", ex)
            return False
        self.task = asyncio.ensure_future(self._pump(codec, prefix or codec))
        log.info("audio stream from %s started (%s)", source,
                 {PCM: "pcm", OPUS: "opus", ADPCM: "adpcm"}[codec])
        return True

    async def _pump(self, codec: int, prefix: int) -> None:
        loop = asyncio.get_running_loop()
        pk = Packetizer(prefix, self.send)
        frame_bytes = RATE * CHANNELS * 2 * FRAME_MS // 1000
        enc = None
        if codec == OPUS:
            import av
            enc = av.CodecContext.create("libopus", "w")
            enc.sample_rate = RATE
            enc.layout = "stereo"
            enc.format = "s16"
            enc.bit_rate = 128000
            enc.options = {"application": "lowdelay", "frame_duration": str(FRAME_MS)}
            enc.open()
        try:
            while True:
                data = await loop.run_in_executor(None, self.proc.stdout.read, frame_bytes)
                if not data:
                    break
                if enc is None:
                    pk.push(data)
                    continue
                import av
                frame = av.AudioFrame.from_ndarray(
                    __import__("numpy").frombuffer(data, "<i2").reshape(1, -1),
                    format="s16", layout="stereo")
                frame.sample_rate = RATE
                for pkt in enc.encode(frame):
                    pk.push(bytes(pkt))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("audio pump failed")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None
        if self.proc:
            self.proc.terminate()
            self.proc = None

    @staticmethod
    def get_volume() -> int:
        try:
            out = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                                 capture_output=True, text=True, timeout=3).stdout
            return int(out.split("%")[0].split()[-1])
        except Exception:
            return 0

    @staticmethod
    def set_volume(v: int) -> None:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{max(0, min(v, 150))}%"],
                       timeout=3, check=False)
