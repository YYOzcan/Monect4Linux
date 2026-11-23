"""Task manager (category 0x0B): system stats, process list, kill, details.

Replies: ``0x15 | kind | flags | json-chunk`` where flags bit0 = first chunk,
bit1 = last chunk; JSON is split in chunks of at most 0x3FFD bytes.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time

import psutil

log = logging.getLogger("taskmgr")

STATS, PROCESSES, KILL, DETAILS = 0, 1, 2, 3


def _send_json(send, kind: int, obj) -> None:
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    step = 0x3FFD
    chunks = [data[i:i + step] for i in range(0, len(data), step)] or [b""]
    for i, c in enumerate(chunks):
        flags = (1 if i == 0 else 0) | (2 if i == len(chunks) - 1 else 0)
        send(bytes([0x15, kind, flags]) + c)


def _gpu_percent() -> float:
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=2).stdout
            return float(out.split()[0])
        except Exception:
            pass
    return 0.0


class TaskManager:
    def __init__(self) -> None:
        self._net = psutil.net_io_counters()
        self._net_t = time.monotonic()
        psutil.cpu_percent(None)
        for p in psutil.process_iter():
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass

    def stats(self) -> dict:
        now = time.monotonic()
        net = psutil.net_io_counters()
        dt = max(now - self._net_t, 1e-3)
        recv = (net.bytes_recv - self._net.bytes_recv) / dt
        sent = (net.bytes_sent - self._net.bytes_sent) / dt
        self._net, self._net_t = net, now
        vm = psutil.virtual_memory()
        return {
            "cpu": psutil.cpu_percent(None),
            "cores": psutil.cpu_count(),
            "gpu": _gpu_percent(),
            "memTotal": vm.total,
            "memUsed": vm.total - vm.available,
            "diskPercent": psutil.disk_usage("/").percent,
            "netRecvBps": int(recv),
            "netSendBps": int(sent),
            "uptimeSec": int(time.time() - psutil.boot_time()),
        }

    @staticmethod
    def _proc(p: psutil.Process, ncpu: int) -> dict:
        with p.oneshot():
            name = p.name()
            try:
                desc = " ".join(p.cmdline())[:200] or name
            except psutil.Error:
                desc = name
            return {"pid": p.pid, "name": name, "desc": desc,
                    # client divides by core count itself (per-core percent, integer)
                    "cpu": int(round(p.cpu_percent(None))),
                    "mem": p.memory_info().rss}

    def processes(self) -> dict:
        ncpu = psutil.cpu_count() or 1
        out = []
        for p in psutil.process_iter():
            try:
                out.append(self._proc(p, ncpu))
            except psutil.Error:
                continue
        return {"cores": ncpu, "processes": out}

    def handle(self, b: bytes, send) -> None:
        sub = b[0]
        req = {}
        if len(b) > 1:
            try:
                req = json.loads(b[1:].decode("utf-8", "replace") or "{}")
            except ValueError:
                _send_json(send, sub, {"code": "bad_request", "message": "invalid json"})
                return
        if sub == STATS:
            _send_json(send, STATS, self.stats())
        elif sub == PROCESSES:
            _send_json(send, PROCESSES, self.processes())
        elif sub == KILL:
            pid = int(req.get("pid", -1))
            op = req.get("op", "kill")
            try:
                p = psutil.Process(pid)
                (p.terminate if op in ("terminate", "end", "close") else p.kill)()
                res = {"pid": pid, "op": op, "ok": True}
            except psutil.NoSuchProcess:
                res = {"pid": pid, "op": op, "ok": False, "error": "not_found"}
            except psutil.AccessDenied:
                res = {"pid": pid, "op": op, "ok": False, "error": "access_denied"}
            except Exception:
                res = {"pid": pid, "op": op, "ok": False, "error": "failed"}
            log.info("kill %s -> %s", pid, res)
            _send_json(send, KILL, res)
        elif sub == DETAILS:
            pid = int(req.get("pid", -1))
            try:
                p = psutil.Process(pid)
                info = self._proc(p, psutil.cpu_count() or 1)
                try:
                    info["path"] = p.exe()
                except psutil.Error:
                    info["path"] = ""
                _send_json(send, DETAILS, info)
            except psutil.Error:
                _send_json(send, DETAILS, {"pid": pid, "error": "not_found"})
        else:
            _send_json(send, sub, {"code": "bad_request", "message": "unknown sub command"})
