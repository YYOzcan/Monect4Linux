# Monect4Linux

**Linux receiver for the Monect *PC Remote* Android app** – use your phone as a
touchpad, keyboard, gamepad, motion controller, remote screen and more on a
Linux PC. A from-scratch re-implementation of the Windows "PC Remote Receiver".

**Monect *PC Remote* Android uygulaması için Linux alıcısı** – telefonunu Linux
bilgisayarda touchpad, klavye, oyun kolu, hareket kumandası, uzak ekran ve daha
fazlası olarak kullan. Windows'taki "PC Remote Receiver"ın sıfırdan yazılmış Linux karşılığı.

> Unofficial, independent project for interoperability. Not affiliated with
> Monect. Contains no Monect code or binaries.
> Resmi değildir; Monect ile bağlantısı yoktur, Monect'e ait kod/dosya içermez.

---

## Install / Kurulum (one click / tek komut)

```bash
curl -fsSL https://raw.githubusercontent.com/YYOzcan/Monect4Linux/main/install.sh | bash
```

or / ya da

```bash
git clone https://github.com/YYOzcan/Monect4Linux && cd Monect4Linux && ./install.sh
```

The installer detects Arch/CachyOS/Manjaro, Debian/Ubuntu/Mint, Fedora and
openSUSE, installs only the missing packages (asks for your password once),
allows virtual input devices, opens the firewall for your LAN if one is active,
and starts the receiver plus the tray app at every login.

Kurulum dağıtımını tanır, yalnızca eksik paketleri kurar (bir kez şifre sorar),
sanal giriş aygıtlarına izin verir, güvenlik duvarı açıksa yerel ağa izin verir;
alıcı ve tepsi uygulaması her oturum açılışında başlar.

| Command / Komut | |
|---|---|
| `monect4linux-setup --webcam` | phone camera as a webcam / telefon kamerası = web kamerası |
| `monect4linux-setup --update` | update / güncelle |
| `monect4linux-uninstall [--purge]` | remove / kaldır |

Requirements: Wayland or X11 desktop with PipeWire + xdg-desktop-portal (KDE,
GNOME…), Python 3.10+. NVIDIA (NVENC) or any CPU for video encoding.

## Features / Özellikler

| PC Remote feature | Linux |
|---|---|
| Touchpad, mouse, keyboard, media keys | ✅ uinput |
| Gamepads (Xbox 360 / DirectInput) | ✅ real virtual pads |
| 3D gesture / DSU motion (Wiimote-like) | ✅ cemuhook server `udp/26760` → Dolphin, Cemu, Ryujinx |
| Remote desktop + PC sound + touch | ✅ PipeWire capture, NVENC/x264, low-latency |
| Projector (phone screen on the PC) | ✅ |
| File explorer (browse, down/upload…) | ✅ Linux tree appears as drive `L:\` |
| Task manager | ✅ |
| Games (Steam + game shortcuts), emulator launcher | ✅ |
| Power widget (lock, sleep, reboot…), PPT/media/web widgets | ✅ |
| Virtual camera (phone → webcam) | ✅ needs `--webcam` |
| PC microphone → phone | ✅ |
| Data cable (phone FTP) | ✅ open the shown `ftp://` address in your file manager |
| Password / ask-before-connect security | ✅ |
| Monect AI, blackboard, PC webcam → phone | ❌ not yet |

## Usage / Kullanım

1. Phone and PC on the same network. / Telefon ve PC aynı ağda.
2. Open **PC Remote** on the phone, pick your computer. / Telefonda **PC Remote**'u aç, bilgisayarını seç.
3. First remote-desktop start shows the desktop's screen-sharing dialog once. /
   İlk uzak masaüstünde ekran paylaşım izni bir kez sorulur.

**Motion controls in Dolphin:** Options → Controller Settings → Alternate Input
Sources → DSU Client → `127.0.0.1:26760`.

Settings (language TR/EN, security, video quality) are in the **Monect4Linux**
tray app. / Ayarlar (dil, güvenlik, görüntü kalitesi) tepsideki **Monect4Linux** uygulamasında.

## Troubleshooting / Sorun giderme

```bash
systemctl --user status monect-receiver
journalctl --user -u monect-receiver -f
```

* Phone doesn't see the PC → UDP 28451 must be reachable on the LAN.
* Laggy remote desktop → lower the bitrate in the tray app (6–8 Mbit/s), use 5 GHz Wi-Fi.

## How it works

[`PROTOCOL.md`](PROTOCOL.md) documents the LAN protocol (UDP discovery/handshake,
WebRTC signaling tunnel, data-channel message formats) as reconstructed for
interoperability. `tests/fake_phone.py` is a protocol self-test that plays the
phone's role.

License: MIT
