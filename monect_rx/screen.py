"""Remote desktop: Wayland screen capture -> H.264/HEVC -> data channel."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass

from .packetizer import Packetizer
from .portal import ScreenCastSession, Stream

log = logging.getLogger("screen")

VIDEO_PREFIX = 0x05


@dataclass
class StartRequest:
    codec: int          # byte 0x25 of the request (echoed back)
    monitor: int        # byte 2
    fps: int
    max_w: int
    max_h: int
    flags_tail: int     # byte 0x30 (echoed back)
    raw: bytes

    @classmethod
    def parse(cls, m: bytes) -> "StartRequest":
        """m = full message including the 0x01 0x00 header."""
        def u32(o):
            return struct.unpack(">I", m[o:o + 4])[0] if len(m) >= o + 4 else 0

        def b(o, d=0):
            return m[o] if len(m) > o else d
        # client layout: [2] screen inches, [4:12] phone screen WxH, [19] fps,
        # [21:37] decoder min/max W/H, [37] codec, [40:48] target WxH, [49] audio
        fps = b(0x13) or 30
        tw, th = u32(40), u32(44)
        if not (tw and th):
            tw, th = u32(4), u32(8)
        dec_max_w, dec_max_h = u32(25), u32(33)
        if dec_max_w and dec_max_h:
            tw, th = min(tw, max(dec_max_w, dec_max_h)), min(th, max(dec_max_w, dec_max_h))
        return cls(codec=b(0x25), monitor=0, fps=max(15, min(fps, 60)),
                   max_w=tw, max_h=th, flags_tail=b(0x30), raw=m)


def monitors() -> list[dict]:
    """Logical monitor geometry from kscreen-doctor (KDE), best effort."""
    out = []
    if shutil.which("kscreen-doctor"):
        try:
            j = json.loads(subprocess.run(["kscreen-doctor", "-j"], capture_output=True,
                                          text=True, timeout=5).stdout)
            for o in j.get("outputs", []):
                if not (o.get("enabled") and o.get("connected")):
                    continue
                mode = next((m for m in o.get("modes", []) if m["id"] == o.get("currentModeId")), None)
                if not mode:
                    continue
                scale = float(o.get("scale", 1)) or 1.0
                w, h = mode["size"]["width"], mode["size"]["height"]
                if o.get("rotation") in (2, 8):
                    w, h = h, w
                out.append({"x": o["pos"]["x"], "y": o["pos"]["y"],
                            "w": round(w / scale), "h": round(h / scale),
                            "primary": bool(o.get("priority") == 1 or o.get("primary")),
                            "name": o.get("name", "")})
        except Exception as ex:
            log.debug("kscreen-doctor failed: %s", ex)
    return out


def desktop_bbox(mons: list[dict]) -> tuple[int, int, int, int]:
    if not mons:
        return 0, 0, 1920, 1080
    x0 = min(m["x"] for m in mons)
    y0 = min(m["y"] for m in mons)
    x1 = max(m["x"] + m["w"] for m in mons)
    y1 = max(m["y"] + m["h"] for m in mons)
    return x0, y0, x1 - x0, y1 - y0


def pick_encoder(hevc: bool) -> list[str]:
    try:
        encs = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                              text=True, timeout=10).stdout
    except Exception:
        encs = ""
    order = (["hevc_nvenc", "hevc_vaapi", "libx265"] if hevc
             else ["h264_nvenc", "h264_vaapi", "libx264"])
    for name in order:
        if f" {name} " in encs:
            return [name]
    return ["libx264"]


class ScreenStreamer:
    """Low-latency remote desktop: GStreamer appsink -> PyAV NVENC -> data channel.

    * appsink keeps at most one frame and drops older ones (always newest image)
    * each encoded frame is sent immediately (no next-frame delimiter wait)
    * keyframes on demand (phone sends 01 06), re-encoding the last image if static
    * back-pressure: when the data channel buffer grows, frames are skipped and the
      stream resumes with a keyframe, so latency never accumulates
    """

    HIGH_WATER = 48 * 1024      # ~30 ms of video at 12 Mbit/s
    LOW_WATER = 16 * 1024

    def __init__(self, send, cfg: dict, token_file, buffered=lambda: 0) -> None:
        self.send = send                 # called in the event loop
        self.buffered = buffered         # () -> bytes queued in the data channel
        self.cfg = cfg
        self.token_file = token_file
        self.session: ScreenCastSession | None = None
        self.stream: Stream | None = None
        self.thread = None
        self.running_flag = False
        self.paused = False
        self.force_idr = True
        self.req: StartRequest | None = None
        self.geom = (0, 0, 1920, 1080)
        self.pipeline = None
        self.loop = None
        self.stats = {"sent": 0, "dropped": 0}

    @property
    def running(self) -> bool:
        return self.running_flag

    def request_keyframe(self) -> None:
        self.force_idr = True

    async def start(self, req: StartRequest) -> bool:
        await self.stop()
        self.req = req
        self.loop = asyncio.get_running_loop()
        test = bool(os.environ.get("MONECT_SCREEN_TEST"))
        if test:
            s = Stream(-1, 0, 1280, 720, 0, 0, None)
        else:
            try:
                self.session = ScreenCastSession(self.token_file)
                self.stream = await self.loop.run_in_executor(None, self.session.start)
            except Exception as ex:
                log.error("screen capture unavailable: %s", ex)
                self.session = None
                return False
            s = self.stream
        self.geom = (s.x, s.y, s.width or 1920, s.height or 1080)
        w, h = self.geom[2], self.geom[3]
        if req.max_w >= 320 and req.max_h >= 240:
            bw, bh = max(req.max_w, req.max_h), min(req.max_w, req.max_h)
            if w < h:
                bw, bh = bh, bw
            k = min(1.0, bw / w, bh / h)
            w, h = int(w * k), int(h * k)
        w, h = max(16, w & ~15 if w >= 64 else w & ~1), max(16, h & ~1)
        fps = req.fps
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        src = ("videotestsrc is-live=true pattern=ball" if test else
               f"pipewiresrc fd={s.fd} path={s.node_id} do-timestamp=true keepalive-time=1000 "
               "always-copy=true")
        desc = (f"{src} ! videoconvert n-threads=4 ! videoscale ! "
                f"video/x-raw,format=NV12,width={w},height={h} ! "
                "appsink name=sink max-buffers=1 drop=true sync=false emit-signals=false")
        self.pipeline = Gst.parse_launch(desc)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.running_flag = True
        self.paused = False
        self.force_idr = True
        import threading
        self.thread = threading.Thread(target=self._worker, args=(w, h, fps), daemon=True,
                                       name="rd-encoder")
        self.thread.start()
        return True

    def _make_encoder(self, w: int, h: int, fps: int):
        import av
        from fractions import Fraction
        br = int(self.cfg.get("bitrate", 8_000_000))
        for name, opts in (("h264_nvenc", {"preset": "p1", "tune": "ull", "zerolatency": "1",
                                           "rc": "cbr", "delay": "0", "forced-idr": "1",
                                           "rc-lookahead": "0", "bf": "0"}),
                           ("libx264", {"preset": "ultrafast", "tune": "zerolatency",
                                        "x264-params": "repeat-headers=1"})):
            try:
                enc = av.CodecContext.create(name, "w")
                enc.width, enc.height = w, h
                enc.pix_fmt = "nv12" if name.endswith("nvenc") else "yuv420p"
                enc.time_base = Fraction(1, 1000)
                enc.framerate = Fraction(fps, 1)
                enc.bit_rate = br
                enc.gop_size = fps * 4
                enc.max_b_frames = 0
                enc.options = dict(opts, maxrate=str(br),
                                   bufsize=str(max(br // fps, 64000)))  # ~1 frame VBV
                enc.open()
                log.info("remote desktop %dx%d@%d with %s (%.1f Mbit/s)", w, h, fps, name,
                         br / 1e6)
                return enc
            except Exception as ex:
                log.warning("encoder %s unavailable: %s", name, ex)
        raise RuntimeError("no H.264 encoder")

    def _worker(self, w: int, h: int, fps: int) -> None:
        import av
        import time
        import numpy as np
        sink = self.pipeline.get_by_name("sink")
        try:
            enc = self._make_encoder(w, h, fps)
        except Exception:
            log.exception("encoder init failed")
            self.running_flag = False
            return
        nv12 = enc.pix_fmt == "nv12"
        interval = 1.0 / fps
        last_sent = 0.0
        last_frame = None
        t0 = time.monotonic()
        congested = False
        frames = 0
        st_t, st_frames, st_skip, st_q, st_enc, st_n = time.monotonic(), 0, 0, 0, 0.0, 0
        while self.running_flag:
            sample = sink.emit("try-pull-sample", int(interval * 1e9))
            now = time.monotonic()
            if sample is not None:
                buf = sample.get_buffer()
                ok, info = buf.map(1)       # Gst.MapFlags.READ
                if not ok:
                    continue
                try:
                    arr = np.frombuffer(info.data, np.uint8)[: w * h * 3 // 2].copy()
                finally:
                    buf.unmap(info)
                last_frame = arr.reshape(h * 3 // 2, w)
            elif not (self.force_idr and last_frame is not None):
                continue                     # screen static and nothing requested
            if self.paused or last_frame is None:
                continue
            if now - last_sent < interval * 0.9 and not self.force_idr:
                continue                     # cap to requested fps
            # back-pressure: skip frames while the channel is congested
            queued = self.buffered()
            st_q = max(st_q, queued)
            if now - st_t >= 5:
                log.info("rd stats: %.1f fps, %d skipped, max queue %d KB, encode %.1f ms",
                         st_frames / (now - st_t), st_skip, st_q // 1024,
                         (st_enc / st_n * 1000) if st_n else 0)
                st_t, st_frames, st_skip, st_q, st_enc, st_n = now, 0, 0, 0, 0.0, 0
            if queued > self.HIGH_WATER or (congested and queued > self.LOW_WATER):
                if not congested:
                    log.debug("channel congested (%d bytes queued), skipping frames", queued)
                congested = True
                st_skip += 1
                self.stats["dropped"] += 1
                continue
            if congested:
                congested = False
                self.force_idr = True
            frame = av.VideoFrame.from_ndarray(last_frame, format="nv12")
            if not nv12:
                frame = frame.reformat(format="yuv420p")
            frame.pts = int((now - t0) * 1000)
            if self.force_idr:
                try:
                    frame.pict_type = av.video.frame.PictureType.I
                except Exception:
                    frame.pict_type = "I"
                self.force_idr = False
            te = time.monotonic()
            try:
                packets = enc.encode(frame)
                st_enc += time.monotonic() - te
                st_n += 1
            except Exception as ex:
                log.warning("encode failed: %s", ex)
                continue
            for pkt in packets:
                data = bytes(pkt)
                self.loop.call_soon_threadsafe(self.send, data)
                frames += 1
                st_frames += 1
                self.stats["sent"] += 1
                if frames == 1:
                    log.info("first video frame sent (%d bytes)", len(data))
            last_sent = now
        try:
            enc.close()
        except Exception:
            pass
        log.info("video stream ended (%d sent, %d skipped)", self.stats["sent"],
                 self.stats["dropped"])

    async def stop(self) -> None:
        self.running_flag = False
        if self.thread:
            await asyncio.get_running_loop().run_in_executor(None, self.thread.join, 2)
            self.thread = None
        if self.pipeline is not None:
            from gi.repository import Gst
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.stream:
            try:
                os.close(self.stream.fd)
            except OSError:
                pass
            self.stream = None
        if self.session:
            self.session.close()
            self.session = None

    def map_abs(self, nx: float, ny: float) -> tuple[float, float]:
        """Normalised position on the captured monitor -> 0..1 on the desktop bbox."""
        mons = monitors()
        bx, by, bw, bh = desktop_bbox(mons)
        x, y, w, h = self.geom
        return ((x + nx * w) - bx) / bw, ((y + ny * h) - by) / bh


def monitors_payload() -> bytes:
    mons = monitors() or [{"x": 0, "y": 0, "w": 1920, "h": 1080, "primary": True}]
    out = bytes([len(mons)])
    for i, m in enumerate(mons):
        out += struct.pack(">6i", i, 1 if m.get("primary") else 0,
                           m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"])
    return out
