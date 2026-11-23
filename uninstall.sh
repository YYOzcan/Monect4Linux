#!/usr/bin/env bash
# Monect4Linux uninstaller (keeps ~/.config/monect-linux unless --purge)
set -u
systemctl --user disable --now monect-receiver.service 2>/dev/null
pkill -f "monect_rx.ui" 2>/dev/null
rm -f ~/.config/systemd/user/monect-receiver.service \
      ~/.local/share/applications/monect4linux.desktop \
      ~/.config/autostart/monect4linux.desktop \
      ~/.local/bin/monect4linux-setup ~/.local/bin/monect4linux-uninstall
systemctl --user daemon-reload
if [ "${1:-}" = "--purge" ]; then
  rm -rf ~/.config/monect-linux ~/.local/share/monect4linux
  if command -v sudo >/dev/null; then
    sudo rm -f /etc/udev/rules.d/60-monect4linux-uinput.rules /etc/modules-load.d/monect4linux-*.conf \
               /etc/modprobe.d/monect4linux-v4l2loopback.conf
  fi
fi
echo "Monect4Linux removed."
