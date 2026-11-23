"""Virtual input devices backed by /dev/uinput."""
from __future__ import annotations

import logging
import shutil
import struct
import subprocess
import threading

from evdev import AbsInfo, UInput, ecodes as e

from .keymaps import (ALL_KEYS, CONSUMER_BITS, HID_TO_KEY, MODIFIER_KEYS,
                      SET1_E0_TO_KEY, SET1_TO_KEY)

log = logging.getLogger("input")


class Mouse:
    """Relative pointer fed with HID boot-protocol mouse reports."""

    BUTTONS = [(0x01, e.BTN_LEFT), (0x02, e.BTN_RIGHT), (0x04, e.BTN_MIDDLE)]

    def __init__(self) -> None:
        caps = {
            e.EV_KEY: [b for _, b in self.BUTTONS],
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_WHEEL_HI_RES],
        }
        self.dev = UInput(caps, name="Monect Virtual Mouse")
        self.buttons = 0
        self.wheel_acc = 0

    def report(self, buttons: int, dx: int, dy: int, wheel: int) -> None:
        changed = buttons ^ self.buttons
        for bit, btn in self.BUTTONS:
            if changed & bit:
                self.dev.write(e.EV_KEY, btn, 1 if buttons & bit else 0)
        self.buttons = buttons
        if dx:
            self.dev.write(e.EV_REL, e.REL_X, dx)
        if dy:
            self.dev.write(e.EV_REL, e.REL_Y, dy)
        if wheel:
            # one unit = 60/120 of a detent on the Windows side
            hires = wheel * 60
            self.dev.write(e.EV_REL, e.REL_WHEEL_HI_RES, hires)
            self.wheel_acc += hires
            notches = int(self.wheel_acc / 120)
            if notches:
                self.dev.write(e.EV_REL, e.REL_WHEEL, notches)
                self.wheel_acc -= notches * 120
        self.dev.syn()

    def close(self) -> None:
        self.dev.close()


class AbsPointer:
    """Absolute pointer spanning the whole desktop (0..65535 on both axes)."""

    MAX = 65535

    def __init__(self) -> None:
        ax = AbsInfo(0, 0, self.MAX, 0, 0, 0)
        caps = {e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
                e.EV_ABS: [(e.ABS_X, ax), (e.ABS_Y, ax)]}
        self.dev = UInput(caps, name="Monect Absolute Pointer")

    def move(self, fx: float, fy: float) -> None:
        clamp = lambda v: max(0, min(self.MAX, int(v * self.MAX)))
        self.dev.write(e.EV_ABS, e.ABS_X, clamp(fx))
        self.dev.write(e.EV_ABS, e.ABS_Y, clamp(fy))
        self.dev.syn()

    def close(self) -> None:
        self.dev.close()


class TouchScreen:
    """Multi-touch screen covering the streamed monitor (remote desktop touch)."""

    MAX = 65535
    SLOTS = 10

    def __init__(self) -> None:
        ax = AbsInfo(0, 0, self.MAX, 0, 0, 0)
        caps = {
            e.EV_KEY: [e.BTN_TOUCH],
            e.EV_ABS: [(e.ABS_X, ax), (e.ABS_Y, ax),
                       (e.ABS_MT_SLOT, AbsInfo(0, 0, self.SLOTS - 1, 0, 0, 0)),
                       (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                       (e.ABS_MT_POSITION_X, ax), (e.ABS_MT_POSITION_Y, ax)],
        }
        self.dev = UInput(caps, name="Monect Touchscreen", input_props=[e.INPUT_PROP_DIRECT])
        self.slots: dict[int, int] = {}          # contact id -> slot
        self.next_tid = 1

    def update(self, points: list[tuple[int, int, float, float]]) -> None:
        """points: (contact id, flags, x 0..1, y 0..1); flags 1 down, 2 move, 4 up."""
        clamp = lambda v: max(0, min(self.MAX, int(v * self.MAX)))
        for cid, flags, x, y in points:
            if flags & 4:
                slot = self.slots.pop(cid, None)
                if slot is None:
                    continue
                self.dev.write(e.EV_ABS, e.ABS_MT_SLOT, slot)
                self.dev.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
                continue
            if cid not in self.slots:
                used = set(self.slots.values())
                free = [s for s in range(self.SLOTS) if s not in used]
                if not free:
                    continue
                self.slots[cid] = free[0]
                self.dev.write(e.EV_ABS, e.ABS_MT_SLOT, free[0])
                self.dev.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, self.next_tid)
                self.next_tid = (self.next_tid + 1) & 0xFFFF
            else:
                self.dev.write(e.EV_ABS, e.ABS_MT_SLOT, self.slots[cid])
            self.dev.write(e.EV_ABS, e.ABS_MT_POSITION_X, clamp(x))
            self.dev.write(e.EV_ABS, e.ABS_MT_POSITION_Y, clamp(y))
            if not self.slots or len(self.slots) == 1:
                self.dev.write(e.EV_ABS, e.ABS_X, clamp(x))
                self.dev.write(e.EV_ABS, e.ABS_Y, clamp(y))
        self.dev.write(e.EV_KEY, e.BTN_TOUCH, 1 if self.slots else 0)
        self.dev.syn()

    def release_all(self) -> None:
        self.update([(cid, 4, 0, 0) for cid in list(self.slots)])

    def close(self) -> None:
        self.release_all()
        self.dev.close()


class Keyboard:
    """Keyboard fed with HID boot reports, single scancodes or consumer bits."""

    def __init__(self) -> None:
        self.dev = UInput({e.EV_KEY: ALL_KEYS}, name="Monect Virtual Keyboard")
        self.pressed: set[int] = set()
        self.consumer = 0

    def _set(self, key: int, down: bool) -> None:
        self.dev.write(e.EV_KEY, key, 1 if down else 0)

    def boot_report(self, report: bytes) -> None:
        mods = report[0]
        now: set[int] = {k for i, k in enumerate(MODIFIER_KEYS) if mods & (1 << i)}
        for usage in report[2:8]:
            if usage > 3 and usage in HID_TO_KEY:
                now.add(HID_TO_KEY[usage])
        for k in self.pressed - now:
            self._set(k, False)
        for k in now - self.pressed:
            self._set(k, True)
        self.pressed = now
        self.dev.syn()

    def scancode(self, code: int, extended: bool, down: bool) -> None:
        key = (SET1_E0_TO_KEY if extended else SET1_TO_KEY).get(code)
        if key is None:
            log.info("unmapped scancode %s%02x", "e0 " if extended else "", code)
            return
        self._set(key, down)
        self.dev.syn()

    def consumer_bits(self, mask: int) -> None:
        changed = mask ^ self.consumer
        for bit, key in enumerate(CONSUMER_BITS):
            if changed & (1 << bit):
                self._set(key, bool(mask & (1 << bit)))
        self.consumer = mask
        self.dev.syn()

    def tap(self, *keys: int) -> None:
        for k in keys:
            self._set(k, True)
        for k in reversed(keys):
            self._set(k, False)
        self.dev.syn()

    def release_all(self) -> None:
        for k in self.pressed:
            self._set(k, False)
        self.pressed.clear()
        self.consumer_bits(0)

    def close(self) -> None:
        self.release_all()
        self.dev.close()


class Pen:
    """Pressure-sensitive stylus (tablet) covering the whole desktop."""

    MAX = 32767

    def __init__(self) -> None:
        ax = AbsInfo(0, 0, self.MAX, 0, 0, 200)
        caps = {e.EV_KEY: [e.BTN_TOOL_PEN, e.BTN_TOOL_RUBBER, e.BTN_TOUCH, e.BTN_STYLUS,
                           e.BTN_STYLUS2],
                e.EV_ABS: [(e.ABS_X, ax), (e.ABS_Y, ax),
                           (e.ABS_PRESSURE, AbsInfo(0, 0, 4095, 0, 0, 0)),
                           (e.ABS_TILT_X, AbsInfo(0, -90, 90, 0, 0, 0)),
                           (e.ABS_TILT_Y, AbsInfo(0, -90, 90, 0, 0, 0)),
                           (e.ABS_DISTANCE, AbsInfo(0, 0, 255, 0, 0, 0))]}
        self.dev = UInput(caps, name="Monect Pen", input_props=[e.INPUT_PROP_DIRECT],
                          bustype=e.BUS_USB, vendor=0x056A, product=0x5000)
        self.tool: int | None = None

    def event(self, flags: int, fx: float, fy: float, eraser: bool, pressure: float,
              orientation: float, tilt: float) -> None:
        """flags: Windows POINTER_FLAG_* (0x2 in range, 0x4 in contact, 0x10/0x20 barrel buttons,
        0x40000 up, 0x8000 canceled)."""
        import math
        w = self.dev.write
        tool = e.BTN_TOOL_RUBBER if eraser else e.BTN_TOOL_PEN
        in_range = bool(flags & 0x2) and not flags & 0x8000
        contact = bool(flags & 0x4) and in_range
        if self.tool is not None and (self.tool != tool or not in_range):
            w(e.EV_KEY, e.BTN_TOUCH, 0)
            w(e.EV_ABS, e.ABS_PRESSURE, 0)
            w(e.EV_KEY, self.tool, 0)
            self.dev.syn()
            self.tool = None
        if not in_range:
            return
        clamp = lambda v: max(0, min(self.MAX, int(v * self.MAX)))
        if self.tool is None:
            w(e.EV_KEY, tool, 1)
            self.tool = tool
        w(e.EV_ABS, e.ABS_X, clamp(fx))
        w(e.EV_ABS, e.ABS_Y, clamp(fy))
        w(e.EV_ABS, e.ABS_PRESSURE, int(max(0.0, min(1.0, pressure)) * 4095) if contact else 0)
        deg = math.degrees(tilt) if tilt == tilt else 0.0
        tx = deg * math.sin(orientation) if orientation == orientation else 0.0
        ty = -deg * math.cos(orientation) if orientation == orientation else 0.0
        w(e.EV_ABS, e.ABS_TILT_X, int(max(-90, min(90, tx))))
        w(e.EV_ABS, e.ABS_TILT_Y, int(max(-90, min(90, ty))))
        w(e.EV_KEY, e.BTN_STYLUS, 1 if flags & 0x10 else 0)
        w(e.EV_KEY, e.BTN_STYLUS2, 1 if flags & 0x20 else 0)
        w(e.EV_KEY, e.BTN_TOUCH, 1 if contact else 0)
        self.dev.syn()

    def close(self) -> None:
        self.dev.close()


class X360Pad:
    """Xbox 360 style pad fed with XUSB reports (same layout as XInput)."""

    BUTTONS = [
        (0x0010, e.BTN_START), (0x0020, e.BTN_SELECT), (0x0040, e.BTN_THUMBL),
        (0x0080, e.BTN_THUMBR), (0x0100, e.BTN_TL), (0x0200, e.BTN_TR),
        (0x0400, e.BTN_MODE), (0x1000, e.BTN_SOUTH), (0x2000, e.BTN_EAST),
        (0x4000, e.BTN_NORTH), (0x8000, e.BTN_WEST),
    ]

    def __init__(self, index: int, rumble=None) -> None:
        stick = AbsInfo(0, -32768, 32767, 16, 128, 0)
        trig = AbsInfo(0, 0, 255, 0, 0, 0)
        hat = AbsInfo(0, -1, 1, 0, 0, 0)
        caps = {
            e.EV_KEY: [b for _, b in self.BUTTONS],
            e.EV_ABS: [(e.ABS_X, stick), (e.ABS_Y, stick), (e.ABS_RX, stick),
                       (e.ABS_RY, stick), (e.ABS_Z, trig), (e.ABS_RZ, trig),
                       (e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat)],
            e.EV_FF: [e.FF_RUMBLE, e.FF_PERIODIC, e.FF_SQUARE, e.FF_TRIANGLE, e.FF_SINE,
                      e.FF_GAIN],
        }
        # Microsoft X-Box 360 pad ids so games pick the right mapping
        self.dev = UInput(caps, name="Microsoft X-Box 360 pad (Monect)",
                          vendor=0x045E, product=0x028E, version=0x0110,
                          bustype=e.BUS_USB, max_effects=16)
        self.index = index
        self.last: tuple | None = None
        self.rumble = rumble                   # callback(index, large 0..255, small 0..255)
        self.effects: dict[int, tuple[int, int]] = {}
        self.playing: set[int] = set()
        self.closed = False
        threading.Thread(target=self._ff_loop, daemon=True, name=f"ff{index}").start()

    def _ff_loop(self) -> None:
        """Force feedback from games: uploads/erases/plays -> rumble sent to the phone."""
        try:
            for ev in self.dev.read_loop():
                if ev.type == e.EV_UINPUT and ev.code == e.UI_FF_UPLOAD:
                    up = self.dev.begin_upload(ev.value)
                    up.retval = 0
                    eff = up.effect
                    if eff.type == e.FF_RUMBLE:
                        r = eff.u.ff_rumble_effect
                        self.effects[eff.id] = (r.strong_magnitude >> 8, r.weak_magnitude >> 8)
                    elif eff.type == e.FF_PERIODIC:
                        mag = abs(eff.u.ff_periodic_effect.magnitude) >> 7
                        self.effects[eff.id] = (min(mag, 255), min(mag, 255))
                    self.dev.end_upload(up)
                elif ev.type == e.EV_UINPUT and ev.code == e.UI_FF_ERASE:
                    er = self.dev.begin_erase(ev.value)
                    er.retval = 0
                    self.effects.pop(er.effect_id, None)
                    self.playing.discard(er.effect_id)
                    self.dev.end_erase(er)
                    self._update()
                elif ev.type == e.EV_FF and ev.code in self.effects:
                    (self.playing.add if ev.value else self.playing.discard)(ev.code)
                    self._update()
        except (OSError, ValueError):
            pass                                     # device closed

    def _update(self) -> None:
        large = max((self.effects[i][0] for i in self.playing if i in self.effects), default=0)
        small = max((self.effects[i][1] for i in self.playing if i in self.effects), default=0)
        if self.rumble and not self.closed:
            self.rumble(self.index, large, small)

    @staticmethod
    def _inv(v: int) -> int:
        return max(-32768, min(32767, -v - 1))

    def report(self, rep: bytes) -> None:
        buttons, lt, rt, lx, ly, rx, ry = struct.unpack("<HBBhhhh", rep[:12])
        state = (buttons, lt, rt, lx, ly, rx, ry)
        if state == self.last:
            return
        self.last = state
        for bit, btn in self.BUTTONS:
            self.dev.write(e.EV_KEY, btn, 1 if buttons & bit else 0)
        hx = (1 if buttons & 0x8 else 0) - (1 if buttons & 0x4 else 0)
        hy = (1 if buttons & 0x2 else 0) - (1 if buttons & 0x1 else 0)
        for code, val in ((e.ABS_HAT0X, hx), (e.ABS_HAT0Y, hy), (e.ABS_Z, lt),
                          (e.ABS_RZ, rt), (e.ABS_X, lx), (e.ABS_Y, self._inv(ly)),
                          (e.ABS_RX, rx), (e.ABS_RY, self._inv(ry))):
            self.dev.write(e.EV_ABS, code, val)
        self.dev.syn()

    def close(self) -> None:
        self.closed = True
        self.dev.close()


class DInputPad:
    """Generic HID joystick: 32 buttons, 8 axes, one POV hat."""

    AXES = [e.ABS_X, e.ABS_Y, e.ABS_Z, e.ABS_RX, e.ABS_RY, e.ABS_RZ, e.ABS_THROTTLE, e.ABS_RUDDER]
    BUTTONS = [e.BTN_TRIGGER, e.BTN_THUMB, e.BTN_THUMB2, e.BTN_TOP, e.BTN_TOP2, e.BTN_PINKIE,
               e.BTN_BASE, e.BTN_BASE2, e.BTN_BASE3, e.BTN_BASE4, e.BTN_BASE5, e.BTN_BASE6,
               e.BTN_DEAD] + [e.BTN_TRIGGER_HAPPY1 + i for i in range(19)]
    # pov byte -> (hat x, hat y); 0x01 up, 0x41 right, 0x81 down, 0xC1 left
    POV = {0x01: (0, -1), 0x41: (1, 0), 0x81: (0, 1), 0xC1: (-1, 0)}

    def __init__(self, index: int) -> None:
        ax = AbsInfo(0, -32768, 32767, 16, 128, 0)
        hat = AbsInfo(0, -1, 1, 0, 0, 0)
        caps = {e.EV_KEY: self.BUTTONS,
                e.EV_ABS: [(a, ax) for a in self.AXES] + [(e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat)]}
        self.dev = UInput(caps, name=f"Monect Gamepad {index + 1}", vendor=0x1234,
                          product=0xBEAD, bustype=e.BUS_USB)
        self.last: tuple | None = None

    def report(self, rep: bytes) -> None:
        """rep = pov(1), buttons(u32 LE), 8 x i16 LE axes."""
        pov = rep[0]
        buttons = struct.unpack_from("<I", rep, 1)[0]
        axes = struct.unpack_from("<8h", rep, 5)
        state = (pov, buttons, axes)
        if state == self.last:
            return
        self.last = state
        for i, btn in enumerate(self.BUTTONS):
            self.dev.write(e.EV_KEY, btn, (buttons >> i) & 1)
        for code, v in zip(self.AXES, axes):
            self.dev.write(e.EV_ABS, code, v)
        hx, hy = self.POV.get(pov, (0, 0))
        self.dev.write(e.EV_ABS, e.ABS_HAT0X, hx)
        self.dev.write(e.EV_ABS, e.ABS_HAT0Y, hy)
        self.dev.syn()

    def close(self) -> None:
        self.dev.close()


class InputHub:
    """Owns every virtual device; created lazily on first use."""

    def __init__(self) -> None:
        self._mouse: Mouse | None = None
        self._kbd: Keyboard | None = None
        self._abs: AbsPointer | None = None
        self._touch: TouchScreen | None = None
        self._pen: Pen | None = None
        self.pads: dict = {}
        self.rumble = None                     # set by the server: callback(index, large, small)

    @property
    def pen(self) -> Pen:
        if self._pen is None:
            self._pen = Pen()
        return self._pen

    @property
    def touch(self) -> TouchScreen:
        if self._touch is None:
            self._touch = TouchScreen()
        return self._touch

    @property
    def absptr(self) -> AbsPointer:
        if self._abs is None:
            self._abs = AbsPointer()
        return self._abs

    @property
    def mouse(self) -> Mouse:
        if self._mouse is None:
            self._mouse = Mouse()
        return self._mouse

    @property
    def kbd(self) -> Keyboard:
        if self._kbd is None:
            self._kbd = Keyboard()
        return self._kbd

    def pad(self, index: int) -> X360Pad:
        if index not in self.pads:
            log.info("plugging virtual Xbox 360 pad #%d", index)
            self.pads[index] = X360Pad(index, lambda *a: self.rumble and self.rumble(*a))
        return self.pads[index]

    def dpad(self, index: int) -> DInputPad:
        key = 100 + index
        if key not in self.pads:
            log.info("plugging virtual DirectInput pad #%d", index)
            self.pads[key] = DInputPad(index)
        return self.pads[key]

    def unplug(self, index: int) -> None:
        pad = self.pads.pop(index, None)
        if pad:
            log.info("unplugging pad #%d", index)
            pad.close()

    def paste_text(self, text: str) -> None:
        """Type text with the current keyboard layout; clipboard only for the rest."""
        if not hasattr(self, "_typer"):
            from .typer import Typer
            self._typer = Typer()
        rest = self._typer.type_text(self.kbd, text)
        if not rest:
            return
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=rest.encode(), check=False, timeout=5)
                break
        else:
            q = shutil.which("qdbus6") or shutil.which("qdbus")
            if not q:
                log.warning("cannot type %r and no clipboard tool available", rest)
                return
            subprocess.run([q, "org.kde.klipper", "/klipper", "setClipboardContents", rest],
                           check=False, timeout=5)
        self.kbd.tap(e.KEY_LEFTCTRL, e.KEY_V)

    def release_all(self) -> None:
        if self._touch:
            self._touch.release_all()
        if self._kbd:
            self._kbd.release_all()
        if self._mouse:
            self._mouse.report(0, 0, 0, 0)
        if self._pen:
            self._pen.event(0, 0, 0, False, 0, 0, 0)

    def close(self) -> None:
        for idx in list(self.pads):
            self.unplug(idx)
        for d in (self._mouse, self._kbd, self._abs, self._touch, self._pen):
            if d:
                d.close()
