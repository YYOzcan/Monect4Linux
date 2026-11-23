"""Projector: the phone's screen (and audio) shown on the PC (category 0x05).

Phone -> PC:  05 00 start | 05 02 stop |
              05 01 func(LE u16) len(LE u32) payload   (func 0 capabilities,
              1 H.264 video, 5 audio format, 6 audio data with 4-byte tag)
PC -> phone:  0A 00 launched | 0A 02 function set | 0A 03 stopped | 0A 05 audio applied
"""
from __future__ import annotations

import logging
import shutil
import struct
import subprocess

from .packetizer import Depacketizer

log = logging.getLogger("projector")

AAC, ADPCM, OPUS = 1, 2, 3


class Projector:
    def __init__(self) -> None:
        self.video: subprocess.Popen | None = None
        self.audio: subprocess.Popen | None = None
        self.audio_dec = None
        self.audio_codec = 0
        self.vdepack = Depacketizer()
        self.adepack = Depacketizer()

    # ---- players -----------------------------------------------------------------
    def _video_player(self):
        import os
        dump = os.environ.get("MONECT_PROJECTOR_DUMP")
        if dump:                                   # test hook: write the stream to a file
            if self.video is None:
                class _F:
                    def __init__(self, path):
                        self.stdin = open(path, "wb")

                    def poll(self):
                        return None

                    def terminate(self):
                        pass
                self.video = _F(dump)
            return self.video
        if self.video and self.video.poll() is None:
            return self.video
        if shutil.which("ffplay"):
            cmd = ["ffplay", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
                   "-framedrop", "-probesize", "32", "-sync", "ext", "-f", "h264",
                   "-window_title", "Monect Projector", "-i", "pipe:0"]
        elif shutil.which("mpv"):
            cmd = ["mpv", "--no-cache", "--profile=low-latency", "--demuxer-lavf-format=h264",
                   "--title=Monect Projector", "-"]
        else:
            log.warning("neither ffplay nor mpv installed; cannot show the projector")
            return None
        self.video = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        log.info("projector window opened")
        return self.video

    def _start_audio(self, codec: int, rate: int, channels: int) -> bool:
        self._stop_audio()
        self.audio_codec = codec
        if codec == OPUS:
            import av
            self.audio_dec = av.CodecContext.create("libopus", "r")
            self.audio_dec.sample_rate = rate
            self.audio_dec.layout = "stereo" if channels == 2 else "mono"
            self.audio_dec.open()
        elif codec != 0 and codec not in (OPUS,):
            log.info("projector audio codec %d not supported", codec)
            return False
        import os
        if os.environ.get("MONECT_PROJECTOR_DUMP"):
            self.audio = subprocess.Popen(["cat"], stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL)
            self.rate, self.channels = rate, channels
            return True
        self.audio = subprocess.Popen(
            ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={rate}",
             f"--channels={channels}", "--latency-msec=60", "--client-name=Monect Projector"],
            stdin=subprocess.PIPE)
        self.rate, self.channels = rate, channels
        return True

    def _stop_audio(self) -> None:
        if self.audio:
            try:
                self.audio.stdin.close()
                self.audio.terminate()
            except Exception:
                pass
            self.audio = None
        self.audio_dec = None

    # ---- protocol ------------------------------------------------------------------
    def handle(self, b: bytes, send) -> None:
        """b = message without the category byte."""
        sub = b[0]
        if sub == 0x00:
            log.info("phone projector session started")
            send(b"\x0a\x00")
        elif sub == 0x02:
            self.stop()
            send(b"\x0a\x03")
        elif sub == 0x01 and len(b) >= 7:
            func = struct.unpack_from("<H", b, 1)[0]
            ln = struct.unpack_from("<I", b, 3)[0]
            payload = b[7:7 + ln]
            if func == 1:
                frame = self.vdepack.push(payload)
                p = self._video_player() if frame else None
                if p:
                    try:
                        p.stdin.write(frame)
                        p.stdin.flush()
                    except (BrokenPipeError, OSError):
                        self.video = None
            elif func == 6 and len(payload) > 4:
                self._audio(payload[4:])
            elif func == 5 and len(payload) >= 6:
                codec = payload[0]
                rate = struct.unpack_from(">I", payload, 1)[0]      # BE (observed: 00 00 bb 80)
                channels = payload[5]
                ok = self._start_audio(codec, rate or 48000, channels or 2)
                log.info("projector audio codec=%d %d Hz x%d -> %s", codec, rate, channels,
                         "ok" if ok else "unsupported")
                send(b"\x0a\x05")
            elif func == 0:
                log.info("projector functions offered: %s", payload.hex(" "))
                send(b"\x0a\x02")
            else:
                log.debug("projector func %d (%d bytes)", func, len(payload))
        else:
            log.info("projector sub=0x%02x %s", sub, b[:32].hex(" "))

    def _audio(self, frame: bytes) -> None:
        if not self.audio:
            return
        try:
            if self.audio_dec is not None:
                import av
                for f in self.audio_dec.decode(av.Packet(frame)):
                    self.audio_samples = getattr(self, "audio_samples", 0) + f.samples
                    pcm = f.to_ndarray()
                    if pcm.dtype.kind == "f":
                        pcm = (pcm.clip(-1, 1) * 32767).astype("<i2")
                    if pcm.ndim == 2 and pcm.shape[0] > 1:      # planar -> interleaved
                        pcm = pcm.T
                    self.audio.stdin.write(pcm.tobytes())
            else:
                self.audio.stdin.write(frame)
        except (BrokenPipeError, OSError):
            self._stop_audio()
        except Exception as ex:
            log.debug("projector audio decode error: %s", ex)

    def stop(self) -> None:
        if self.video:
            try:
                self.video.stdin.close()
                self.video.terminate()
            except Exception:
                pass
            self.video = None
        self._stop_audio()
        log.info("projector stopped (%d audio samples played)", getattr(self, "audio_samples", 0))
        self.audio_samples = 0
