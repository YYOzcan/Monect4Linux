"""Helper / power actions (power widget): UDP or data channel ``0x0F value``."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("helper")

NAMES = {1: "this-pc", 2: "media-player", 3: "spreadsheet", 4: "reboot", 5: "power-off",
         6: "sleep", 7: "calculator", 8: "mic-mute", 9: "display-off", 10: "lock",
         11: "hibernate", 12: "shutdown", 13: "log-off"}


def _first(*cands: list[str]) -> list[str] | None:
    for c in cands:
        if shutil.which(c[0]):
            return c
    return None


def _cmd(action: int) -> list[str] | None:
    home = str(Path.home())
    return {
        1: _first(["dolphin", home], ["xdg-open", home]),
        2: _first(["elisa"], ["vlc"], ["haruna"], ["mpv", "--player-operation-mode=pseudo-gui"]),
        3: _first(["libreoffice", "--calc"], ["onlyoffice-desktopeditors"]),
        4: ["systemctl", "reboot"],
        5: ["systemctl", "poweroff"],
        6: ["systemctl", "suspend"],
        7: _first(["kcalc"], ["gnome-calculator"], ["galculator"], ["qalculate-qt"]),
        8: ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "toggle"],
        9: _first(["kscreen-doctor", "--dpms", "off"], ["xset", "dpms", "force", "off"]),
        10: ["loginctl", "lock-session"],
        11: ["systemctl", "hibernate"],
        12: ["systemctl", "poweroff"],
        13: _first(["qdbus6", "org.kde.Shutdown", "/Shutdown", "logout"],
                   ["qdbus", "org.kde.Shutdown", "/Shutdown", "logout"],
                   ["loginctl", "terminate-user", ""]),
    }.get(action)


def run(action: int) -> bool:
    cmd = _cmd(action)
    log.info("helper action %d (%s) -> %s", action, NAMES.get(action, "?"), cmd)
    if not cmd:
        return False
    import os
    if os.environ.get("MONECT_DRYRUN"):
        return True
    if cmd[:2] == ["loginctl", "terminate-user"]:
        import getpass
        cmd[2] = getpass.getuser()
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError:
        return False
