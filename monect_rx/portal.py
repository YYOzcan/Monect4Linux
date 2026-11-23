"""xdg-desktop-portal ScreenCast client (Wayland screen capture).

Runs a GLib main loop in a private thread so it can be used from asyncio.
The restore token is persisted so KDE only asks for permission once.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("portal")

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.ScreenCast"
REQ = "org.freedesktop.portal.Request"


@dataclass
class Stream:
    fd: int
    node_id: int
    width: int
    height: int
    x: int
    y: int
    session: "ScreenCastSession"


class _Loop:
    """Single shared GLib loop thread."""
    _lock = threading.Lock()
    _started = False

    @classmethod
    def ensure(cls):
        with cls._lock:
            if cls._started:
                return
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib
            DBusGMainLoop(set_as_default=True)
            loop = GLib.MainLoop()
            threading.Thread(target=loop.run, name="glib", daemon=True).start()
            cls._started = True


class ScreenCastSession:
    def __init__(self, token_file: Path) -> None:
        _Loop.ensure()
        import dbus
        self.dbus = dbus
        self.bus = dbus.SessionBus()
        self.portal = self.bus.get_object(BUS, PATH)
        self.iface = dbus.Interface(self.portal, IFACE)
        self.token_file = token_file
        self.session_handle = None

    def _request(self, method, *args, options: dict, timeout=120.0) -> dict:
        dbus = self.dbus
        token = "monect" + secrets.token_hex(6)
        options = dict(options)
        options["handle_token"] = token
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        done = threading.Event()
        result: dict = {}

        def on_response(code, res):
            result["code"] = int(code)
            result["res"] = res
            done.set()

        match = self.bus.add_signal_receiver(on_response, "Response", REQ, BUS, req_path)
        try:
            getattr(self.iface, method)(*args, dbus.Dictionary(options, signature="sv"))
            if not done.wait(timeout):
                raise TimeoutError(f"portal {method} timed out")
        finally:
            match.remove()
        if result["code"] != 0:
            raise PermissionError(f"portal {method} denied/cancelled (code {result['code']})")
        return result["res"]

    def start(self, cursor_embedded: bool = True) -> Stream:
        return self.start_all(cursor_embedded)[0]

    def start_all(self, cursor_embedded: bool = True) -> list[Stream]:
        """All shared monitors (the user may pick several so the phone can switch)."""
        dbus = self.dbus
        res = self._request("CreateSession", options={
            "session_handle_token": "monect" + secrets.token_hex(6)})
        self.session_handle = str(res["session_handle"])
        opts = {
            "types": dbus.UInt32(1),                              # monitor
            "multiple": dbus.Boolean(True),
            "cursor_mode": dbus.UInt32(2 if cursor_embedded else 1),
            "persist_mode": dbus.UInt32(2),
        }
        try:
            tok = self.token_file.read_text().strip()
            if tok:
                opts["restore_token"] = dbus.String(tok)
        except OSError:
            pass
        self._request("SelectSources", dbus.ObjectPath(self.session_handle), options=opts)
        res = self._request("Start", dbus.ObjectPath(self.session_handle), "", options={})
        if "restore_token" in res:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(str(res["restore_token"]))
        fd_obj = self.iface.OpenPipeWireRemote(
            dbus.ObjectPath(self.session_handle), dbus.Dictionary({}, signature="sv"))
        fd = fd_obj.take()
        os.set_inheritable(fd, True)
        out = []
        for node_id, props in res["streams"]:
            size = props.get("size", (0, 0))
            pos = props.get("position", (0, 0))
            log.info("screencast node %s size %sx%s at %s,%s", node_id, size[0], size[1],
                     pos[0], pos[1])
            out.append(Stream(fd, int(node_id), int(size[0]), int(size[1]), int(pos[0]),
                              int(pos[1]), self))
        return out

    def close(self) -> None:
        if self.session_handle:
            try:
                obj = self.bus.get_object(BUS, self.session_handle)
                self.dbus.Interface(obj, "org.freedesktop.portal.Session").Close()
            except Exception:
                pass
            self.session_handle = None
