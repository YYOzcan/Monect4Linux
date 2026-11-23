"""Key code translation tables.

HID keyboard usage ids (USB HID Usage Tables, page 0x07) and PC/AT set-1
scancodes are translated to Linux input event codes.
"""
from evdev import ecodes as e

# --- HID usage page 0x07 -> Linux key ------------------------------------
HID_TO_KEY: dict[int, int] = {}

_letters = "abcdefghijklmnopqrstuvwxyz"
for i, ch in enumerate(_letters):
    HID_TO_KEY[0x04 + i] = getattr(e, f"KEY_{ch.upper()}")
for i, n in enumerate("1234567890"):
    HID_TO_KEY[0x1E + i] = getattr(e, f"KEY_{n}")

HID_TO_KEY.update({
    0x28: e.KEY_ENTER, 0x29: e.KEY_ESC, 0x2A: e.KEY_BACKSPACE, 0x2B: e.KEY_TAB,
    0x2C: e.KEY_SPACE, 0x2D: e.KEY_MINUS, 0x2E: e.KEY_EQUAL, 0x2F: e.KEY_LEFTBRACE,
    0x30: e.KEY_RIGHTBRACE, 0x31: e.KEY_BACKSLASH, 0x32: e.KEY_BACKSLASH,
    0x33: e.KEY_SEMICOLON, 0x34: e.KEY_APOSTROPHE, 0x35: e.KEY_GRAVE,
    0x36: e.KEY_COMMA, 0x37: e.KEY_DOT, 0x38: e.KEY_SLASH, 0x39: e.KEY_CAPSLOCK,
    0x46: e.KEY_SYSRQ, 0x47: e.KEY_SCROLLLOCK, 0x48: e.KEY_PAUSE,
    0x49: e.KEY_INSERT, 0x4A: e.KEY_HOME, 0x4B: e.KEY_PAGEUP, 0x4C: e.KEY_DELETE,
    0x4D: e.KEY_END, 0x4E: e.KEY_PAGEDOWN, 0x4F: e.KEY_RIGHT, 0x50: e.KEY_LEFT,
    0x51: e.KEY_DOWN, 0x52: e.KEY_UP, 0x53: e.KEY_NUMLOCK,
    0x54: e.KEY_KPSLASH, 0x55: e.KEY_KPASTERISK, 0x56: e.KEY_KPMINUS,
    0x57: e.KEY_KPPLUS, 0x58: e.KEY_KPENTER,
    0x59: e.KEY_KP1, 0x5A: e.KEY_KP2, 0x5B: e.KEY_KP3, 0x5C: e.KEY_KP4,
    0x5D: e.KEY_KP5, 0x5E: e.KEY_KP6, 0x5F: e.KEY_KP7, 0x60: e.KEY_KP8,
    0x61: e.KEY_KP9, 0x62: e.KEY_KP0, 0x63: e.KEY_KPDOT,
    0x64: e.KEY_102ND, 0x65: e.KEY_COMPOSE, 0x66: e.KEY_POWER, 0x67: e.KEY_KPEQUAL,
    0x7F: e.KEY_MUTE, 0x80: e.KEY_VOLUMEUP, 0x81: e.KEY_VOLUMEDOWN,
    0x85: e.KEY_KPCOMMA, 0x87: e.KEY_RO, 0x88: e.KEY_KATAKANAHIRAGANA,
    0x89: e.KEY_YEN, 0x8A: e.KEY_HENKAN, 0x8B: e.KEY_MUHENKAN,
    0x90: e.KEY_HANGEUL, 0x91: e.KEY_HANJA,
})
for i in range(12):                       # F1..F12
    HID_TO_KEY[0x3A + i] = getattr(e, f"KEY_F{i + 1}")
for i in range(12):                       # F13..F24
    HID_TO_KEY[0x68 + i] = getattr(e, f"KEY_F{i + 13}")

# modifier bits of the boot report byte 0 (usage 0xE0 + bit)
MODIFIER_KEYS = [
    e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_LEFTALT, e.KEY_LEFTMETA,
    e.KEY_RIGHTCTRL, e.KEY_RIGHTSHIFT, e.KEY_RIGHTALT, e.KEY_RIGHTMETA,
]
for i, k in enumerate(MODIFIER_KEYS):
    HID_TO_KEY[0xE0 + i] = k

# --- set-1 scancodes -------------------------------------------------------
# For non-extended set-1 codes 0x01..0x58 the Linux key code is numerically
# equal to the scancode (that is how the Linux AT keyboard map was laid out).
SET1_TO_KEY: dict[int, int] = {sc: sc for sc in range(0x01, 0x59)}
SET1_TO_KEY.update({0x56: e.KEY_102ND, 0x57: e.KEY_F11, 0x58: e.KEY_F12,
                    0x70: e.KEY_KATAKANAHIRAGANA, 0x73: e.KEY_RO,
                    0x79: e.KEY_HENKAN, 0x7B: e.KEY_MUHENKAN, 0x7D: e.KEY_YEN})

SET1_E0_TO_KEY: dict[int, int] = {
    0x1C: e.KEY_KPENTER, 0x1D: e.KEY_RIGHTCTRL, 0x35: e.KEY_KPSLASH,
    0x37: e.KEY_SYSRQ, 0x38: e.KEY_RIGHTALT, 0x46: e.KEY_PAUSE,
    0x47: e.KEY_HOME, 0x48: e.KEY_UP, 0x49: e.KEY_PAGEUP, 0x4B: e.KEY_LEFT,
    0x4D: e.KEY_RIGHT, 0x4F: e.KEY_END, 0x50: e.KEY_DOWN, 0x51: e.KEY_PAGEDOWN,
    0x52: e.KEY_INSERT, 0x53: e.KEY_DELETE, 0x5B: e.KEY_LEFTMETA,
    0x5C: e.KEY_RIGHTMETA, 0x5D: e.KEY_COMPOSE, 0x5E: e.KEY_POWER,
    0x5F: e.KEY_SLEEP, 0x63: e.KEY_WAKEUP,
    0x10: e.KEY_PREVIOUSSONG, 0x19: e.KEY_NEXTSONG, 0x20: e.KEY_MUTE,
    0x21: e.KEY_CALC, 0x22: e.KEY_PLAYPAUSE, 0x24: e.KEY_STOPCD,
    0x2E: e.KEY_VOLUMEDOWN, 0x30: e.KEY_VOLUMEUP, 0x32: e.KEY_HOMEPAGE,
    0x65: e.KEY_SEARCH, 0x66: e.KEY_BOOKMARKS, 0x67: e.KEY_REFRESH,
    0x68: e.KEY_STOP, 0x69: e.KEY_FORWARD, 0x6A: e.KEY_BACK,
    0x6B: e.KEY_COMPUTER, 0x6C: e.KEY_MAIL, 0x6D: e.KEY_MEDIA,
}

# --- consumer-control bitmask (category 0, sub 0x09) ------------------------
# bit index -> key, low byte first
CONSUMER_BITS: list[int] = [
    e.KEY_MUTE, e.KEY_VOLUMEUP, e.KEY_VOLUMEDOWN, e.KEY_PLAYPAUSE,
    e.KEY_STOPCD, e.KEY_PREVIOUSSONG, e.KEY_NEXTSONG, e.KEY_MEDIA,
    e.KEY_SEARCH, e.KEY_HOMEPAGE, e.KEY_BOOKMARKS, e.KEY_REFRESH,
    e.KEY_STOP, e.KEY_FORWARD, e.KEY_BACK, e.KEY_MAIL,
]

ALL_KEYS = sorted(set(HID_TO_KEY.values()) | set(SET1_TO_KEY.values())
                  | set(SET1_E0_TO_KEY.values()) | set(CONSUMER_BITS))
