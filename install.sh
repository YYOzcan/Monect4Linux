#!/usr/bin/env bash
# Monect4Linux installer / updater
#
#   curl -fsSL https://raw.githubusercontent.com/YYOzcan/Monect4Linux/main/install.sh | bash
#
# Options:  --webcam   also set up the phone-camera virtual webcam (v4l2loopback)
#           --update   pull the latest version and reinstall
#           --no-sudo  skip everything that needs root (packages, uinput rule, firewall)
set -euo pipefail

REPO="https://github.com/YYOzcan/Monect4Linux.git"
TARGET="${MONECT4LINUX_DIR:-$HOME/.local/share/monect4linux}"
WEBCAM=0 UPDATE=0 NOSUDO=0
for a in "$@"; do
  case "$a" in
    --webcam) WEBCAM=1 ;;
    --update) UPDATE=1 ;;
    --no-sudo) NOSUDO=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m ✗ %s\033[0m\n' "$*"; exit 1; }

SUDO=""
need_root() {
  [ "$NOSUDO" = 1 ] && return 1
  if [ "$(id -u)" = 0 ]; then SUDO=""; return 0; fi
  command -v sudo >/dev/null || return 1
  SUDO="sudo"
  return 0
}

# ---------------------------------------------------------------- source tree
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/monect_rx/server.py" ]; then
  DIR="$SELF_DIR"
else
  command -v git >/dev/null || die "git is required (install git first)"
  if [ -d "$TARGET/.git" ]; then
    say "Updating $TARGET"; git -C "$TARGET" pull --ff-only
  else
    say "Downloading Monect4Linux to $TARGET"
    mkdir -p "$(dirname "$TARGET")"; git clone --depth 1 "$REPO" "$TARGET"
  fi
  DIR="$TARGET"
fi
if [ "$UPDATE" = 1 ] && [ -d "$DIR/.git" ]; then
  say "Updating"; git -C "$DIR" pull --ff-only || warn "git pull failed, keeping current version"
fi
cd "$DIR"

# ------------------------------------------------------------ system packages
. /etc/os-release 2>/dev/null || true
ID_ALL="${ID:-} ${ID_LIKE:-}"
missing=()
have_py() { python3 -c "import $1" >/dev/null 2>&1; }
have_gst() { gst-inspect-1.0 "$1" >/dev/null 2>&1; }

if [[ "$ID_ALL" == *arch* ]]; then
  PM="pacman -S --needed --noconfirm"
  have_py gi            || missing+=(python-gobject)
  have_py dbus          || missing+=(python-dbus)
  have_gst pipewiresrc  || missing+=(gst-plugin-pipewire)
  have_gst videoconvert || missing+=(gst-plugins-base)
  command -v ffplay  >/dev/null || missing+=(ffmpeg)
  command -v pactl   >/dev/null || missing+=(libpulse)
  command -v git     >/dev/null || missing+=(git)
  WEBCAM_PKG="v4l2loopback-dkms linux-headers"
elif [[ "$ID_ALL" == *debian* || "$ID_ALL" == *ubuntu* ]]; then
  PM="apt-get install -y"
  python3 -c "import venv" 2>/dev/null && python3 -m ensurepip --version >/dev/null 2>&1 || missing+=(python3-venv)
  have_py gi            || missing+=(python3-gi gir1.2-gstreamer-1.0)
  have_py dbus          || missing+=(python3-dbus)
  have_gst pipewiresrc  || missing+=(gstreamer1.0-pipewire)
  have_gst videoconvert || missing+=(gstreamer1.0-plugins-base)
  command -v ffplay  >/dev/null || missing+=(ffmpeg)
  command -v pactl   >/dev/null || missing+=(pulseaudio-utils)
  WEBCAM_PKG="v4l2loopback-dkms"
elif [[ "$ID_ALL" == *fedora* || "$ID_ALL" == *rhel* ]]; then
  PM="dnf install -y"
  have_py gi            || missing+=(python3-gobject gstreamer1)
  have_py dbus          || missing+=(python3-dbus)
  have_gst pipewiresrc  || missing+=(pipewire-gstreamer)
  have_gst videoconvert || missing+=(gstreamer1-plugins-base)
  command -v ffplay  >/dev/null || missing+=(ffmpeg-free)
  command -v pactl   >/dev/null || missing+=(pulseaudio-utils)
  WEBCAM_PKG="v4l2loopback akmod-v4l2loopback"
elif [[ "$ID_ALL" == *suse* ]]; then
  PM="zypper install -y"
  have_py gi            || missing+=(python3-gobject typelib-1_0-Gst-1_0)
  have_py dbus          || missing+=(python3-dbus-python)
  have_gst pipewiresrc  || missing+=(gstreamer-plugin-pipewire)
  command -v ffplay  >/dev/null || missing+=(ffmpeg)
  command -v pactl   >/dev/null || missing+=(pulseaudio-utils)
  WEBCAM_PKG="v4l2loopback-kmp-default"
else
  PM=""; warn "Unknown distribution ($ID_ALL): install PyGObject, dbus-python, GStreamer PipeWire plugin and ffmpeg yourself"
fi

if [ "${#missing[@]}" -gt 0 ] && [ -n "$PM" ]; then
  say "Installing system packages: ${missing[*]}"
  if need_root; then
    [[ "$PM" == apt-get* ]] && $SUDO apt-get update -qq || true
    $SUDO $PM "${missing[@]}" || warn "package installation failed – some features may not work"
  else
    warn "Skipped (no sudo). Install manually: ${missing[*]}"
  fi
else
  ok "System packages present"
fi

# --------------------------------------------------- uinput access (input devices)
if [ ! -w /dev/uinput ]; then
  if need_root; then
    say "Allowing the logged-in user to create virtual input devices (/dev/uinput)"
    echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
      | $SUDO tee /etc/udev/rules.d/60-monect4linux-uinput.rules >/dev/null
    echo uinput | $SUDO tee /etc/modules-load.d/monect4linux-uinput.conf >/dev/null
    $SUDO modprobe uinput || true
    $SUDO udevadm control --reload-rules && $SUDO udevadm trigger --name-match=uinput || true
  else
    warn "/dev/uinput is not writable – mouse/keyboard/gamepads need it (rerun without --no-sudo)"
  fi
fi
[ -w /dev/uinput ] && ok "Virtual input devices allowed"

# ------------------------------------------------------------- python venv
say "Creating Python environment"
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -q --upgrade pip >/dev/null 2>&1 || true
.venv/bin/pip install -q -r requirements.txt
ok "Python packages installed"

# ------------------------------------------------------------------ firewall
LAN="$(ip -4 route 2>/dev/null | awk '/proto kernel/ && /scope link/ {print $1; exit}')"
if [ -n "$LAN" ]; then
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    need_root && { say "Opening firewall for $LAN (ufw)"; $SUDO ufw allow from "$LAN" proto udp comment Monect4Linux >/dev/null; }
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    need_root && { say "Opening firewall for $LAN (firewalld)"
      $SUDO firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=$LAN protocol value=udp accept" >/dev/null
      $SUDO firewall-cmd --reload >/dev/null; }
  fi
fi

# ------------------------------------------------------- optional virtual webcam
if [ "$WEBCAM" = 1 ]; then
  if need_root; then
    say "Setting up the virtual webcam (v4l2loopback)"
    modinfo v4l2loopback >/dev/null 2>&1 || $SUDO $PM $WEBCAM_PKG || warn "could not install v4l2loopback"
    echo v4l2loopback | $SUDO tee /etc/modules-load.d/monect4linux-v4l2loopback.conf >/dev/null
    echo 'options v4l2loopback video_nr=42 card_label="Monect Camera" exclusive_caps=1' \
      | $SUDO tee /etc/modprobe.d/monect4linux-v4l2loopback.conf >/dev/null
    $SUDO modprobe -r v4l2loopback 2>/dev/null || true
    $SUDO modprobe v4l2loopback && ok "Virtual webcam: /dev/video42 (Monect Camera)"
  else
    warn "--webcam needs sudo"
  fi
fi

# ------------------------------------------------------ service, menu, autostart
say "Installing service and menu entry"
mkdir -p ~/.config/systemd/user ~/.local/share/applications ~/.config/autostart ~/.local/bin
sed "s|@DIR@|$DIR|g" packaging/monect-receiver.service > ~/.config/systemd/user/monect-receiver.service
sed "s|@DIR@|$DIR|g" packaging/monect4linux.desktop > ~/.local/share/applications/monect4linux.desktop
sed "s|@DIR@|$DIR|g; s|^Exec=\(.*\)$|Exec=\1 --hidden|" packaging/monect4linux.desktop \
  > ~/.config/autostart/monect4linux.desktop
rm -f ~/.config/autostart/monect-receiver.desktop ~/.local/share/applications/monect-receiver.desktop
ln -sf "$DIR/install.sh" ~/.local/bin/monect4linux-setup
ln -sf "$DIR/uninstall.sh" ~/.local/bin/monect4linux-uninstall
systemctl --user daemon-reload
systemctl --user enable monect-receiver.service >/dev/null 2>&1
systemctl --user restart monect-receiver.service
sleep 1
systemctl --user is-active --quiet monect-receiver.service && ok "Receiver service running" \
  || warn "service did not start: journalctl --user -u monect-receiver"

pkill -f "monect_rx.ui" 2>/dev/null || true
(setsid "$DIR/monect-receiver-ui" >/dev/null 2>&1 &) || true

IP="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
echo
ok "Monect4Linux installed."
echo "   • Phone: open PC Remote, pick \"$(hostname)\"  (IP ${IP:-?})"
echo "   • Settings: 'Monect4Linux' in the app menu / tray icon"
echo "   • Webcam: monect4linux-setup --webcam     • Update: monect4linux-setup --update"
