"""Game center (category 0x07): list installed games and launch them.

Request: 07 00 list | 07 01 <groupId UTF-16 BOM+NUL> <game UTF-16 BOM+NUL> launch
Reply  : 10 00 <seq-framed payload>  payload = BE u32 count, then per group:
         01, BE len, id(UTF-16LE), BE len, title(UTF-16LE), BE u32 n, (BE len, game)*
         10 02 status (0 = launched)
"""
from __future__ import annotations

import configparser
import glob
import logging
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

from .packetizer import Packetizer

log = logging.getLogger("games")

SKIP_DESKTOP = {"steam.desktop", "net.lutris.Lutris.desktop", "com.vysp3r.ProtonPlus.desktop",
                "io.github.benjamimgois.goverlay.desktop"}


def _steam_libraries() -> list[Path]:
    roots = [Path.home() / ".local/share/Steam", Path.home() / ".steam/steam"]
    libs: list[Path] = []
    for r in roots:
        vdf = r / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                libs.append(Path(m.group(1)) / "steamapps")
        if (r / "steamapps").exists():
            libs.append(r / "steamapps")
    out, seen = [], set()
    for p in libs:
        rp = p.resolve()
        if rp not in seen and rp.exists():
            seen.add(rp)
            out.append(rp)
    return out


def steam_games() -> dict[str, str]:
    """title -> appid (skips Proton/runtimes/redistributables)."""
    games: dict[str, str] = {}
    for lib in _steam_libraries():
        for acf in lib.glob("appmanifest_*.acf"):
            txt = acf.read_text(errors="replace")
            appid = re.search(r'"appid"\s+"(\d+)"', txt)
            name = re.search(r'"name"\s+"([^"]+)"', txt)
            if not (appid and name):
                continue
            n = name.group(1)
            if re.search(r"Proton|Steam Linux Runtime|Steamworks|Redistributable", n, re.I):
                continue
            games[n] = appid.group(1)
    return dict(sorted(games.items()))


def desktop_games() -> dict[str, str]:
    """title -> .desktop path for entries in the Game category."""
    games: dict[str, str] = {}
    dirs = ["/usr/share/applications", str(Path.home() / ".local/share/applications")]
    for d in dirs:
        for f in glob.glob(os.path.join(d, "*.desktop")):
            if os.path.basename(f) in SKIP_DESKTOP:
                continue
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if "Game" not in e.get("Categories", "") or e.get("NoDisplay", "").lower() == "true":
                continue
            games[e.get("Name[tr]", e.get("Name", os.path.basename(f)))] = f
    return dict(sorted(games.items()))


def _ws(s: str) -> bytes:
    b = s.encode("utf-16-le")
    return struct.pack(">I", len(b)) + b


def _read_wstr(b: bytes, off: int) -> tuple[str, int]:
    if b[off:off + 2] in (b"\xff\xfe", b"\xfe\xff"):
        off += 2
    end = off
    while end + 1 < len(b) and (b[end] or b[end + 1]):
        end += 2
    return b[off:end].decode("utf-16-le", "replace"), end + 2


class GameCenter:
    def __init__(self) -> None:
        self.steam: dict[str, str] = {}
        self.apps: dict[str, str] = {}

    def handle(self, b: bytes, send) -> None:
        sub = b[0]
        if sub == 0x00:
            self.steam, self.apps = steam_games(), desktop_games()
            groups = [("steam", "Steam", list(self.steam)), ("apps", "Oyunlar", list(self.apps))]
            groups = [g for g in groups if g[2]]
            payload = struct.pack(">I", len(groups))
            for gid, title, names in groups:
                payload += b"\x01" + _ws(gid) + _ws(title) + struct.pack(">I", len(names))
                payload += b"".join(_ws(n) for n in names)
            Packetizer(0x00, lambda m: send(b"\x10" + m)).push(payload)
            log.info("game list sent: %s", {g[1]: len(g[2]) for g in groups})
        elif sub == 0x01:
            gid, nxt = _read_wstr(b, 1)
            game, _ = _read_wstr(b, nxt)
            ok = self.launch(gid, game)
            send(bytes([0x10, 0x02, 0 if ok else 1]))
        else:
            log.info("game center sub=0x%02x %s", sub, b[:32].hex(" "))

    def launch(self, gid: str, game: str) -> bool:
        log.info("launch %s / %s", gid, game)
        try:
            if gid == "steam" and game in self.steam:
                cmd = ["steam", f"steam://rungameid/{self.steam[game]}"]
            elif gid == "apps" and game in self.apps:
                desk = self.apps[game]
                if shutil.which("kioclient"):
                    cmd = ["kioclient", "exec", desk]
                else:
                    cmd = ["gtk-launch", os.path.basename(desk)]
            else:
                return False
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        except OSError:
            return False
