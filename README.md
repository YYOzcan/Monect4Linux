# Monect4Linux

**Linux receiver for the Monect *PC Remote* Android app.** Use your phone as a
touchpad, keyboard, gamepad, motion controller, remote screen and more on a
Linux PC — a from-scratch Linux counterpart of the Windows "PC Remote Receiver".

[Türkçe](README.tr.md)

> Unofficial, independent project for interoperability. Not affiliated with
> Monect. Contains no Monect code or binaries.

## Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/YYOzcan/Monect4Linux/main/install.sh | bash
```

or

```bash
git clone https://github.com/YYOzcan/Monect4Linux && cd Monect4Linux && ./install.sh
```

The installer detects Arch/CachyOS/Manjaro, Debian/Ubuntu/Mint, Fedora and
openSUSE, installs only missing packages (asks for your password once), allows
virtual input devices, opens the firewall for your LAN if one is active, and
starts the receiver plus the tray app at every login.

| Command | |
|---|---|
| `monect4linux-setup --webcam` | use the phone camera as a webcam |
| `monect4linux-setup --update` | update |
| `monect4linux-uninstall [--purge]` | remove |

Requirements: KDE, GNOME or another desktop with PipeWire + xdg-desktop-portal
(Wayland or X11), Python 3.10+. NVIDIA NVENC is used when present, otherwise x264.

## Features

| PC Remote feature | Linux |
|---|---|
| Touchpad, mouse, keyboard (layout-aware typing), media keys | ✅ |
| Gamepads (Xbox 360 / DirectInput) | ✅ real virtual pads |
| 3D gesture / DSU motion (Wiimote-like) | ✅ cemuhook server `udp/26760` → Dolphin, Cemu, Ryujinx |
| Remote desktop + PC sound + multi-touch | ✅ low latency |
| Projector (phone screen on the PC) | ✅ |
| File explorer (browse, download/upload, rename, delete, open) | ✅ Linux tree appears as drive `L:\` |
| Task manager | ✅ |
| Games (Steam + game shortcuts), emulator launcher | ✅ |
| Power widget (lock, sleep, reboot…), PPT / media / web widgets | ✅ |
| Virtual camera (phone → webcam) | ✅ needs `--webcam` |
| PC microphone → phone | ✅ |
| Data cable (phone FTP) | ✅ open the shown `ftp://` address in your file manager |
| Security: open / ask before connecting / password | ✅ |
| Monect AI, blackboard, PC webcam → phone | ❌ not yet |

## Usage

1. Put the phone and the PC on the same network.
2. Open **PC Remote** on the phone and pick your computer.
3. The first remote-desktop session shows the desktop's screen-sharing dialog once.

**Motion controls in Dolphin:** Options → Controller Settings → Alternate Input
Sources → DSU Client → `127.0.0.1:26760`.

Settings (language, security, video quality, autostart) live in the
**Monect4Linux** tray app.

## Troubleshooting

```bash
systemctl --user status monect-receiver
journalctl --user -u monect-receiver -f
```

* Phone doesn't see the PC → UDP 28451 must be reachable on the LAN.
* Remote desktop not smooth → lower the bitrate in the tray app (6–8 Mbit/s), prefer 5 GHz Wi-Fi.

## How it works

[`PROTOCOL.md`](PROTOCOL.md) documents the LAN protocol (UDP discovery and
handshake, WebRTC signaling tunnel, data-channel message formats) as
reconstructed for interoperability. `tests/fake_phone.py` is a protocol
self-test that plays the phone's role.

License: MIT
