"""Blackboard (category 0x0C): phones draw on the PC screen, strokes are shared between phones.

phone -> PC   0C 00 launch | 0C 01 close | 0C 02 n ops… | 0C 03 lastSeq(BE i32)
PC -> phone   16 00 status [session id (8)]        status 0 ok, 1 failed
              16 01 sid(8) seq(i32) n ops…         strokes drawn by another phone
              16 02 seq(i32) chunk(u16) chunks(u16) n ops…   full redraw (chunk 0 clears)
ops (big endian): 00 id w(f32, × screen width) argb mode(bit0 eraser, bit1 clear) x y (f32 0..1)
                  01 id n (x y)×n | 02 id | 03 clear | 04 background argb
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import struct
import subprocess
import sys

log = logging.getLogger("blackboard")


class Overlay:
    """The Qt helper process (monect_rx.overlay), started on first use."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.sent: list[dict] = []            # test hook (MONECT_DRYRUN)

    def send(self, **cmd) -> None:
        if os.environ.get("MONECT_DRYRUN"):
            self.sent.append(cmd)
            return
        if self.proc is None or self.proc.poll() is not None:
            env = dict(os.environ)
            if env.get("DISPLAY") and shutil.which("Xwayland") or env.get("XDG_SESSION_TYPE") == "x11":
                env["QT_QPA_PLATFORM"] = "xcb"        # always-on-top + click-through work on X11
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
            try:
                self.proc = subprocess.Popen([sys.executable, "-m", "monect_rx.overlay"],
                                             stdin=subprocess.PIPE, env=env, text=True,
                                             stdout=subprocess.DEVNULL)
            except OSError as ex:
                log.warning("overlay unavailable: %s", ex)
                return
        try:
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.proc = None

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(2)
            except Exception:
                self.proc.kill()
        self.proc = None


def parse_ops(b: bytes, count: int) -> list[tuple]:
    """-> [(op, fields…, raw bytes)]; stops at the first malformed op."""
    out, o = [], 0
    for _ in range(count):
        if o >= len(b):
            break
        op = b[o]
        if op == 0 and o + 25 <= len(b):
            sid, w, color, mode, x, y = struct.unpack_from(">ifIiff", b, o + 1)
            out.append((0, sid, w, color, mode, x, y, b[o:o + 25]))
            o += 25
        elif op == 1 and o + 6 <= len(b):
            sid, n = struct.unpack_from(">iB", b, o + 1)
            end = o + 6 + 8 * n
            if end > len(b):
                break
            pts = list(struct.unpack_from(f">{2 * n}f", b, o + 6))
            out.append((1, sid, pts, b[o:end]))
            o = end
        elif op == 2 and o + 5 <= len(b):
            out.append((2, struct.unpack_from(">i", b, o + 1)[0], b[o:o + 5]))
            o += 5
        elif op == 3:
            out.append((3, b[o:o + 1]))
            o += 1
        elif op == 4 and o + 5 <= len(b):
            out.append((4, struct.unpack_from(">I", b, o + 1)[0], b[o:o + 5]))
            o += 5
        else:
            break
    return out


class Blackboard:
    def __init__(self, overlay: Overlay) -> None:
        self.overlay = overlay
        self.members: dict[int, tuple[bytes, object]] = {}     # id(session) -> (sid, channel)
        self.seq = 0
        self.strokes: list[dict] = []                            # history for late joiners
        self.open: dict[tuple[bytes, int], dict] = {}
        self.bg = 0

    def handle(self, sess, ch, b: bytes) -> None:
        sub = b[0]
        if sub == 0x00:
            sid = secrets.token_bytes(8)
            self.members[id(sess)] = (sid, ch)
            self.overlay.send(cmd="bb", on=True)
            log.info("blackboard opened by %s", getattr(getattr(sess, "client", None), "name", "?"))
            ch.send(b"\x16\x00\x00" + sid)
        elif sub == 0x01:
            self.leave(sess)
        elif sub == 0x03:
            self.resync(ch)
        elif sub == 0x02 and len(b) >= 2:
            m = self.members.get(id(sess))
            if m is None:
                return
            sid = m[0]
            ops = parse_ops(b[2:], b[1])
            if not ops:
                return
            first = self.seq + 1
            for op in ops:
                self.seq += 1
                self.apply(sid, op)
            raw = b"".join(op[-1] for op in ops)
            msg = b"\x16\x01" + sid + struct.pack(">iB", first, len(ops)) + raw
            for other, (osid, och) in list(self.members.items()):
                if other != id(sess):
                    try:
                        och.send(msg)
                    except Exception:
                        self.members.pop(other, None)

    def apply(self, sid: bytes, op: tuple) -> None:
        key = sid.hex()
        kind = op[0]
        if kind == 0:
            _, i, w, color, mode, x, y, _raw = op
            st = {"w": w, "color": color, "mode": mode, "pts": [x, y]}
            self.strokes.append(st)
            self.open[(sid, i)] = st
            self.overlay.send(cmd="begin", key=key, id=i, w=w, color=color, mode=mode, x=x, y=y)
        elif kind == 1:
            _, i, pts, _raw = op
            st = self.open.get((sid, i))
            if st is not None:
                st["pts"] += pts
            self.overlay.send(cmd="points", key=key, id=i, pts=pts)
        elif kind == 2:
            self.open.pop((sid, op[1]), None)
            self.overlay.send(cmd="end", key=key, id=op[1])
        elif kind == 3:
            self.strokes.clear()
            self.open.clear()
            self.overlay.send(cmd="clear")
        elif kind == 4:
            self.bg = op[1]
            self.overlay.send(cmd="bg", color=op[1])

    def history_ops(self) -> list[bytes]:
        ops = []
        for n, st in enumerate(self.strokes):
            p = st["pts"]
            ops.append(b"\x00" + struct.pack(">ifIiff", n, st["w"], st["color"], st["mode"],
                                              p[0], p[1]))
            rest = p[2:]
            for k in range(0, len(rest) - 1, 64):
                chunk = rest[k:k + 64]
                ops.append(b"\x01" + struct.pack(f">iB{len(chunk)}f", n, len(chunk) // 2, *chunk))
            ops.append(b"\x02" + struct.pack(">i", n))
        if self.bg:
            ops.append(b"\x04" + struct.pack(">I", self.bg))
        return ops

    def resync(self, ch) -> None:
        ops = self.history_ops()
        chunks, cur, size = [], [], 0
        for op in ops:
            if cur and (len(cur) >= 255 or size + len(op) > 14000):
                chunks.append(cur)
                cur, size = [], 0
            cur.append(op)
            size += len(op)
        chunks.append(cur)
        for i, c in enumerate(chunks):
            ch.send(b"\x16\x02" + struct.pack(">iHHB", self.seq, i, len(chunks), len(c))
                    + b"".join(c))

    def leave(self, sess) -> None:
        if self.members.pop(id(sess), None) is not None and not self.members:
            self.overlay.send(cmd="bb", on=False)
            log.info("blackboard closed")
