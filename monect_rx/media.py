"""Phone camera -> v4l2loopback webcam, phone microphone -> PipeWire source.

The phone publishes these as WebRTC media tracks on the existing peer
connection; aiortc decodes them and we forward raw frames/samples.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("media")


def find_loopback() -> str | None:
    for dev in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            name = Path(dev, "name").read_text().strip().lower()
        except OSError:
            continue
        if "monect" in name or "loopback" in name or "dummy" in name:
            node = "/dev/" + os.path.basename(dev)
            if os.access(node, os.W_OK):
                return node
    return None


class VirtualCamera:
    """Writes decoded frames into a v4l2loopback device through ffmpeg."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.size = (0, 0)
        self.device = None
        self.next_try = 0.0

    def _open(self, w: int, h: int) -> bool:
        import time
        if time.monotonic() < self.next_try:
            return False
        self.close()
        self.device = find_loopback()
        if not self.device:
            self.next_try = time.monotonic() + 5
            log.warning("no writable v4l2loopback device; run: sudo modprobe v4l2loopback "
                        "card_label='Monect Camera' exclusive_caps=1")
            return False
        self.proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "yuv420p",
             "-s", f"{w}x{h}", "-i", "pipe:0", "-f", "v4l2", "-pix_fmt", "yuv420p", self.device],
            stdin=subprocess.PIPE)
        self.size = (w, h)
        log.info("virtual camera %s %dx%d", self.device, w, h)
        return True

    def write(self, frame) -> None:
        w, h = frame.width, frame.height
        if (w, h) != self.size or self.proc is None:
            if not self._open(w, h):
                return
        try:
            self.proc.stdin.write(frame.to_ndarray(format="yuv420p").tobytes())
        except (BrokenPipeError, OSError):
            self.close()

    def close(self) -> None:
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        self.size = (0, 0)


class VirtualMic:
    """PipeWire/Pulse pipe-source fed with s16le stereo 48 kHz."""

    FIFO = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "monect-mic.fifo"

    def __init__(self) -> None:
        self.module = None
        self.fd = None
        self.resampler = None

    def open(self) -> bool:
        try:
            out = subprocess.run(
                ["pactl", "load-module", "module-pipe-source", "source_name=monect_mic",
                 f"file={self.FIFO}", "format=s16le", "rate=48000", "channels=2",
                 "source_properties=device.description=Monect\\ Mikrofon"],
                capture_output=True, text=True, timeout=5)
            self.module = out.stdout.strip() or None
            self.fd = os.open(self.FIFO, os.O_WRONLY | os.O_NONBLOCK)
            log.info("virtual microphone 'Monect Mikrofon' ready")
            return True
        except Exception as ex:
            log.warning("cannot create virtual microphone: %s", ex)
            return False

    def write(self, frame) -> None:
        import av
        if self.fd is None and not self.open():
            return
        if self.resampler is None:
            self.resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
        for f in self.resampler.resample(frame):
            try:
                os.write(self.fd, bytes(f.planes[0])[:f.samples * 4])
            except BlockingIOError:
                pass
            except OSError:
                self.close()
                return

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.module:
            subprocess.run(["pactl", "unload-module", self.module], timeout=5, check=False)
            self.module = None


async def consume(track, sink) -> None:
    """Pump an incoming aiortc track into a sink until it ends."""
    log.info("receiving %s track", track.kind)
    try:
        while True:
            frame = await track.recv()
            sink.write(frame)
    except Exception as ex:
        log.info("%s track ended (%s)", track.kind, type(ex).__name__)
    finally:
        sink.close()


class H264CameraSink:
    """Decodes the phone's H.264 camera stream and feeds a VirtualCamera."""

    def __init__(self) -> None:
        import av
        self.dec = av.CodecContext.create("h264", "r")
        self.cam = VirtualCamera()
        self.frames = 0
        from .packetizer import Depacketizer
        self.depack = Depacketizer()

    def feed(self, data: bytes) -> None:
        data = self.depack.push(data)
        if not data:
            return
        try:
            for pkt in self.dec.parse(data):
                for frame in self.dec.decode(pkt):
                    self.cam.write(frame)
                    self.frames += 1
                    if self.frames == 1:
                        log.info("phone camera: first frame %dx%d", frame.width, frame.height)
        except Exception as ex:
            log.debug("camera decode error: %s", ex)

    def close(self) -> None:
        self.cam.close()


def find_webcam() -> str | None:
    """First real V4L2 capture device (skips our own v4l2loopback 'Monect Camera')."""
    import glob
    for dev in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            name = open(dev + "/name").read().strip()
            idx = int(open(dev + "/index").read().strip() or 0)
        except (OSError, ValueError):
            continue
        if "Monect" in name or "loopback" in name.lower() or idx != 0:
            continue                     # index 0 = the capture node of a UVC camera
        node = "/dev/" + os.path.basename(dev)
        if os.access(node, os.R_OK):
            return node
    return None
