"""Layout-aware text typing through the virtual keyboard.

Uses libxkbcommon (ctypes) to find, for the user's current keyboard layout,
which key + modifiers produce each character, so typed text arrives exactly as
sent (Turkish ğüşıöç, @, € … included) without touching the clipboard.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import re
import shutil
import subprocess

log = logging.getLogger("typer")


class _RuleNames(ctypes.Structure):
    _fields_ = [("rules", ctypes.c_char_p), ("model", ctypes.c_char_p),
                ("layout", ctypes.c_char_p), ("variant", ctypes.c_char_p),
                ("options", ctypes.c_char_p)]


def current_layout() -> tuple[str, str, str, str]:
    """(layout, variant, model, options) of the first active layout."""
    layout = variant = model = options = ""
    if shutil.which("kreadconfig6"):
        def k(key):
            return subprocess.run(["kreadconfig6", "--file", "kxkbrc", "--group", "Layout",
                                   "--key", key], capture_output=True, text=True).stdout.strip()
        layout, variant, model, options = (k("LayoutList"), k("VariantList"), k("Model"),
                                           k("Options"))
    if not layout and shutil.which("localectl"):
        out = subprocess.run(["localectl", "status"], capture_output=True, text=True).stdout
        def g(name):
            m = re.search(rf"X11 {name}:\s*(\S+)", out)
            return m.group(1) if m else ""
        layout, variant, model, options = g("Layout"), g("Variant"), g("Model"), g("Options")
    first = lambda s: s.split(",")[0] if s else ""
    return first(layout) or "us", first(variant), model, options


def build_charmap() -> dict[str, tuple[int, bool, bool]]:
    """char -> (linux keycode, shift, altgr) for the current layout."""
    lib = ctypes.CDLL(ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0")
    lib.xkb_context_new.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RuleNames),
                                              ctypes.c_int]
    lib.xkb_keymap_num_levels_for_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    lib.xkb_keymap_key_get_syms_by_level.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32))]
    lib.xkb_keymap_key_get_mods_for_level.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t]
    lib.xkb_keymap_mod_get_index.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.xkb_keymap_mod_get_index.restype = ctypes.c_uint32
    lib.xkb_keysym_to_utf32.argtypes = [ctypes.c_uint32]
    lib.xkb_keysym_to_utf32.restype = ctypes.c_uint32

    layout, variant, model, options = current_layout()
    names = _RuleNames(b"evdev", (model or "pc105").encode(), layout.encode(),
                       variant.encode() or None, options.encode() or None)
    ctx = lib.xkb_context_new(0)
    km = lib.xkb_keymap_new_from_names(ctx, ctypes.byref(names), 0)
    if not km:
        raise RuntimeError(f"xkb keymap for {layout}/{variant} failed")
    shift_bit = 1 << lib.xkb_keymap_mod_get_index(km, b"Shift")
    l3 = lib.xkb_keymap_mod_get_index(km, b"Mod5")
    altgr_bit = 1 << l3 if l3 < 32 else 0
    lock_bit = 1 << lib.xkb_keymap_mod_get_index(km, b"Lock")
    chars: dict[str, tuple[int, bool, bool]] = {}
    for kc in range(8, 256):
        levels = lib.xkb_keymap_num_levels_for_key(km, kc, 0)
        for level in range(levels):
            syms = ctypes.POINTER(ctypes.c_uint32)()
            n = lib.xkb_keymap_key_get_syms_by_level(km, kc, 0, level, ctypes.byref(syms))
            if n != 1:
                continue
            cp = lib.xkb_keysym_to_utf32(syms[0])
            if not cp:
                continue
            masks = (ctypes.c_uint32 * 8)()
            m = lib.xkb_keymap_key_get_mods_for_level(km, kc, 0, level, masks, 8)
            if m < 1:
                continue
            options = [masks[i] for i in range(m) if not masks[i] & lock_bit]
            if not options:
                continue                       # only reachable with Caps Lock
            mask = min(options, key=lambda v: bin(v).count("1"))
            if mask & ~(shift_bit | altgr_bit):
                continue                       # needs some other modifier; skip
            ch = chr(cp)
            entry = (kc - 8, bool(mask & shift_bit), bool(mask & altgr_bit))
            # prefer the simplest way to type a character
            if ch not in chars or sum(chars[ch][1:]) > sum(entry[1:]):
                chars[ch] = entry
    log.info("keyboard layout %s%s: %d typeable characters", layout,
             f"({variant})" if variant else "", len(chars))
    return chars


class Typer:
    def __init__(self) -> None:
        self.map: dict[str, tuple[int, bool, bool]] | None = None

    def charmap(self) -> dict:
        if self.map is None:
            try:
                self.map = build_charmap()
            except Exception as ex:
                log.warning("cannot load keyboard layout (%s); falling back to clipboard", ex)
                self.map = {}
        return self.map

    def type_text(self, kbd, text: str) -> str:
        """Types what it can with `kbd` (inputdev.Keyboard); returns the untypeable rest."""
        from evdev import ecodes as e
        cmap = self.charmap()
        rest = []
        for ch in text:
            if ch in ("\n", "\r"):
                kbd.tap(e.KEY_ENTER)
                continue
            if ch == "\t":
                kbd.tap(e.KEY_TAB)
                continue
            if ch == "\b":
                kbd.tap(e.KEY_BACKSPACE)
                continue
            entry = cmap.get(ch)
            if entry is None:
                rest.append(ch)
                continue
            code, shift, altgr = entry
            mods = ([e.KEY_LEFTSHIFT] if shift else []) + ([e.KEY_RIGHTALT] if altgr else [])
            kbd.tap(*mods, code)
        return "".join(rest)
