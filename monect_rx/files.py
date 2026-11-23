"""File explorer (category 0x06): browse, download, upload, delete, rename, open.

The app speaks Windows paths, so the Linux tree is exposed as drive ``L:``
(``L:\\home\\user`` <-> ``/home/user``).  All replies start with ``0x0B``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import stat
import struct
import subprocess
from pathlib import Path

log = logging.getLogger("files")

DRIVE = "L:"
ENTRY = struct.Struct(">7I520s")          # 548 bytes
ATTR_DIR, ATTR_HIDDEN, ATTR_READONLY, ATTR_NORMAL = 0x10, 0x02, 0x01, 0x80
CHUNK = 0x3FFC


def xdg_dir(name: str, fallback: str) -> str:
    try:
        out = subprocess.run(["xdg-user-dir", name], capture_output=True, text=True,
                             timeout=2).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return str(Path.home() / fallback)


# ids used by the app: AppData, Desktop, Pictures, Documents, Downloads, Music, Videos
SPECIAL = [
    lambda: os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")),
    lambda: xdg_dir("DESKTOP", "Desktop"),
    lambda: xdg_dir("PICTURES", "Pictures"),
    lambda: xdg_dir("DOCUMENTS", "Documents"),
    lambda: xdg_dir("DOWNLOAD", "Downloads"),
    lambda: xdg_dir("MUSIC", "Music"),
    lambda: xdg_dir("VIDEOS", "Videos"),
]


def to_linux(p: str) -> str:
    p = p.rstrip("\x00")
    if len(p) >= 2 and p[1] == ":":
        p = p[2:]
    p = p.replace("\\", "/") or "/"
    return os.path.normpath(p) if p != "/" else "/"


def to_win(p: str) -> str:
    return DRIVE + p.replace("/", "\\")


def wstr(b: bytes, off: int) -> tuple[str, int]:
    """Read a NUL-terminated UTF-16LE string; return (text, offset after NUL)."""
    end = off
    while end + 1 < len(b) and (b[end] or b[end + 1]):
        end += 2
    return b[off:end].decode("utf-16-le", "replace"), end + 2


def u16le(s: str) -> bytes:
    return s.encode("utf-16-le")


def entry(name: str, st: os.stat_result | None, etype: int = 0, attrs: int | None = None,
          size: int | None = None) -> bytes:
    if st is not None and attrs is None:
        attrs = ATTR_DIR if stat.S_ISDIR(st.st_mode) else ATTR_NORMAL
        if name.startswith("."):
            attrs |= ATTR_HIDDEN
    size = size if size is not None else (st.st_size if st and not stat.S_ISDIR(st.st_mode) else 0)
    t = (lambda v: int(v) & 0xFFFFFFFF)
    return ENTRY.pack(etype, attrs or 0, t(st.st_ctime) if st else 0, t(st.st_atime) if st else 0,
                      t(st.st_mtime) if st else 0, size >> 32, size & 0xFFFFFFFF,
                      u16le(name)[:518])


class FileExplorer:
    def __init__(self) -> None:
        self.next_id = 1
        self.downloads: dict[int, dict] = {}
        self.uploads: dict[int, dict] = {}

    # ---- helpers ------------------------------------------------------------
    def _alloc(self) -> int:
        i = self.next_id
        self.next_id += 1
        return i

    def root_entries(self) -> list[bytes]:
        out = []
        for path, label in [("/", "L:\\")] + [(Path.home().as_posix(), "Home")]:
            try:
                vfs = os.statvfs(path)
                total = vfs.f_blocks * vfs.f_frsize
                free = vfs.f_bavail * vfs.f_frsize
            except OSError:
                total = free = 0
            if label == "L:\\":
                # drive: type 1, attrs = DRIVE_FIXED (3); size = total, ctime slot = free GiB
                out.append(ENTRY.pack(1, 3, free >> 30, total >> 30, 0, total >> 32,
                                      total & 0xFFFFFFFF, u16le(label)))
        for i in (1, 3, 4, 2, 5, 6):
            p = SPECIAL[i]()
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append(entry(to_win(p), st))
        return out

    # ---- dispatcher -----------------------------------------------------------
    async def handle(self, b: bytes, send) -> None:
        """b = message without the category byte (b[0] = sub-command)."""
        sub = b[0]
        if sub == 0x00:                                     # list directory
            path, _ = wstr(b, 3)
            await self.list_dir(path, send)
        elif sub == 0x01:                                   # download to phone
            path, _ = wstr(b, 3)
            self._start_download(path, 0, self._alloc(), send)
        elif sub == 0x06 and len(b) >= 0x13:                # resume download
            tid = struct.unpack(">Q", b[1:9])[0]
            offset = struct.unpack(">Q", b[9:17])[0]
            path, _ = wstr(b, 0x13)
            self._start_download(path, offset, tid, send)
        elif sub == 0x04 and len(b) >= 9:                   # cancel download
            tid = struct.unpack(">Q", b[1:9])[0]
            d = self.downloads.get(tid)
            if d:
                d["cancel"] = True
        elif sub == 0x02:                                   # delete
            path, _ = wstr(b, 3)
            ok = self._delete(to_linux(path))
            send(b"\x0b\x03" + bytes([ok]) + u16le(path))
        elif sub == 0x05:                                   # rename / move
            src, nxt = wstr(b, 3)
            dst, _ = wstr(b, nxt + 2) if nxt + 2 < len(b) else ("", 0)
            ok = self._rename(to_linux(src), to_linux(dst) if ":" in dst or dst.startswith("\\")
                              else os.path.join(os.path.dirname(to_linux(src)), dst))
            send(b"\x0b\x02" + bytes([ok]) + u16le(src))
        elif sub == 0x03:                                   # open on PC
            path, _ = wstr(b, 3)
            subprocess.Popen(["xdg-open", to_linux(path)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        elif sub == 0x07 and len(b) >= 3:                   # special folder paths
            n = min(struct.unpack(">H", b[1:3])[0], 7)
            body = b""
            cnt = 0
            for fid in b[3:3 + n]:
                if fid < len(SPECIAL):
                    p = u16le(to_win(SPECIAL[fid]()))
                    body += bytes([fid]) + struct.pack(">I", len(p)) + p
                    cnt += 1
            send(b"\x0b\x06" + bytes([cnt]) + body)
        elif sub == 0x08:
            await self.upload(b, send)
        elif sub == 0x0B:                                   # display name for a path
            path, _ = wstr(b, 3)
            lp = to_linux(path)
            if path.rstrip("\\") in (DRIVE, ""):
                name = "Linux (/)"
            else:
                name = os.path.basename(lp.rstrip("/")) or lp
            wp, wn = u16le(path), u16le(name)
            send(b"\x0b\x01" + struct.pack(">I", len(wp)) + wp + struct.pack(">I", len(wn)) + wn)
        else:
            log.debug("file op 0x%02x %s", sub, b[:48].hex(" "))

    async def list_dir(self, path: str, send) -> None:
        if path in ("\\", "", "/"):
            items = self.root_entries()
        else:
            lp = to_linux(path)
            items = []
            try:
                with os.scandir(lp) as it:
                    for de in it:
                        try:
                            items.append(entry(de.name, de.stat(follow_symlinks=True)))
                        except OSError:
                            items.append(entry(de.name, None, attrs=ATTR_NORMAL))
            except OSError as ex:
                log.info("cannot list %s: %s", lp, ex)
        head = b"\x0b\x00" + struct.pack(">I", len(items))
        per = (0x4000 - 6) // ENTRY.size
        if not items:
            send(head)
        for i in range(0, len(items), per):
            send(head + b"".join(items[i:i + per]))

    @staticmethod
    def _delete(p: str) -> bool:
        try:
            if shutil.which("gio"):
                return subprocess.run(["gio", "trash", p], timeout=10).returncode == 0
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            return True
        except Exception:
            return False

    @staticmethod
    def _rename(a: str, b: str) -> bool:
        try:
            os.rename(a, b)
            return True
        except OSError:
            return False

    # ---- PC -> phone -------------------------------------------------------------
    def _start_download(self, wpath: str, offset: int, tid: int, send) -> None:
        d = {"cancel": False}
        self.downloads[tid] = d
        asyncio.ensure_future(self._download(wpath, offset, tid, send, d))

    async def _download(self, wpath, offset, tid, send, d) -> None:
        name = u16le(wpath)
        lp = to_linux(wpath)
        try:
            f = open(lp, "rb")
        except OSError:
            send(b"\x0b\x04\x03" + struct.pack(">QI", tid, len(name)) + name)
            return
        loop = asyncio.get_running_loop()
        try:
            size = os.fstat(f.fileno()).st_size
            f.seek(offset)
            head = (b"\x0b\x04" + bytes([2 if offset else 0]) + struct.pack(">Q", tid)
                    + bytes(16) + struct.pack(">QI", size, len(name)) + name)
            first = True
            while True:
                room = CHUNK - (len(head) if first else 11) - 4
                data = await loop.run_in_executor(None, f.read, room)
                if d["cancel"]:
                    send(b"\x0b\x05" + struct.pack(">Q", tid))
                    return
                if not data and not first:
                    break
                pre = head if first else b"\x0b\x04\x01" + struct.pack(">Q", tid)
                send(pre + struct.pack(">I", len(data)) + data)
                first = False
                if not data:
                    break
                await asyncio.sleep(0)
            log.info("sent %s (%d bytes, id %d)", lp, size, tid)
        finally:
            f.close()
            self.downloads.pop(tid, None)

    # ---- phone -> PC -------------------------------------------------------------
    async def upload(self, b: bytes, send) -> None:
        phase = b[1] if len(b) > 1 else 0xFF
        if phase == 0 and len(b) >= 0x1F:
            md5 = b[2:18]
            size = struct.unpack(">Q", b[0x13:0x1B])[0]
            name, nxt = wstr(b, 0x1D)
            folder, _ = wstr(b, nxt + 2) if nxt + 2 < len(b) else ("", 0)
            dest_dir = to_linux(folder) if folder else SPECIAL[4]()
            name = os.path.basename(name.replace("\\", "/")) or "upload.bin"
            target = os.path.join(dest_dir, name)
            stem, ext = os.path.splitext(target)
            n = 1
            while os.path.exists(target):
                target = f"{stem}({n}){ext}"
                n += 1
            tid = self._alloc()
            try:
                os.makedirs(dest_dir, exist_ok=True)
                fh = open(target, "wb")
            except OSError as ex:
                log.warning("upload open failed: %s", ex)
                send(b"\x0b\x07\x02" + struct.pack(">Q", tid))
                return
            self.uploads[tid] = {"fh": fh, "size": size, "done": 0, "md5": md5, "path": target}
            wname, wdir = u16le(name), u16le(to_win(dest_dir))
            send(b"\x0b\x07\x00" + struct.pack(">QI", tid, len(wname)) + wname
                 + struct.pack(">I", len(wdir)) + wdir)
            log.info("upload %s (%d bytes) id %d", target, size, tid)
            if size == 0:
                await self._finish_upload(tid, send)
        elif phase == 1 and len(b) >= 14:
            tid = struct.unpack(">Q", b[2:10])[0]
            ln = struct.unpack(">I", b[10:14])[0]
            u = self.uploads.get(tid)
            if not u:
                return
            u["fh"].write(b[14:14 + ln])
            u["done"] += ln
            if u["done"] >= u["size"]:
                await self._finish_upload(tid, send)
        elif phase == 2 and len(b) >= 10:
            tid = struct.unpack(">Q", b[2:10])[0]
            u = self.uploads.pop(tid, None)
            if u:
                u["fh"].close()
                try:
                    os.remove(u["path"])
                except OSError:
                    pass
        else:
            log.info("upload phase %s %s", phase, b[:48].hex(" "))

    async def _finish_upload(self, tid: int, send) -> None:
        u = self.uploads.pop(tid)
        u["fh"].close()
        ok = True
        if any(u["md5"]):
            h = hashlib.md5()
            with open(u["path"], "rb") as f:
                for blk in iter(lambda: f.read(1 << 20), b""):
                    h.update(blk)
            ok = h.digest() == u["md5"]
        send(b"\x0b\x07" + bytes([1 if ok else 2]) + struct.pack(">Q", tid))
        log.info("upload %s finished (%s)", u["path"], "ok" if ok else "md5 mismatch")
