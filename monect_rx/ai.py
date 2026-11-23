"""Monect AI on Linux: the phone's AI chat, answered by Claude with tools that control the PC.

phone -> PC   09 00 flags [frag(BE u16) if flags & 4] JSON
              flags: 1 first fragment, 2 last fragment, 4 fragment index present
PC -> phone   12 stream flags [frag] JSON        (same flags)
              stream 0 final answer   {requestId, ok, assistantText, cancelled?, status?}
              stream 1 live text      {requestId, delta, thinkingDelta, done, error, toolRunning}
              stream 4 confirmation   {confirmationId, toolName, summary, args, timeoutMs}
              stream 5 status/event   {status: {...}, running} | {event: "launch_start"}
actions: status, launch, chat, cancel, bargeIn, sessionClear, toolConfirmationResponse,
         setActiveMode, activeModeSubscribe, stop

The model is Claude through the Anthropic API. Credentials: the API key saved in the tray app
(settings "ai_api_key"), else ANTHROPIC_API_KEY / an `ant auth login` profile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

log = logging.getLogger("ai")

MODEL = "claude-opus-5-5"
AI_PROTOCOL = 3
CHUNK = 16000

SYSTEM = """You are Monect AI, an assistant the user talks to from the PC Remote app on their \
phone. You run on their Linux computer ({host}, {desktop}) and can act on it with tools: \
open websites/files, start applications, control media and volume, type text, read system \
information and, with the user's approval on the phone, run shell commands.
Answer in the language the user writes in. Keep replies short and conversational - they are \
read on a phone screen (and may be read aloud), so avoid long lists, tables and markdown \
headings. When the user asks you to do something on the PC, do it with a tool instead of \
explaining how. Never claim an action happened unless the tool result says it did."""


def _tool(name, desc, props, required):
    return {"name": name, "description": desc, "eager_input_streaming": True,
            "input_schema": {"type": "object", "properties": props, "required": required,
                             "additionalProperties": False}}


TOOLS = [
    _tool("open_target", "Open a web URL, a file or a folder on the PC with its default app.",
          {"target": {"type": "string", "description": "URL (https://…) or absolute path"}},
          ["target"]),
    _tool("launch_app", "Start an installed desktop application by (part of) its name, "
          "e.g. 'Firefox', 'Steam', 'Dolphin'.",
          {"name": {"type": "string"}}, ["name"]),
    _tool("media_control", "Press a media key on the PC.",
          {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop",
                                                 "volume_up", "volume_down", "mute"]}},
          ["action"]),
    _tool("set_volume", "Set the PC's output volume.",
          {"percent": {"type": "integer", "minimum": 0, "maximum": 150}}, ["percent"]),
    _tool("type_text", "Type text into the focused window on the PC (keyboard input).",
          {"text": {"type": "string"}}, ["text"]),
    _tool("system_info", "CPU, memory, battery, uptime, time and the busiest processes.",
          {}, []),
    _tool("run_command", "Run a shell command on the PC (bash). The user must approve it on "
          "the phone first. Prefer the other tools when they fit.",
          {"command": {"type": "string"},
           "summary": {"type": "string", "description": "one sentence for the approval prompt"}},
          ["command", "summary"]),
]


def _desktop() -> str:
    return os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "Linux"


class _Conv:
    def __init__(self) -> None:
        self.history: list[dict] = []          # server-side history (serverHistory mode)


class AIAgent:
    def __init__(self, settings: dict, hub=None, client_factory=None) -> None:
        self.settings = settings
        self.hub = hub
        self.frags: dict[int, bytearray] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.confirm: dict[str, asyncio.Future] = {}
        self.convs: dict[str, _Conv] = {}
        self.client_factory = client_factory or self._client
        self._cli = None
        self._cli_key = None

    # ---- framing ----------------------------------------------------------------
    def handle(self, sess, ch, body: bytes) -> None:
        if len(body) < 2 or body[0] != 0x00:
            return
        flags = body[1]
        o = 4 if flags & 4 else 2
        key = id(ch)
        if flags & 1:
            self.frags[key] = bytearray()
        buf = self.frags.setdefault(key, bytearray())
        buf += body[o:]
        if not flags & 2:
            return
        data = bytes(self.frags.pop(key, b""))
        try:
            msg = json.loads(data.decode("utf-8"))
        except ValueError:
            log.warning("bad AI message (%d bytes)", len(data))
            return
        self.on_message(ch, msg)

    @staticmethod
    def send(ch, stream: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode()
        parts = [raw[i:i + CHUNK] for i in range(0, len(raw), CHUNK)] or [b""]
        for i, part in enumerate(parts):
            flags = (1 if i == 0 else 0) | (2 if i == len(parts) - 1 else 0) | 4
            try:
                ch.send(bytes([0x12, stream, flags]) + i.to_bytes(2, "big") + part)
            except Exception:
                return

    def status(self) -> dict:
        return {"installed": True, "running": True, "aiProtocol": AI_PROTOCOL,
                "activeMode": False, "imageEditing": {"available": False},
                "host": os.uname().nodename, "configured": self.configured()}

    def configured(self) -> bool:
        return bool(self.settings.get("ai_api_key") or os.environ.get("ANTHROPIC_API_KEY")
                    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                    or (Path.home() / ".config/anthropic").exists())

    # ---- actions ----------------------------------------------------------------
    def on_message(self, ch, m: dict) -> None:
        action = m.get("action")
        rid = m.get("requestId") or str(uuid.uuid4())
        log.info("AI action %s", action)
        if action in ("status", "launch", "setActiveMode", "activeModeSubscribe"):
            if action == "launch":
                self.send(ch, 5, {"event": "launch_start", "requestId": rid})
            self.send(ch, 5, {"requestId": rid, "running": True, "status": self.status()})
            self.send(ch, 0, {"requestId": rid, "ok": True, "status": self.status()})
        elif action == "chat":
            t = asyncio.ensure_future(self.chat(ch, rid, m))
            self.tasks[rid] = t
            t.add_done_callback(lambda _t: self.tasks.pop(rid, None))
        elif action in ("cancel", "bargeIn", "stop"):
            target = m.get("targetRequestId") or m.get("cancelRequestId")
            for r, t in list(self.tasks.items()):
                if target in (None, "", r):
                    t.cancel()
            self.send(ch, 0, {"requestId": rid, "ok": True})
        elif action == "sessionClear":
            self.convs.pop(m.get("sessionId", ""), None)
            self.send(ch, 0, {"requestId": rid, "ok": True, "status": self.status()})
        elif action == "toolConfirmationResponse":
            fut = self.confirm.pop(m.get("confirmationId", ""), None)
            if fut and not fut.done():
                fut.set_result(bool(m.get("approved")))
        else:
            self.send(ch, 0, {"requestId": rid, "ok": True})

    # ---- conversation -------------------------------------------------------------
    @staticmethod
    def _content(c) -> list[dict] | str:
        if isinstance(c, str):
            return c
        out = []
        for part in c or []:
            if part.get("type") == "text" and part.get("text"):
                out.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:") and ";base64," in url:
                    mime, data = url[5:].split(";base64,", 1)
                    out.append({"type": "image", "source": {"type": "base64",
                                                            "media_type": mime, "data": data}})
                elif url.startswith("http"):
                    out.append({"type": "image", "source": {"type": "url", "url": url}})
        return out or ""

    def _messages(self, m: dict) -> list[dict]:
        msgs = []
        for x in m.get("messages") or []:
            role = x.get("role")
            if role not in ("user", "assistant"):
                continue
            content = self._content(x.get("content"))
            if not content:
                continue
            if msgs and msgs[-1]["role"] == role:        # keep roles alternating
                prev = msgs[-1]["content"]
                if isinstance(prev, str):
                    prev = [{"type": "text", "text": prev}]
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                msgs[-1]["content"] = prev + content
            else:
                msgs.append({"role": role, "content": content})
        text = m.get("message") or ""
        if not msgs or msgs[-1]["role"] != "user":
            msgs.append({"role": "user", "content": text or "…"})
        conv = self.convs.setdefault(m.get("sessionId", ""), _Conv())
        if m.get("serverHistory") and conv.history:
            msgs = conv.history + [msgs[-1]]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    def _client(self):
        import anthropic
        key = self.settings.get("ai_api_key") or None
        if self._cli is None or key != self._cli_key:
            self._cli = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
            self._cli_key = key
        return self._cli

    async def chat(self, ch, rid: str, m: dict) -> None:
        full = ""

        def delta(text="", thinking="", **extra):
            obj = {"requestId": rid, "delta": text, "thinkingDelta": thinking}
            obj.update(extra)
            self.send(ch, 1, obj)

        try:
            if not self.configured():
                full = ("Monect AI needs a Claude API key on this PC. Open the Monect4Linux "
                        "tray app → AI tab, paste your key from console.anthropic.com, then "
                        "ask again.")
                delta(full)
            else:
                full = await self._run(ch, rid, m, delta)
            delta(done=True)
            self.send(ch, 0, {"requestId": rid, "ok": True, "assistantText": full,
                              "status": self.status()})
        except asyncio.CancelledError:
            self.send(ch, 1, {"requestId": rid, "delta": "", "done": True})
            self.send(ch, 0, {"requestId": rid, "ok": True, "cancelled": True,
                              "assistantText": full})
        except Exception as ex:
            err = self._error_text(ex)
            log.warning("AI request failed: %s", err)
            self.send(ch, 1, {"requestId": rid, "delta": "", "error": err, "done": True})
            self.send(ch, 0, {"requestId": rid, "ok": False, "error": err})

    @staticmethod
    def _error_text(ex: Exception) -> str:
        try:
            import anthropic
            if isinstance(ex, anthropic.AuthenticationError):
                return "The Claude API key was rejected - check it in the Monect4Linux tray app."
            if isinstance(ex, anthropic.RateLimitError):
                return "Claude is rate limited right now, try again in a moment."
            if isinstance(ex, anthropic.APIConnectionError):
                return "Cannot reach the Claude API (network)."
            if isinstance(ex, anthropic.APIStatusError):
                return f"Claude API error {ex.status_code}."
        except ImportError:
            return "The 'anthropic' Python package is not installed (run monect4linux-setup --update)."
        return str(ex) or type(ex).__name__

    async def _run(self, ch, rid: str, m: dict, delta) -> str:
        client = self.client_factory()
        messages = self._messages(m)
        effort = "low" if m.get("responseLength") == "short" else "medium"
        thinking = {"type": "adaptive",
                    "display": "summarized" if m.get("thinkingEnabled") else "omitted"}
        system = SYSTEM.format(host=os.uname().nodename, desktop=_desktop())
        answer = ""
        for _turn in range(12):
            async with client.beta.messages.stream(
                    model=MODEL, max_tokens=16000, system=system, messages=messages,
                    tools=TOOLS, thinking=thinking, output_config={"effort": effort},
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default") as stream:
                async for ev in stream:
                    if ev.type == "text":
                        answer += ev.text
                        delta(ev.text)
                    elif ev.type == "thinking" and ev.thinking:
                        delta(thinking=ev.thinking)
                resp = await stream.get_final_message()
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                if not answer:
                    answer = "Sorry, I can't help with that."
                    delta(answer)
                break
            if resp.stop_reason == "pause_turn":
                continue
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                break
            if resp.stop_reason == "max_tokens":
                break
            results = []
            for u in uses:
                delta(toolRunning=u.name)
                ok, out = await self.run_tool(ch, u.name, u.input)
                results.append({"type": "tool_result", "tool_use_id": u.id,
                                "content": out[:20000], "is_error": not ok})
            delta(toolRunning="")
            messages.append({"role": "user", "content": results})
            if answer and not answer.endswith((" ", "\n")):
                answer += "\n"
                delta("\n")
        conv = self.convs.setdefault(m.get("sessionId", ""), _Conv())
        conv.history = [x for x in messages if isinstance(x.get("content"), str)][-16:]
        conv.history.append({"role": "assistant", "content": answer or "…"})
        return answer

    # ---- tools -------------------------------------------------------------------
    async def ask(self, ch, tool: str, summary: str, args: dict, timeout: float = 60.0) -> bool:
        cid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.confirm[cid] = fut
        self.send(ch, 4, {"confirmationId": cid, "toolName": tool, "summary": summary,
                          "args": args, "timeoutMs": int(timeout * 1000)})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self.confirm.pop(cid, None)

    async def run_tool(self, ch, name: str, args) -> tuple[bool, str]:
        if not isinstance(args, dict):
            return False, "INVALID_JSON"
        str_arg = lambda k: args.get(k) if isinstance(args.get(k), str) else None
        loop = asyncio.get_running_loop()
        dry = bool(os.environ.get("MONECT_DRYRUN"))
        try:
            if name == "open_target":
                t = str_arg("target")
                if not t:
                    return False, "target missing"
                if dry:
                    return True, f"(dry run) opened {t}"
                subprocess.Popen(["xdg-open", t], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return True, f"opened {t}"
            if name == "launch_app":
                q = str_arg("name")
                if not q:
                    return False, "name missing"
                return await loop.run_in_executor(None, launch_app, q, dry)
            if name == "media_control":
                keys = {"play_pause": "KEY_PLAYPAUSE", "next": "KEY_NEXTSONG",
                        "previous": "KEY_PREVIOUSSONG", "stop": "KEY_STOPCD",
                        "volume_up": "KEY_VOLUMEUP", "volume_down": "KEY_VOLUMEDOWN",
                        "mute": "KEY_MUTE"}
                k = keys.get(str_arg("action") or "")
                if not k:
                    return False, "unknown action"
                if self.hub is not None:
                    from evdev import ecodes
                    self.hub.kbd.tap(getattr(ecodes, k))
                return True, f"pressed {k}"
            if name == "set_volume":
                p = args.get("percent")
                if not isinstance(p, int) or not 0 <= p <= 150:
                    return False, "percent must be 0..150"
                if not dry:
                    subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{p}%"],
                                   timeout=5, check=False)
                return True, f"volume set to {p}%"
            if name == "type_text":
                t = str_arg("text")
                if t is None:
                    return False, "text missing"
                if self.hub is not None:
                    await loop.run_in_executor(None, self.hub.paste_text, t)
                return True, f"typed {len(t)} characters"
            if name == "system_info":
                return True, await loop.run_in_executor(None, system_info)
            if name == "run_command":
                cmd = str_arg("command")
                if not cmd:
                    return False, "command missing"
                summary = str_arg("summary") or cmd
                if not await self.ask(ch, "run_command", summary, {"command": cmd}):
                    return False, "The user declined (or did not answer) on the phone."
                if dry:
                    return True, f"(dry run) would run: {cmd}"
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), 60)
                except asyncio.TimeoutError:
                    proc.kill()
                    return False, "timed out after 60 s"
                text = out.decode("utf-8", "replace")[-8000:]
                return proc.returncode == 0, f"exit {proc.returncode}\n{text}"
            return False, f"unknown tool {name}"
        except Exception as ex:
            return False, f"{type(ex).__name__}: {ex}"


def launch_app(query: str, dry: bool = False) -> tuple[bool, str]:
    q = query.lower().strip()
    dirs = [Path.home() / ".local/share/applications"] + [
        Path(d) / "applications" for d in
        os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")]
    dirs.append(Path("/var/lib/flatpak/exports/share/applications"))
    best = None
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.desktop"):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if "NoDisplay=true" in text:
                continue
            name = next((ln[5:] for ln in text.splitlines() if ln.startswith("Name=")), "")
            score = 3 if name.lower() == q else 2 if name.lower().startswith(q) else \
                1 if q in name.lower() or q in f.stem.lower() else 0
            if score and (best is None or score > best[0]):
                best = (score, f, name)
    if best is None:
        return False, f"no application matching {query!r}"
    _, f, name = best
    if not dry:
        cmd = (["gtk-launch", f.stem] if shutil.which("gtk-launch") else
               ["kioclient", "exec", str(f)] if shutil.which("kioclient") else
               ["gio", "launch", str(f)])
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    return True, f"started {name}"


def system_info() -> str:
    import psutil
    vm = psutil.virtual_memory()
    parts = [f"time {time.strftime('%Y-%m-%d %H:%M')}",
             f"CPU {psutil.cpu_percent(interval=0.3):.0f}% ({psutil.cpu_count()} threads)",
             f"RAM {vm.used / 2**30:.1f}/{vm.total / 2**30:.1f} GiB",
             f"uptime {int((time.time() - psutil.boot_time()) // 3600)} h"]
    bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if bat:
        parts.append(f"battery {bat.percent:.0f}%{' charging' if bat.power_plugged else ''}")
    procs = sorted(psutil.process_iter(["name", "memory_info"]),
                   key=lambda p: (p.info["memory_info"].rss if p.info["memory_info"] else 0),
                   reverse=True)[:5]
    parts.append("top memory: " + ", ".join(
        f"{p.info['name']} {p.info['memory_info'].rss / 2**20:.0f} MiB" for p in procs
        if p.info["memory_info"]))
    return "; ".join(parts)
