"""Local control socket for the UI ($XDG_RUNTIME_DIR/monect-linux.sock).

Line-delimited JSON. Requests from the UI:
  {"cmd": "status"}                       -> {"type": "status", ...}
  {"cmd": "get_settings"}                 -> {"type": "settings", "settings": {...}}
  {"cmd": "set_settings", "settings": {}} -> {"type": "settings", ...}  (persisted)
  {"cmd": "disconnect", "ip": "..."}
  {"cmd": "confirm", "id": n, "accept": bool}
Events pushed to every UI: {"type": "event", "event": "connected"|"disconnected"|"confirm", ...}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("control")

SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "monect-linux.sock"


class ControlServer:
    def __init__(self, server) -> None:
        self.server = server
        self.writers: set[asyncio.StreamWriter] = set()
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 1

    async def start(self) -> None:
        try:
            SOCK.unlink()
        except FileNotFoundError:
            pass
        await asyncio.start_unix_server(self._client, path=str(SOCK))
        os.chmod(SOCK, 0o600)
        log.info("control socket %s", SOCK)

    @property
    def has_ui(self) -> bool:
        return bool(self.writers)

    def broadcast(self, obj: dict) -> None:
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        for w in list(self.writers):
            try:
                w.write(line)
            except Exception:
                self.writers.discard(w)

    def event(self, name: str, **kw) -> None:
        self.broadcast({"type": "event", "event": name, **kw})

    async def ask_confirm(self, device: str, ip: str, timeout: float = 60) -> bool:
        """Ask the UI whether a device may connect. No UI -> deny."""
        if not self.has_ui:
            log.warning("confirmation required but no UI running; denying %s", device)
            return False
        cid = self.next_id
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        self.event("confirm", id=cid, device=device, ip=ip)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(cid, None)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                resp = await self._handle(req)
                if resp is not None:
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()

    async def _handle(self, req: dict):
        cmd = req.get("cmd")
        srv = self.server
        if cmd == "status":
            return {"type": "status", **srv.status()}
        if cmd == "get_settings":
            return {"type": "settings", "settings": srv.public_settings()}
        if cmd == "set_settings":
            srv.update_settings(req.get("settings") or {})
            return {"type": "settings", "settings": srv.public_settings()}
        if cmd == "disconnect":
            await srv.disconnect(req.get("ip", ""))
            return {"type": "status", **srv.status()}
        if cmd == "confirm":
            fut = self.pending.get(int(req.get("id", 0)))
            if fut and not fut.done():
                fut.set_result(bool(req.get("accept")))
            return None
        return {"type": "error", "message": f"unknown cmd {cmd}"}
